# -*- coding: utf-8 -*-
"""
샌드박스 환경에서는 data.krx.co.kr / opendart.fss.or.kr / finance.naver.com 으로의
아웃바운드 접속이 조직 정책으로 차단되어 있어 실제 네트워크 호출을 테스트할 수 없다.
(curl 로도 CONNECT tunnel 403 확인됨)

그래서 pykrx/DART/Naver 가 실제로 반환하는 응답 스키마를 그대로 흉내 낸 목(mock) 데이터로
각 모듈의 계산 로직(이동평균, 거래량배수, 종가강도, 필터링, 점수화, 신호 생성 파이프라인)이
올바르게 동작하는지 검증한다.
"""

import os
import sys
import json
import shutil
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np

import config

# 테스트 전용 데이터 디렉터리로 교체 (실제 data/ 디렉터리를 건드리지 않기 위함)
TEST_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_data")
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
config.DATA_DIR = TEST_DATA_DIR
config.DART_API_KEY = "FAKE_KEY_FOR_TEST"

import data_fetcher  # noqa: E402
import news_fetcher  # noqa: E402
import pattern_matcher  # noqa: E402
import screener  # noqa: E402
import signal_generator  # noqa: E402

# signal_generator.WEB_DATA_DIR 는 실제 프로젝트의 docs/data 를 가리키므로(GitHub Pages 배포용),
# 테스트에서 그대로 두면 진짜 docs/data 를 가짜 데이터로 오염시킨다. 반드시 테스트 전용 경로로 교체.
TEST_WEB_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_docs_data")
shutil.rmtree(TEST_WEB_DATA_DIR, ignore_errors=True)
signal_generator.WEB_DATA_DIR = TEST_WEB_DATA_DIR

# ---------------------------------------------------------------------------
# 1) get_trading_days() 로직 검증 - pykrx get_index_ohlcv_by_date 목킹
# ---------------------------------------------------------------------------
def fake_index_ohlcv(start, end, code):
    # 2026-08-01 ~ 2026-09-18 사이의 평일만 거래일로 취급 (공휴일 무시, 검증 목적으로는 충분)
    idx = pd.bdate_range(start=datetime.strptime(start, "%Y%m%d"),
                          end=datetime.strptime(end, "%Y%m%d"))
    return pd.DataFrame({"종가": 3000}, index=idx)


with mock.patch("data_fetcher.stock.get_index_ohlcv_by_date", side_effect=fake_index_ohlcv):
    days = data_fetcher.get_trading_days("20260918", 25)
    assert len(days) == 25, f"expected 25 trading days, got {len(days)}"
    assert days[-1] == "20260918", days[-1]
    assert days == sorted(days)
    print("[PASS] get_trading_days: 25 trading days ending 20260918 ->", days[0], "~", days[-1])


# ---------------------------------------------------------------------------
# 2) build_snapshot_with_indicators() 계산 로직 검증
#    - 종목 A: 상승추세 + 거래량 급증 + 종가강도 높음 -> 필터 통과해야 함
#    - 종목 B: 등락률 낮음 -> 필터에서 걸러져야 함
#    - 종목 C: 거래량배수 낮음 -> 필터에서 걸러져야 함
#    - 종목 D: 정배열 아님(5MA < 20MA) -> 필터에서 걸러져야 함
# ---------------------------------------------------------------------------
trading_days = pd.bdate_range(end="2026-09-18", periods=25)
dates_str = [d.strftime("%Y%m%d") for d in trading_days]

def make_history(ticker, closes_24, last_day_change_pct, last_day_vol_mult, close_strength):
    """closes_24: 과거 24거래일 종가 리스트 (오늘 제외). 오늘 종가는 closes_24[-1] 대비
    last_day_change_pct 만큼 변동한 값으로 결정론적으로 계산한다."""
    rows = []
    base_vol = 500_000
    for i, (d, c) in enumerate(zip(dates_str[:-1], closes_24)):
        vol = base_vol  # 과거 거래량은 고정값 -> 평균거래량20 계산이 결정론적이 되도록
        rows.append({
            "티커": ticker, "날짜": d, "시가": c * 0.99, "고가": c * 1.02,
            "저가": c * 0.98, "종가": c, "거래량": vol,
            "거래대금": c * vol, "등락률": 0.0, "시장": "KOSPI",
        })
    today_close = closes_24[-1] * (1 + last_day_change_pct / 100.0)
    today_vol = base_vol * last_day_vol_mult
    day_low = today_close * 0.95
    day_high = day_low + (today_close - day_low) / close_strength if close_strength > 0 else today_close
    rows.append({
        "티커": ticker, "날짜": dates_str[-1], "시가": today_close * 0.97, "고가": day_high,
        "저가": day_low, "종가": today_close, "거래량": today_vol,
        "거래대금": today_close * today_vol, "등락률": last_day_change_pct, "시장": "KOSPI",
    })
    return rows


