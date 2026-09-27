# -*- coding: utf-8 -*-
"""
백테스트 엔진 검증 (합성 데이터, 네트워크 불필요).

1) 미래정보 누수 검사: 종목별 패턴 통계가 과거 사례만 쓰는지
2) 3년 창 방식(windowed_pattern_stats) == 전체이력 방식(per_stock_pattern_stats) (창이 충분히 길 때)
3) 액면분할 등 가짜 갭 제거
4) 시장 추세 지표가 당일까지의 정보만 쓰는지
5) "숨겨둔 수익 규칙"이 있는 가짜 시장 -> 전략이 플러스, 유효 판정
6) 규칙이 전혀 없는(무작위) 가짜 시장 -> 비용 때문에 마이너스, 유효 판정 안 함
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd

import backtest
import config
import features as F

# 1) 누수 검사
V = np.array([[0, 0, 0, 0, .08]] * 5, float)
up = np.array([1, 0, 1, 1, np.nan])
c, w = F.per_stock_pattern_stats(V, up)
assert list(c) == [0, 1, 2, 3, 4] and list(w) == [0, 1, 1, 2, 3], (c, w)
print("[PASS] 종목별 패턴 통계: 당일 이후 정보 미사용 (누수 없음)")


def make_market(n_tickers=160, n_days=600, edge=0.0, seed=0, split_day=None):
    rng = np.random.default_rng(seed)
    days = [d.strftime("%Y%m%d") for d in pd.bdate_range("2021-01-04", periods=n_days)]
    rows = []
    for t in range(n_tickers):
        tk, mkt = f"{t:06d}", ("KOSPI" if t % 2 else "KOSDAQ")
        shares = rng.uniform(2e7, 2e8)
        prev_close, prev_signal = rng.uniform(5000, 50000), False
        for i, d in enumerate(days):
            gap = rng.normal(0.0, 0.008) + (edge if prev_signal else 0.0)
            if split_day is not None and t == 0 and i == split_day:
                prev_close /= 2
            o = prev_close * (1 + gap)
            c = o * (1 + rng.normal(0.004, 0.035))
            h = max(o, c) * (1 + abs(rng.normal(0, 0.006)))
            l = min(o, c) * (1 - abs(rng.normal(0, 0.006)))
            chg = (c / prev_close - 1) * 100
            vol = rng.lognormal(12, 0.3) * (3.0 if chg > 3 else 1.0)
            strength = (c - l) / (h - l) if h > l else 1
            prev_signal = chg > 3 and strength > 0.8   # 숨겨둔 규칙: 3%+ 강세 마감 다음날 갭상승
            rows.append((tk, d, mkt, o, h, l, c, vol, c * vol, chg, c * shares))
            prev_close = c
    cols = ["ticker", "date", "market", "open", "high", "low", "close", "volume", "value", "chg", "cap"]
    return pd.DataFrame(rows, columns=cols), days


# 2) 3년 창 방식 == 전체이력 방식 (합성 데이터는 3년보다 짧으므로 두 결과가 같아야 함)
panel, days = make_market(n_tickers=4, n_days=300, seed=5)
f = F.compute_features(panel, days)
tgt = f[["ticker", "date"]].copy()
wst = F.windowed_pattern_stats(f, tgt)
for tk, g in f.groupby("ticker"):
    cnt, wins = F.per_stock_pattern_stats(g[["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]].values, g["next_up"].values)
    sub = wst.loc[g.index]
    enough = np.arange(len(g)) + 1 >= config.PATTERN_MIN_HISTORY_DAYS   # 실시간과 같은 최소 이력 조건
    assert (sub["ps_count"].values[enough] == cnt[enough]).all()
    assert (sub["ps_wins"].values[enough] == wins[enough]).all()
print("[PASS] 3년 창 방식 과거패턴 통계 == 전체이력 방식 (창 안에서는 동일)")

# 3) 액면분할 가짜 갭 제거
panel, days = make_market(n_tickers=3, n_days=60, split_day=40)
df = F.compute_features(panel, days)
row = df[(df["ticker"] == "000000") & (df["date"] == days[39])].iloc[0]
assert not row["target_valid"]
assert df.loc[df["target_valid"], "target_gross"].abs().max() < 0.2
print("[PASS] 액면분할 등 기준가 변경일의 가짜 갭 자동 제외")

# 4) 시장 추세: 마지막 날 데이터를 바꿔도 그 이전 날짜 판정은 변하지 않아야 함 (미래정보 미사용)
panel, days = make_market(n_tickers=10, n_days=80, seed=9)
t1 = F.market_trend(panel)
p2 = panel.copy()
p2.loc[p2["date"] == days[-1], "chg"] = -50.0
t2 = F.market_trend(p2)
assert t1.loc[days[:-1]].equals(t2.loc[days[:-1]]) and bool(t2.loc[days[-1]]) is False
print("[PASS] 시장 추세 판정은 당일까지 정보만 사용")

# 합성 데이터 가격대에 맞게 유동성 조건 완화
config.MIN_TRADING_VALUE = config.MIN_MARKET_CAP = config.MIN_PRICE = 0
config.MAX_PRICE = 10**9


def run_market(edge, seed):
    panel, days = make_market(edge=edge, seed=seed)
    ds = backtest.build_dataset(panel, days, eval_start=days[60])
    return backtest.run(ds, dev=(days[60], days[350]), test_start=days[351])


# 5) 숨겨둔 수익 규칙이 있는 시장
res = run_market(0.012, 1)
for tag in ["dev", "test"]:
    v = res["summary"]["strategies"][tag]["E_live"]
    print(f"   [edge 시장 {tag}] 거래 {v.get('trades')} / 1회 평균 {v.get('avg_net_pct')}% / t={v.get('t_stat')}")
t = res["summary"]["strategies"]["test"]
assert t["E_live"]["avg_net_pct"] > t["A_baseline_all_liquid"]["avg_net_pct"]
assert t["E_live"]["avg_net_pct"] > 0.3
assert res["model"]["live_strategy"]["edge_confirmed"] is True
print("[PASS] 숨겨진 수익 규칙을 찾아내고 유효 판정")

# 6) 규칙 없는 무작위 시장
res0 = run_market(0.0, 2)
for tag in ["dev", "test"]:
    v = res0["summary"]["strategies"][tag]["E_live"]
    print(f"   [무작위 시장 {tag}] 거래 {v.get('trades')} / 1회 평균 {v.get('avg_net_pct')}% / t={v.get('t_stat')}")
assert res0["summary"]["strategies"]["test"]["A_baseline_all_liquid"]["avg_net_pct"] < 0
assert res0["model"]["live_strategy"]["edge_confirmed"] is False
print("[PASS] 규칙 없는 시장에서는 유효 판정 안 함 (가짜 수익 채택 안 함)")

html = backtest.render_html(res["summary"])
assert "검증 기간" in html and "E. ★현재★" in html
print("[PASS] 리포트 HTML 생성")
print("\n=== 백테스트 엔진 검증 통과 ===")
