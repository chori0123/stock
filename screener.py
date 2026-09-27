# -*- coding: utf-8 -*-
"""
스크리닝 엔진 (11년치 실제 데이터 백테스트로 검증된 규칙).

추천 = 아래를 모두 만족
  1) 기술적 조건: 등락률 3~29.5%, 거래량 20일평균 2배↑, 종가강도 0.65↑, 5일선>20일선, 유동성
     (상한가 마감·상장 직후 종목 제외 - 종가/NXT 저녁에 사실상 체결 불가)
  2) 종목 자체 과거패턴: 최근 3년에 같은 3일 패턴이 5건 이상, 다음날 시가 상승 60% 이상
  3) 시장 추세: 시장지수(시총가중)가 20일 이동평균 위
  4) DART 악재공시 없음

백테스트 요약 (비용 차감, 하루 최대 5종목, 다음날 시가 매도):
  - 기존 점수 가중합 방식: 통계적으로 의미 있는 수익 없음
  - 2016~2022 (규칙 탐색 기간)   1회 평균 +0.21%, t=4.8, 최대낙폭 -10%
  - 2023~2026 (처음 보는 검증 기간) 1회 평균 +0.47%, t=5.1, 최대낙폭 -16%
과거 성과가 미래 수익을 보장하지 않는다.
"""

import logging

import numpy as np
import pandas as pd

import config
import data_fetcher
import features as F
import news_fetcher
import pattern_matcher

logger = logging.getLogger(__name__)

KR2EN = {"티커": "ticker", "날짜": "date", "시장": "market", "시가": "open", "고가": "high", "저가": "low",
         "종가": "close", "거래량": "volume", "거래대금": "value", "등락률": "chg", "시가총액": "cap"}


def to_panel(hist: pd.DataFrame) -> pd.DataFrame:
    """data_fetcher.get_market_history() 결과(한국어 컬럼) -> features 입력 형식."""
    p = hist.rename(columns=KR2EN).copy()
    if "cap" not in p.columns:
        p["cap"] = np.nan
    p["date"] = p["date"].astype(str)
    p["ticker"] = p["ticker"].astype(str).str.zfill(6)
    return p[["ticker", "date", "market", "open", "high", "low", "close", "volume", "value", "chg", "cap"]]


def run_screen(end_date: str = None) -> pd.DataFrame:
    end_date = end_date or data_fetcher.get_latest_trading_day()
    hist = data_fetcher.get_market_history(end_date)
    if hist.empty:
        logger.warning("시세 이력이 비어 있습니다.")
        return pd.DataFrame()
    panel = to_panel(hist)
    if panel["cap"].isna().all():  # 구버전 pykrx 대비: 당일 시가총액만 별도 조회
        cap = data_fetcher.get_market_cap_snapshot(end_date)
        if not cap.empty:
            m = cap.set_index("티커")["시가총액"]
            today = panel["date"] == end_date
            panel.loc[today, "cap"] = panel.loc[today, "ticker"].map(m)

    df = F.compute_features(panel, sorted(panel["date"].unique()))
    trend = F.market_trend(panel)
    market_up = bool(trend.get(end_date)) if pd.notna(trend.get(end_date)) else False
    logger.info("시장지수 %d일선 위: %s", config.MARKET_MA_DAYS, market_up)
    today = df[df["date"] == end_date].copy()
    cand = today[F.rules_mask(today, config)].copy()
    logger.info("기술적 조건 통과: %d개 (전체 %d개 중)", len(cand), len(today))
    if cand.empty:
        return pd.DataFrame()

    tickers = cand["ticker"].tolist()
    pat = pattern_matcher.build_pattern_scores(tickers, end_date)
    for k in ["패턴매칭수", "패턴상승수", "패턴승률", "패턴확인", "패턴사례"]:
        cand[k] = cand["ticker"].map(lambda t: pat.get(t, {}).get(k))
    cand["패턴확인"] = cand["패턴확인"].fillna(False).astype(bool)

    news = news_fetcher.build_news_scores(tickers, end_date)
    cand["공시히트"] = cand["ticker"].map(lambda t: news.get(t, {}).get("disclosure_hits", []))
    cand["뉴스히트"] = cand["ticker"].map(lambda t: news.get(t, {}).get("news_hits", []))
    cand["악재공시"] = cand["ticker"].map(lambda t: news.get(t, {}).get("disclosure_score", 0.0) < 0)

    cand["시장추세"] = market_up
    cand["추천"] = cand["패턴확인"] & ~cand["악재공시"] & (market_up or not config.MARKET_TREND_FILTER)
    # 표본이 적은 100% 승률을 과대평가하지 않도록 (승+2)/(건수+4) 로 축소한 값으로 정렬 (표시용)
    cand["_wr"] = (cand["패턴상승수"].fillna(0) + 2) / (cand["패턴매칭수"].fillna(0) + 4)
    cand = cand.sort_values(["추천", "_wr", "vol_ratio"], ascending=[False, False, False])

    cand["종목명"] = cand["ticker"].apply(data_fetcher.get_ticker_name)
    out = pd.DataFrame({
        "티커": cand["ticker"], "종목명": cand["종목명"], "시장": cand["market"], "날짜": cand["date"],
        "종가": cand["close"], "등락률": cand["chg"].round(2), "거래량배수": cand["vol_ratio"].round(2),
        "종가강도": cand["close_strength"].round(3), "MA정배열": cand["ma_align"] == 1,
        "거래대금": cand["value"], "시가총액": cand["cap"],
        "패턴매칭수": cand["패턴매칭수"], "패턴상승수": cand["패턴상승수"], "패턴승률": cand["패턴승률"],
        "패턴확인": cand["패턴확인"], "패턴사례": cand["패턴사례"],
        "악재공시": cand["악재공시"], "공시히트": cand["공시히트"], "뉴스히트": cand["뉴스히트"],
        "시장추세": cand["시장추세"], "추천": cand["추천"],
    })
    return out.head(config.TOP_N_CANDIDATES * 3).reset_index(drop=True)