np.random.seed(42)
all_rows = []
# A: 과거 24일 완만한 상승(정배열 성립) + 오늘 +8% 급등 + 거래량 3.5배 + 종가강도 0.9 -> 전부 통과 예상
closes_up = [10000 * (1.003 ** i) for i in range(24)]
all_rows += make_history("000001", closes_up, last_day_change_pct=8.0, last_day_vol_mult=3.5,
                          close_strength=0.9)
# B: A와 동일 추세이나 오늘 등락률만 +1% -> 등락률 필터 미달로 탈락
all_rows += make_history("000002", closes_up, last_day_change_pct=1.0, last_day_vol_mult=3.5,
                          close_strength=0.9)
# C: A와 동일 추세/등락률이나 거래량배수만 1.2배 -> 거래량배수 필터 미달로 탈락
all_rows += make_history("000003", closes_up, last_day_change_pct=8.0, last_day_vol_mult=1.2,
                          close_strength=0.9)
# D: 최근 5일은 하락 추세(MA5 < MA20이 되도록 직전 5일을 낮게), 오늘 +8% 급등이 섞여도
#    5일 평균이 20일 평균보다 낮게 유지되도록 설계 -> MA정배열 실패로 탈락해야 함
closes_down = [10000 * (1.006 ** i) for i in range(19)]  # 0~18일: 완만한 상승으로 MA20을 높게 유지
closes_down += [closes_down[-1] * (0.985 ** j) for j in range(1, 6)]  # 19~23일: 급격한 하락 5일
all_rows += make_history("000004", closes_down, last_day_change_pct=8.0, last_day_vol_mult=3.5,
                          close_strength=0.9)

hist_df = pd.DataFrame(all_rows)

def fake_fetch_one_day(date, markets=None):
    sub = hist_df[hist_df["날짜"] == date]
    return sub.reset_index(drop=True)

def fake_market_cap_snapshot(date, markets=None):
    tickers = hist_df["티커"].unique()
    return pd.DataFrame({
        "티커": tickers,
        "시가총액": [200_000_000_000] * len(tickers),  # 2000억 (필터 통과하도록 충분히 크게)
        "상장주식수": [10_000_000] * len(tickers),
        "시장": ["KOSPI"] * len(tickers),
    })

def fake_ticker_name(t):
    return {"000001": "가상전자", "000002": "가상바이오",
            "000003": "가상화학", "000004": "가상건설"}.get(t, t)

with mock.patch("data_fetcher.get_trading_days", new=lambda *a, **k: dates_str), \
     mock.patch("data_fetcher._fetch_ohlcv_one_day", new=fake_fetch_one_day), \
     mock.patch("data_fetcher.get_market_cap_snapshot", new=fake_market_cap_snapshot), \
     mock.patch("data_fetcher.get_ticker_name", new=fake_ticker_name):
    snap = data_fetcher.build_snapshot_with_indicators("20260918")

assert len(snap) == 4, f"expected 4 tickers in snapshot, got {len(snap)}"
row_a = snap[snap["티커"] == "000001"].iloc[0]
assert row_a["MA정배열"] == True, "종목A는 정배열이어야 함"
assert row_a["거래량배수"] > 3.0, row_a["거래량배수"]
assert 0.85 < row_a["종가강도"] < 0.95, row_a["종가강도"]
print("[PASS] build_snapshot_with_indicators: MA5/MA20/거래량배수/종가강도 계산 정상")
print(snap[["티커", "종가", "등락률", "거래량배수", "종가강도", "MA5", "MA20", "MA정배열"]].to_string(index=False))

import features as F  # noqa: E402

