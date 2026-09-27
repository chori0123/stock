# -*- coding: utf-8 -*-
"""
전략 백테스트 (주간 재검증용).

전략 가정: 정규장 종가 부근 매수(NXT 저녁 매수의 근사) -> 다음 거래일 시가 매도.
비용: 연도별 실제 증권거래세(2016~ 0.30% ... 2026 0.20%) + 수수료·슬리피지 0.05%.

검증 설계 (과최적화 방지):
  - 탐색 기간 DEV  2016-01 ~ 2022-12 : 규칙 개선안은 이 기간 결과만 보고 골랐다.
  - 검증 기간 TEST 2023-01 ~ 현재    : 고른 규칙을 한 번만 평가한 기간.
  - 최근 1년 RECENT                 : 전략이 최근에도 통하는지 매주 감시 (나빠지면 경고).
  - 종목별 과거패턴은 실시간 프로그램과 똑같이 '각 날짜 기준 직전 3년'만 사용.

데이터: FinanceData/marcap (KRX 전종목 일별 시세 공개 데이터셋, 로그인 불필요)

실행:
    python backtest.py                      # 2015~현재 데이터 자동 다운로드 후 실행
    python backtest.py --marcap-dir 경로     # parquet 을 미리 받아둔 폴더 사용
"""

import argparse
import json
import logging
import os
import sys

import numpy as np
import pandas as pd

import config
import data_fetcher
import features as F
from signal_generator import _clean

logger = logging.getLogger("backtest")

CACHE_DIR = os.path.join(config.DATA_DIR, "cache")
MODEL_PATH = os.path.join(config.DATA_DIR, "model.json")
SUMMARY_JSON = os.path.join(config.BASE_DIR, "docs", "data", "backtest_summary.json")
REPORT_HTML = os.path.join(config.BASE_DIR, "docs", "backtest.html")
MARCAP_URL = "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"

