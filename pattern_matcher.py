# -*- coding: utf-8 -*-
"""
종목별 과거 캔들 패턴 매칭 (백테스트로 효과가 확인된 조건).

후보 종목의 최근 3거래일 일봉 패턴 [1일차 몸통, 2일차 갭, 2일차 몸통, 3일차 갭, 3일차 몸통]과
같은 종목의 최근 3년 일봉에서 유클리드 거리 PATTERN_SIMILARITY_THRESHOLD(0.04) 이내인
과거 사례를 모두 찾고, 그때 "패턴 마지막날 종가 -> 다음날 시가"가 올랐던 비율을 계산한다.

계산 정의는 features.py 를 그대로 사용하므로 backtest.py 의 검증 결과와 1:1로 대응한다.
"""

import logging
from datetime import datetime, timedelta

import config
import data_fetcher
import features as F

logger = logging.getLogger(__name__)

EMPTY = {"패턴매칭수": 0, "패턴상승수": 0, "패턴승률": None, "패턴확인": False, "패턴사례": []}


_CAL = {}


def _trading_days(end_date: str) -> list:
    """시장 거래일 달력 (거래정지로 날짜가 건너뛴 사례를 백테스트와 똑같이 제외하기 위함)."""
    if end_date not in _CAL:
        try:
            _CAL[end_date] = data_fetcher.get_trading_days(end_date, int(config.PATTERN_LOOKBACK_YEARS * 250) + 40)
        except Exception as e:
            logger.warning("거래일 달력 조회 실패 - 연속일 검사 생략: %s", e)
            _CAL[end_date] = None
    return _CAL[end_date]


def evaluate_history(hist, trading_days=None) -> dict:
    """종목 하나의 일봉 이력(pykrx 형식)으로 마지막 날 기준 패턴 통계를 계산."""
    if hist is None or hist.empty or len(hist) < max(config.PATTERN_MIN_HISTORY_DAYS, 4):
        return dict(EMPTY)
    f = F.compute_features(F.history_to_panel(hist), trading_days)
    matches = F.last_day_pattern_matches(f)
    total = len(matches)
    if total == 0:
        return dict(EMPTY)
    up = sum(1 for m in matches if m["next_day_up"])
    return {
        "패턴매칭수": total,
        "패턴상승수": up,
        "패턴승률": round(up / total, 3),
        "패턴확인": bool(F.own_pattern_confirmed([total], [up])[0]),
        "패턴사례": sorted(matches, key=lambda m: m["distance"])[:5],
    }


def evaluate_pattern(ticker: str, end_date: str) -> dict:
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    start_dt = end_dt - timedelta(days=int(config.PATTERN_LOOKBACK_YEARS * 365.25) + 10)
    hist = data_fetcher.get_ticker_ohlcv_history(ticker, start_dt.strftime("%Y%m%d"), end_date)
    return evaluate_history(hist, _trading_days(end_date))


def build_pattern_scores(tickers: list, end_date: str) -> dict:
    out = {}
    for t in tickers:
        try:
            out[t] = evaluate_pattern(t, end_date)
        except Exception as e:
            logger.warning("패턴 매칭 실패 (%s): %s", t, e)
            out[t] = dict(EMPTY)
    return out