hist_with_cap = hist_df.assign(시가총액=200_000_000_000)
fdf = F.compute_features(screener.to_panel(hist_with_cap), dates_str)
last = fdf[fdf["date"] == dates_str[-1]].set_index("ticker")
# 기존 스냅샷 계산과 features.py 계산이 같은 값을 내는지 (백테스트-실시간 일치 확인)
for tk in ["000001", "000004"]:
    s_row = snap.set_index("티커").loc[tk]
    assert abs(last.loc[tk, "vol_ratio"] - s_row["거래량배수"]) < 1e-9
    assert abs(last.loc[tk, "close_strength"] - s_row["종가강도"]) < 1e-9
    assert bool(last.loc[tk, "ma_align"]) == bool(s_row["MA정배열"])
passed = set(last.index[F.rules_mask(last, config)])
assert passed == {"000001"}, f"필터 결과가 예상과 다름: {passed}"
print("[PASS] features.rules_mask: 종목A만 통과 (B/C/D 탈락), 기존 지표 계산과 값 일치")


# ---------------------------------------------------------------------------
# 3) 뉴스/공시 점수화 로직 검증
# ---------------------------------------------------------------------------
fake_disclosures = [
    {"stock_code": "000001", "report_nm": "단일판매공급계약체결", "corp_name": "가상전자"},
    {"stock_code": "000001", "report_nm": "무상증자 결정", "corp_name": "가상전자"},
    {"stock_code": "000002", "report_nm": "유상증자 결정", "corp_name": "가상바이오"},
]
scores = news_fetcher.score_disclosures(fake_disclosures, ["000001", "000002", "000003"])
assert scores["000001"]["score"] == 2.0, scores["000001"]  # 공급계약(+1) + 무상증자(+1)
assert scores["000002"]["score"] == -1.5, scores["000002"]  # 유상증자(-1.5)
assert scores["000003"]["score"] == 0.0
print("[PASS] score_disclosures: 호재(+1×2=2.0) / 악재(-1.5) 키워드 점수화 정상")

with mock.patch("news_fetcher.fetch_naver_news_titles", return_value=["가상전자, 대규모 수주에 신고가 경신"]):
    r = news_fetcher.score_news_for_ticker("000001")
    assert r["score"] == 0.5, r
    print("[PASS] score_news_for_ticker: 긍정 키워드(+0.5) 정상 반영")


# ---------------------------------------------------------------------------
# 3-1) 과거 패턴 매칭 로직 검증
#      최근 3거래일(오늘 포함) = "완만 -> 완만 -> +8% 급등" 패턴을 만들고,
#      같은 종목의 과거 이력 중 동일 패턴이 3번(2번은 익일 시가 상승, 1번은 하락) 나오도록
#      결정론적으로 데이터를 구성한 뒤, evaluate_pattern() 이 정확히 매칭수 3 / 승률 2/3 을
#      계산하는지, 그리고 패턴확인(True) 판정이 올바른지 확인한다.
# ---------------------------------------------------------------------------
def _build_pattern_history(end_date_str: str) -> pd.DataFrame:
    rows = []  # (open, close)
    price = 10000.0

    def add_flat(n):
        nonlocal price
        for _ in range(n):
            rows.append((price, price))

    def add_pattern_occurrence(next_day_up: bool):
        nonlocal price
        rows.append((price, price))              # day1: 완만(보합)
        rows.append((price, price))               # day2: 완만(보합)
        o3, c3 = price, price * 1.08               # day3: +8% 급등
        rows.append((o3, c3))
        price = c3
        o_next = price * (1.03 if next_day_up else 0.98)
        rows.append((o_next, o_next))              # 익일: 시가=종가(보합), 상승/하락 여부만 반영
        price = o_next

    add_flat(50)                       # 최소 이력일수(60일) 확보용 패딩
    add_pattern_occurrence(True)       # 과거 사례 1: 익일 시가 상승
    add_flat(3)
    add_pattern_occurrence(True)       # 과거 사례 2: 익일 시가 상승
    add_flat(3)
    add_pattern_occurrence(False)      # 과거 사례 3: 익일 시가 하락
    add_flat(3)
    # 오늘(=마지막 3일) : 과거 사례와 동일한 패턴, 다음날 데이터는 아직 없음(오늘이 마지막 행)
    rows.append((price, price))
    rows.append((price, price))
    rows.append((price, price * 1.08))

    dates = pd.bdate_range(end=datetime.strptime(end_date_str, "%Y%m%d"), periods=len(rows))
    df = pd.DataFrame(rows, columns=["시가", "종가"], index=dates)
    df["고가"] = df[["시가", "종가"]].max(axis=1) * 1.001
    df["저가"] = df[["시가", "종가"]].min(axis=1) * 0.999
    df.index.name = "날짜"
    return df