DEV = ("20160101", "20221231")
TEST_START = "20230101"
PCOLS = ["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------
def load_panel_marcap(start_year: int = 2015, end_year: int = None, local_dir: str = None):
    import requests
    end_year = end_year or data_fetcher.now_kst().year
    os.makedirs(CACHE_DIR, exist_ok=True)
    cols = ["Code", "Name", "Date", "Market", "Open", "High", "Low", "Close", "Volume", "Amount",
            "ChangesRatio", "Marcap"]
    frames = []
    for y in range(start_year, end_year + 1):
        path = os.path.join(local_dir or CACHE_DIR, f"marcap-{y}.parquet")
        if not local_dir and (not os.path.exists(path) or y == end_year):
            logger.info("marcap %d 다운로드...", y)
            r = requests.get(MARCAP_URL.format(year=y), timeout=300)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
        if os.path.exists(path):
            frames.append(pd.read_parquet(path, columns=cols))
    raw = pd.concat(frames, ignore_index=True)
    raw["Market"] = raw["Market"].replace({"KOSDAQ GLOBAL": "KOSDAQ"})
    raw = raw[raw["Market"].isin(config.MARKETS) & ~raw["Name"].fillna("").str.contains("스팩")]
    raw["date"] = pd.to_datetime(raw["Date"]).dt.strftime("%Y%m%d")
    panel = pd.DataFrame({
        "ticker": raw["Code"].astype(str).str.zfill(6), "date": raw["date"], "market": raw["Market"],
        "open": raw["Open"], "high": raw["High"], "low": raw["Low"], "close": raw["Close"],
        "volume": raw["Volume"], "value": raw["Amount"], "chg": raw["ChangesRatio"], "cap": raw["Marcap"],
    }).drop_duplicates(["ticker", "date"]).reset_index(drop=True)
    return panel, sorted(panel["date"].unique())


def build_dataset(panel: pd.DataFrame, days: list, eval_start: str = DEV[0]) -> pd.DataFrame:
    """연도별로 나눠 지표 계산(메모리 절약) -> 유동성 조건 통과 행 + 종목별 과거패턴 통계 + 시장추세 + 비용."""
    trend = F.market_trend(panel)
    minimal, liq_rows = [], []
    years = sorted({d[:4] for d in days})
    for y in years:
        ydays = [d for d in days if d[:4] == y]
        i0, i1 = days.index(ydays[0]), days.index(ydays[-1])
        win = days[max(0, i0 - 45): min(len(days), i1 + 2)]
        f = F.compute_features(panel[panel["date"].isin(set(win))], win)
        f = f[f["date"].isin(set(ydays))]
        minimal.append(f[["ticker", "date"] + PCOLS + ["next_up", "target_gross"]])
        liq = F.liquid_mask(f, config.MIN_PRICE, config.MIN_TRADING_VALUE, config.MIN_MARKET_CAP,
                            config.MAX_PRICE) & f["target_valid"] & (f["date"] >= eval_start)
        rows = f[liq].copy()
        rows["rules"] = F.rules_mask(rows, config)
        liq_rows.append(rows[["ticker", "date", "market", "close", "chg", "vol_ratio", "close_strength",
                              "next_open", "target_gross", "rules"]])
        logger.info("  %s년 지표 계산 완료 (유동성 통과 %d행)", y, len(rows))
    mini = pd.concat(minimal, ignore_index=True)
    ds = pd.concat(liq_rows, ignore_index=True)
    logger.info("종목별 과거패턴 통계 계산 (기술적 조건 통과 %d행)...", int(ds["rules"].sum()))
    st = F.windowed_pattern_stats(mini, ds[ds["rules"]])
    ds = ds.join(st)
    ds[["ps_count", "ps_wins"]] = ds[["ps_count", "ps_wins"]].fillna(0).astype(int)
    ds["market_up"] = ds["date"].map(trend).eq(True)
    ds["cost"] = ds["date"].map(F.tax_rate) + config.FEE_SLIPPAGE
    ds["net"] = ds["target_gross"] - ds["cost"]
    return ds


# ---------------------------------------------------------------------------
# 선택 규칙
# ---------------------------------------------------------------------------
def _day_minmax(df, s):
    g = s.groupby(df["date"])
    lo, hi = g.transform("min"), g.transform("max")
    return ((s - lo) / (hi - lo)).where(hi > lo, 0.5)


def pick_heuristic(cand: pd.DataFrame, k: int) -> pd.DataFrame:
    """예전 버전 점수(등락률·거래량·종가강도 가중합) 상위 k개 - 비교용."""
    if cand.empty:
        return cand
    q95 = cand.groupby("date")["vol_ratio"].transform(lambda x: x.quantile(0.95))
    vr = cand["vol_ratio"].clip(upper=q95)
    score = (_day_minmax(cand, cand["chg"]) * 0.4 + _day_minmax(cand, vr) * 0.35
             + _day_minmax(cand, cand["close_strength"]) * 0.25)
    return cand.assign(score=score).sort_values(["date", "score"], ascending=[True, False]).groupby("date").head(k)


def pick_live(cand: pd.DataFrame, k: int) -> pd.DataFrame:
    """실시간 screener 와 동일한 정렬: 축소 상승률 -> 거래량배수, 하루 최대 k개."""
    d = cand.assign(_wr=(cand["ps_wins"] + 2) / (cand["ps_count"] + 4))
    return d.sort_values(["date", "_wr", "vol_ratio"], ascending=[True, False, False]).groupby("date").head(k)


def masks(ds: pd.DataFrame) -> dict:
    rate = ds["ps_wins"] / ds["ps_count"].replace(0, np.nan)
    old = ds["rules"] & (ds["ps_count"] >= 3) & (rate >= 0.6)
    new = ds["rules"] & pd.Series(F.own_pattern_confirmed(ds["ps_count"], ds["ps_wins"]), index=ds.index)
    if config.MARKET_TREND_FILTER:
        new &= ds["market_up"]
    return {"old": old.fillna(False), "new": new.fillna(False)}


# ---------------------------------------------------------------------------
# 성과 측정
# ---------------------------------------------------------------------------
def portfolio_stats(picks: pd.DataFrame, all_days: list) -> dict:
    """매일 선택 종목에 균등 투자 -> 다음날 시가 전량 매도. 신호 없는 날은 현금(0%)."""
    if picks is None or picks.empty:
        return {"trades": 0, "days_traded": 0}
    net = picks["net"]
    daily = picks.groupby("date")["net"].mean()
    eq = (1 + daily.reindex(all_days, fill_value=0.0)).cumprod()
    tstat = float(daily.mean() / daily.std(ddof=1) * np.sqrt(len(daily))) if len(daily) > 2 and daily.std() > 0 else 0.0
    yrs = max(len(all_days) / 248, 1e-9)
    years = {y: {"trades": int(len(g)), "avg_net_pct": round(float(g["net"].mean()) * 100, 3),
                 "win_rate": round(float((g["net"] > 0).mean()), 3)}
             for y, g in picks.groupby(picks["date"].str[:4])}
    return {
        "trades": int(len(picks)), "days_traded": int(len(daily)),
        "avg_net_pct": round(float(net.mean()) * 100, 3),
        "median_net_pct": round(float(net.median()) * 100, 3),
        "win_rate": round(float((net > 0).mean()), 3),
        "avg_gross_pct": round(float(picks["target_gross"].mean()) * 100, 3),
        "worst_trade_pct": round(float(net.min()) * 100, 2),
        "compound_return_pct": round(float(eq.iloc[-1] - 1) * 100, 2),
        "annual_return_pct": round(float(eq.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
        "max_drawdown_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 2),
        "t_stat": round(tstat, 2),
        "by_year": years,
    }


STRAT_NAMES = {
    "A_baseline_all_liquid": "A. 기준선: 유동성 조건 통과 전 종목",
    "B_rules_all": "B. 기술적 조건 통과 전 종목",
    "C_old_score_top10": "C. 예전 버전: 점수 가중합 상위 10",
    "D_old_own_pattern": "D. 직전 버전: 기술적 조건 + 과거패턴(3건↑·60%↑), 5종목",
    "E_live": "E. ★현재★ 기술적 조건 + 과거패턴(5건↑·60%↑) + 시장 20일선 위, 5종목",
}


def run(ds: pd.DataFrame, dev=DEV, test_start=TEST_START, k=None) -> dict:
    k = k or config.TOP_K
    all_days = sorted(ds["date"].unique())
    periods = {
        "dev": [d for d in all_days if dev[0] <= d <= dev[1]],
        "test": [d for d in all_days if d >= test_start],
    }
    periods["recent"] = all_days[-248:]
    m = masks(ds)
    strategies, stress = {}, {}
    for tag, days in periods.items():
        if not days:
            continue
        sel = ds["date"].isin(set(days))
        live = pick_live(ds[m["new"] & sel], k)
        strategies[tag] = {
            "A_baseline_all_liquid": portfolio_stats(ds[sel], days),
            "B_rules_all": portfolio_stats(ds[ds["rules"] & sel], days),
            "C_old_score_top10": portfolio_stats(pick_heuristic(ds[ds["rules"] & sel], 10), days),
            "D_old_own_pattern": portfolio_stats(pick_live(ds[m["old"] & sel], k), days),
            "E_live": portfolio_stats(live, days),
        }
        stress[tag] = {}
        for extra in [0.001, 0.002]:
            st = portfolio_stats(live.assign(net=live["net"] - extra), days)
            stress[tag][f"+{extra*100:.1f}%"] = {kk: st.get(kk) for kk in ["avg_net_pct", "win_rate", "t_stat"]}

    def good(st):
        return st.get("trades", 0) >= 100 and st.get("avg_net_pct", -1) > 0 and st.get("t_stat", 0) >= 2.0

    t = strategies.get("test", {}).get("E_live", {})
    d = strategies.get("dev", {}).get("E_live", {})
    r = strategies.get("recent", {}).get("E_live", {})
    slim = lambda st: {kk: st.get(kk) for kk in ["trades", "avg_net_pct", "median_net_pct", "win_rate", "t_stat",
                                                  "max_drawdown_pct", "annual_return_pct"]}
    live_strategy = {
        "name": STRAT_NAMES["E_live"],
        "edge_confirmed": bool(good(t) and d.get("avg_net_pct", -1) > 0),
        "recent_warning": bool(r.get("trades", 0) >= 30 and r.get("avg_net_pct", 0) <= 0),
        "is": slim(d), "oos": slim(t), "recent": slim(r),
        "cost_stress": stress,
        "periods": {kk: [v[0], v[-1]] for kk, v in periods.items() if v},
    }
    now = data_fetcher.now_kst().isoformat()
    model = {"version": 2, "trained_at": now, "live_strategy": live_strategy}
    summary = {"generated_at": now, "live_strategy": live_strategy, "strategies": strategies}
    return {"model": model, "summary": summary}


def write_outputs(result: dict):
    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    with open(MODEL_PATH, "w", encoding="utf-8") as f:
        json.dump(_clean(result["model"]), f, ensure_ascii=False, indent=1, allow_nan=False)
    os.makedirs(os.path.dirname(SUMMARY_JSON), exist_ok=True)
    with open(SUMMARY_JSON, "w", encoding="utf-8") as f:
        json.dump(_clean(result["summary"]), f, ensure_ascii=False, indent=1, allow_nan=False)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(result["summary"]))
    logger.info("저장: %s, %s, %s", MODEL_PATH, SUMMARY_JSON, REPORT_HTML)


def render_html(s: dict) -> str:
    def row(name, st):
        if not st.get("trades"):
            return f"<tr><td>{name}</td><td colspan=7>거래 없음</td></tr>"
        cls = "pos" if (st["avg_net_pct"] > 0 and st["t_stat"] >= 2) else ("neg" if st["avg_net_pct"] <= 0 else "dim")
        return (f"<tr><td>{name}</td><td>{st['trades']}</td><td class={cls}>{st['avg_net_pct']:.3f}%</td>"
                f"<td>{st['win_rate']*100:.1f}%</td><td>{st['annual_return_pct']:.1f}%</td>"
                f"<td>{st['max_drawdown_pct']:.1f}%</td><td>{st['t_stat']:.2f}</td></tr>")
    ls = s["live_strategy"]
    pr = ls["periods"]
    titles = {"test": f"검증 기간 {pr['test'][0]}~{pr['test'][1]} (규칙 선정 때 보지 않은 기간) ← 이 표로 판단하세요",
              "recent": f"최근 1년 {pr['recent'][0]}~{pr['recent'][1]} (전략이 아직 통하는지 감시)",
              "dev": f"탐색 기간 {pr['dev'][0]}~{pr['dev'][1]} (규칙을 고른 기간, 참고용)"}
    tables = ""
    for tag in ["test", "recent", "dev"]:
        if tag not in s["strategies"]:
            continue
        rows = "".join(row(STRAT_NAMES[k], v) for k, v in s["strategies"][tag].items())
        tables += (f"<h2>{titles[tag]}</h2><div class=scroll><table><tr><th>전략</th><th>거래수</th><th>1회 평균 순수익</th>"
                   f"<th>승률</th><th>연수익(복리)</th><th>최대낙폭</th><th>t값</th></tr>{rows}</table></div>")
    ok = ls["edge_confirmed"] and not ls["recent_warning"]
    verdict = ("<b class=pos>현재 전략 유효 (검증 기간에서 통계적으로 유의한 플러스)</b>" if ok else
               "<b class=neg>⚠️ 유효성 경고 - " + ("최근 1년 평균이 마이너스" if ls["recent_warning"] else "검증 기간 유의성 부족")
               + " → 매수 보류 권고</b>")
    stress = "".join(f"<li class=note>매수가가 종가보다 {e} 비쌀 때 ({'검증' if tag == 'test' else '탐색' if tag == 'dev' else '최근1년'}): "
                     f"1회 평균 {v['avg_net_pct']}%, t={v['t_stat']}</li>"
                     for tag in ["dev", "test"] for e, v in ls["cost_stress"].get(tag, {}).items())
    return f"""<!DOCTYPE html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>백테스트 결과</title>
<style>body{{background:#0b0f14;color:#e7edf3;font-family:-apple-system,'Apple SD Gothic Neo','Malgun Gothic',sans-serif;padding:16px;font-size:14px}}
table{{border-collapse:collapse;min-width:640px;margin-bottom:24px;font-size:12px}}td:first-child{{min-width:240px}}td,th{{border:1px solid #232b36;padding:6px;text-align:right}}
td:first-child,th:first-child{{text-align:left}}.pos{{color:#2ecc71}}.neg{{color:#ff5c5c}}.dim{{color:#8b98a5}}h1{{font-size:18px}}h2{{font-size:14px;margin-top:24px}}
.note{{color:#8b98a5;font-size:12px;line-height:1.6}}div.scroll{{overflow-x:auto}}</style></head><body>
<h1>📈 종가매수 → 익일 시가매도 백테스트</h1>
<p class=note>연도별 실제 거래세 + 수수료·슬리피지 0.05% 차감 · 하루 최대 5종목 균등 · 생성 {s['generated_at'][:16]}</p>
<h2>결론</h2><p>{verdict}</p><ul>{stress}</ul>
<p class=note>판정 기준: 검증 기간 거래 100회 이상, 1회 평균 순수익 &gt; 0, t값 ≥ 2, 탐색 기간도 플러스. 초록색은 t≥2 인 경우만.</p>
{tables}
<p class=note>⚠️ 과거 성과는 미래 수익을 보장하지 않습니다. 매수가는 정규장 종가, 매도가는 다음날 09:00 시가로 가정했습니다.
실제 NXT 저녁 체결가가 종가보다 비싸면 수익이 위 '매수가가 비쌀 때' 수치처럼 줄어듭니다. NXT 미상장 종목도 포함돼 있습니다.</p></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--marcap-dir", default=None, help="marcap parquet 을 미리 받아둔 폴더")
    ap.add_argument("--start-year", type=int, default=2015, help="데이터 시작 연도 (3년 과거패턴 창 때문에 평가 시작보다 앞서야 함)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    panel, days = load_panel_marcap(a.start_year, None, a.marcap_dir)
    logger.info("데이터: %s ~ %s, %d행", days[0], days[-1], len(panel))
    ds = build_dataset(panel, days)
    del panel
    result = run(ds)
    write_outputs(result)
    s = result["summary"]
    for tag in ["dev", "test", "recent"]:
        print(f"\n=== {tag} ===")
        for kk, v in s["strategies"][tag].items():
            print(f"{STRAT_NAMES[kk]}: " + json.dumps({x: y for x, y in v.items() if x != 'by_year'}, ensure_ascii=False))
    ls = s["live_strategy"]
    print("\nedge_confirmed:", ls["edge_confirmed"], "| recent_warning:", ls["recent_warning"])


if __name__ == "__main__":
    main()