pattern_end_date = "20260918"
pattern_hist = _build_pattern_history(pattern_end_date)
assert len(pattern_hist) >= config.PATTERN_MIN_HISTORY_DAYS, len(pattern_hist)

# 패턴 벡터 단위 검증: 오늘(마지막 3행)의 벡터가 [0,0,0,0,0.08] 이어야 함
pf = F.compute_features(F.history_to_panel(pattern_hist))
today_vec = pf[["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]].iloc[-1].values.astype(float)
np.testing.assert_allclose(today_vec, [0, 0, 0, 0, 0.08], atol=1e-9)
print("[PASS] 패턴 벡터: 오늘 패턴 [0,0,0,0,0.08] 정확히 계산됨")

# 테스트에서는 거래일 달력(KRX 조회)을 쓰지 않음
pattern_matcher._trading_days = lambda end_date: None

with mock.patch("data_fetcher.get_ticker_ohlcv_history", return_value=pattern_hist):
    presult = pattern_matcher.evaluate_pattern("000099", pattern_end_date)

# 실시간(evaluate_pattern) 결과 == 백테스트(per_stock_pattern_stats) 결과 여야 함
bc, bw = F.per_stock_pattern_stats(pf[["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]].values, pf["next_up"].values)
assert (bc[-1], bw[-1]) == (presult["패턴매칭수"], presult["패턴상승수"]), (bc[-1], bw[-1], presult)
print("[PASS] 실시간 패턴 판정 == 백테스트 패턴 판정 (정의 일치)")

assert presult["패턴매칭수"] == 3 and presult["패턴상승수"] == 2, presult
assert abs(presult["패턴승률"] - (2 / 3)) < 1e-3, presult
# 현재 기준(사례 5건 이상)에서는 3건짜리는 근거 부족 -> 패턴확인 False
assert config.MIN_PATTERN_MATCHES == 5
assert presult["패턴확인"] is False, presult
print("[PASS] evaluate_pattern: 유사패턴 3건(상승2) 검출, 현재 기준(5건↑) 미달 -> 패턴확인=False")
print("       상위 근접 사례:", presult["패턴사례"])

# 최소 건수 기준을 3건으로 낮추면 같은 데이터가 패턴확인=True 가 되어야 함
with mock.patch.object(config, "MIN_PATTERN_MATCHES", 3), \
     mock.patch("data_fetcher.get_ticker_ohlcv_history", return_value=pattern_hist):
    presult_loose = pattern_matcher.evaluate_pattern("000099", pattern_end_date)
assert presult_loose["패턴확인"] is True
print("[PASS] evaluate_pattern: 최소 건수 기준 3건이면 같은 데이터 패턴확인=True (기준값이 판정에 반영됨)")

# 이력이 짧은(최근 상장 등) 종목은 매칭수 0 / 패턴확인 False 로 안전하게 처리되는지 확인
short_hist = pattern_hist.iloc[-10:]
with mock.patch("data_fetcher.get_ticker_ohlcv_history", return_value=short_hist):
    presult_short = pattern_matcher.evaluate_pattern("000099", pattern_end_date)
assert presult_short["패턴매칭수"] == 0 and presult_short["패턴확인"] is False
print("[PASS] evaluate_pattern: 이력 부족 종목은 매칭수 0 / 패턴확인 False 로 안전 처리")


# ---------------------------------------------------------------------------
# 3-2) screener.run_screen() 통합 검증 - 기술적 조건 + 패턴확인 + 악재공시 제외 -> 추천 여부
# ---------------------------------------------------------------------------
def run_screen_with(pattern_ok: bool, disclosure_score: float, hist_override=None, market_up=True):
    h = hist_with_cap if hist_override is None else hist_override
    trend = pd.Series({dates_str[-1]: market_up})
    with mock.patch("data_fetcher.get_market_history", return_value=h), \
         mock.patch.object(F, "market_trend", return_value=trend), \
         mock.patch("data_fetcher.get_ticker_name", new=fake_ticker_name), \
         mock.patch.object(news_fetcher, "build_news_scores", return_value={"000001": {
             "news_score": disclosure_score, "disclosure_score": disclosure_score,
             "disclosure_hits": ["유상증자"] if disclosure_score < 0 else [], "news_hits": []}}), \
         mock.patch.object(pattern_matcher, "build_pattern_scores", return_value={"000001": {
             "패턴매칭수": 5, "패턴상승수": 4 if pattern_ok else 1, "패턴승률": 0.8 if pattern_ok else 0.2,
             "패턴확인": pattern_ok, "패턴사례": []}}):
        return screener.run_screen(dates_str[-1])

r = run_screen_with(True, 0.0)
assert list(r["티커"]) == ["000001"] and bool(r.loc[0, "추천"]) is True, r
r = run_screen_with(False, 0.0)
assert bool(r.loc[0, "추천"]) is False
r = run_screen_with(True, -1.5)
assert bool(r.loc[0, "추천"]) is False and bool(r.loc[0, "악재공시"]) is True
r = run_screen_with(True, 0.0, market_up=False)
assert bool(r.loc[0, "추천"]) is False and bool(r.loc[0, "시장추세"]) is False
print("[PASS] screener.run_screen: 조건+패턴확인+시장상승 -> 추천 / 패턴 미확인·악재공시·시장 20일선 아래 -> 제외")

# 실제 시장추세 계산이 스크리너 경로에서 동작하는지 (목킹 없이): 합성 데이터는 상승 추세
real_trend = F.market_trend(screener.to_panel(hist_with_cap))
assert bool(real_trend.loc[dates_str[-1]]) is True
print("[PASS] 시장추세 계산 (합성 상승장 -> 20일선 위)")

# 상한가(+29% 이상) 마감 종목은 NXT 저녁에 체결이 거의 불가능하므로 후보에서 제외돼야 함
hist_lu = hist_with_cap.copy()
lu = (hist_lu["티커"] == "000001") & (hist_lu["날짜"] == dates_str[-1])
prev_close = hist_lu[(hist_lu["티커"] == "000001") & (hist_lu["날짜"] == dates_str[-2])]["종가"].iloc[0]
hist_lu.loc[lu, ["종가", "고가"]] = prev_close * 1.2995
hist_lu.loc[lu, "등락률"] = 29.95
r = run_screen_with(True, 0.0, hist_lu)
assert r.empty or "000001" not in set(r["티커"]), r
print("[PASS] 상한가 마감 종목은 체결 불가로 후보 제외")


# ---------------------------------------------------------------------------
# 4) 전체 파이프라인 (screen -> buy -> sell) 저장/로딩 흐름 검증
# ---------------------------------------------------------------------------
fake_screen_result = pd.DataFrame([{
    "티커": "000001", "종목명": "가상전자", "시장": "KOSPI", "날짜": "20260918",
    "종가": 13000, "등락률": 8.0, "거래량배수": 3.5, "종가강도": 0.9, "MA정배열": True,
    "거래대금": 5_000_000_000, "시가총액": 200_000_000_000,
    "패턴매칭수": 5, "패턴상승수": 4, "패턴승률": 0.8, "패턴확인": True, "패턴사례": [],
    "악재공시": False, "시장추세": True, "추천": True,
    "공시히트": ["단일판매공급계약체결"], "뉴스히트": [],
}, {
    "티커": "000002", "종목명": "가상바이오", "시장": "KOSDAQ", "날짜": "20260918",
    "종가": 5000, "등락률": 5.0, "거래량배수": 2.5, "종가강도": 0.8, "MA정배열": True,
    "거래대금": 4_000_000_000, "시가총액": 100_000_000_000,
    "패턴매칭수": 1, "패턴상승수": 0, "패턴승률": 0.0, "패턴확인": False, "패턴사례": [],
    "악재공시": False, "시장추세": True, "추천": False, "공시히트": [], "뉴스히트": [],
}])

with mock.patch("screener.run_screen", return_value=fake_screen_result):
    p1 = signal_generator.run_screen_and_save("20260918")
    d1 = json.load(open(p1, encoding="utf-8"))
    assert d1["stage"] == "screen_15_30"
    assert len(d1["candidates"]) == 2
    print("[PASS] run_screen_and_save 저장 정상:", p1)

p2 = signal_generator.generate_buy_signals("20260918")
d2 = json.load(open(p2, encoding="utf-8"))
assert [b["티커"] for b in d2["buy_candidates"]] == ["000001"], "추천=True 종목만 매수 신호에 들어가야 함"
assert d2["buy_candidates"][0]["참고매수가"] == 13000
assert d2["buy_candidates"][0]["매수상한가"] == int(13000 * (1 + config.MAX_BUY_PREMIUM))
assert d2["buy_candidates"][0]["패턴확인"] is True
print("[PASS] generate_buy_signals: 추천 종목만 포함, 매수상한가 계산 정상:", p2)

# 추천 종목이 하나도 없는 날 -> 빈 매수 리스트 + '매수하지 않는 날' 안내
with mock.patch("screener.run_screen", return_value=fake_screen_result.assign(추천=False)):
    signal_generator.run_screen_and_save("20260917")
d_empty = json.load(open(signal_generator.generate_buy_signals("20260917"), encoding="utf-8"))
assert d_empty["buy_candidates"] == [] and "매수하지 않는" in d_empty["note"]
import telegram_notifier  # noqa: E402
assert "매수하지 않습니다" in telegram_notifier.format_buy_message(d_empty)
print("[PASS] 검증조건 통과 종목이 없으면 '매수하지 않는 날'로 안내")

# 시장지수가 20일선 아래인 날 -> 시장 사유로 매수 안 함 안내
with mock.patch("screener.run_screen", return_value=fake_screen_result.assign(추천=False, 시장추세=False)):
    ps = signal_generator.run_screen_and_save("20260916")
assert json.load(open(ps, encoding="utf-8"))["market_up"] is False
d_mkt = json.load(open(signal_generator.generate_buy_signals("20260916"), encoding="utf-8"))
assert d_mkt["buy_candidates"] == [] and "20일 이동평균 아래" in d_mkt["note"]
assert "20일선 아래" in telegram_notifier.format_buy_message(d_mkt)
print("[PASS] 시장 20일선 아래인 날은 '시장 사유로 매수 안 함' 안내")

with mock.patch("news_fetcher.fetch_dart_disclosures", return_value=[
    {"stock_code": "000001", "report_nm": "횡령 배임 혐의 발생", "corp_name": "가상전자"},
]):
    p3 = signal_generator.generate_sell_signals("20260918", "20260919")
d3 = json.load(open(p3, encoding="utf-8"))
sell_item = d3["sell_candidates"][0]
assert sell_item["우선매도권고"] is True, "횡령/배임 악재 공시가 있으면 우선매도권고=True 여야 함"
assert sell_item["익일공시점수"] == -1.5
print("[PASS] generate_sell_signals 저장 정상 (악재 공시 -> 우선매도권고 True):", p3)


# ---------------------------------------------------------------------------
# 5) GitHub Pages 정적 배포용 docs/data/latest_*.json 발행 검증
#    (반드시 테스트 전용 WEB_DATA_DIR 에만 쓰이고, 실제 프로젝트 docs/data 는 건드리지 않아야 함)
# ---------------------------------------------------------------------------
assert signal_generator.WEB_DATA_DIR == TEST_WEB_DATA_DIR, "테스트가 실제 docs/data 를 오염시킬 뻔했습니다!"
real_docs_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")

for kind, expect_key in [("screen", "candidates"), ("buy", "buy_candidates"), ("sell", "sell_candidates")]:
    web_path = os.path.join(TEST_WEB_DATA_DIR, f"latest_{kind}.json")
    assert os.path.exists(web_path), f"{web_path} 가 생성되지 않았습니다"
    web_data = json.load(open(web_path, encoding="utf-8"))
    assert expect_key in web_data
print("[PASS] docs/data/latest_*.json 발행 정상 (테스트 전용 디렉터리에만 기록됨)")

# 실제 프로젝트 docs/data 에는 .gitkeep 외에 아무 파일도 생기지 않았어야 함
if os.path.isdir(real_docs_data):
    leaked = [f for f in os.listdir(real_docs_data) if f not in (".gitkeep", "backtest_summary.json")]
    assert not leaked, f"실제 docs/data 가 테스트로 오염되었습니다: {leaked}"
print("[PASS] 실제 프로젝트 docs/data 디렉터리는 오염되지 않음")


# ---------------------------------------------------------------------------
# 6) KST 타임존 처리 검증 - 서버 시스템 시각이 UTC여도 날짜 계산이 한국시각 기준이어야 함
#    (특히 08:30 KST 매도 신호는 UTC 기준으로는 전날 밤이라, 이 처리가 틀리면 날짜가 하루 밀린다)
# ---------------------------------------------------------------------------
now = data_fetcher.now_kst()
assert now.utcoffset().total_seconds() == 9 * 3600, "KST는 UTC+9 여야 함"
print(f"[PASS] data_fetcher.now_kst() 가 UTC+9 로 정상 반환됨 (현재: {now.isoformat()})")

print("\n=== 모든 로직 검증 통과 ===")
