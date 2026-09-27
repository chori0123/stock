# -*- coding: utf-8 -*-
"""
KRX(한국거래소) 데이터 수집 모듈.

pykrx 를 통해 정규장 15:30 종가 기준 시세/거래량/시가총액 데이터를 가져오고,
스크리닝에 필요한 이동평균/거래량 배수 등 파생 지표를 계산한다.

주의: 이 모듈은 data.krx.co.kr 에 직접 접속합니다. 사내/샌드박스 방화벽이나
프록시가 이 도메인을 막아둔 환경에서는 동작하지 않으니, 실제 운영 시에는
일반 인터넷 접속이 가능한 PC/서버에서 실행하세요.
"""

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from pykrx import stock

import config

logger = logging.getLogger(__name__)

# 서버(특히 클라우드 VM/GitHub Actions 러너)의 시스템 시각은 보통 UTC라서, 그냥 datetime.now()를
# 쓰면 "오늘 날짜"가 한국 기준과 하루씩 어긋날 수 있다(특히 08:30 KST 매도 신호 실행 시점은
# UTC로는 전날 밤이라 문제가 생김). 그래서 날짜가 필요한 모든 곳에서 명시적으로 KST를 사용한다.
KST = ZoneInfo("Asia/Seoul")


def now_kst() -> datetime:
    return datetime.now(KST)


# ---------------------------------------------------------------------------
# 거래일 계산
# ---------------------------------------------------------------------------
def get_trading_days(end_date: str, n_days: int) -> list:
    """end_date(YYYYMMDD) 이전(포함) n_days 개의 실제 거래일 리스트를 반환한다.

    KOSPI 지수 일별 시세를 조회해 실제로 시장이 열렸던 날짜만 골라낸다.
    (주말/공휴일 자동 제외)
    """
    end_dt = datetime.strptime(end_date, "%Y%m%d")
    # 여유있게 n_days * 2.2 + 15 일 만큼 과거로 잡아서 조회 (연휴 등 대비)
    start_dt = end_dt - timedelta(days=int(n_days * 2.2) + 15)
    start_str = start_dt.strftime("%Y%m%d")

    idx = stock.get_index_ohlcv_by_date(start_str, end_date, "1001")  # KOSPI 지수
    if idx.empty:
        raise RuntimeError(
            "거래일 조회 실패 (KOSPI 지수 데이터를 가져오지 못했습니다). "
            "네트워크 연결 또는 KRX 접속 상태를 확인하세요."
        )
    dates = sorted(idx.index.strftime("%Y%m%d").tolist())
    dates = [d for d in dates if d <= end_date]
    return dates[-n_days:]


def get_latest_trading_day(as_of: str = None) -> str:
    """as_of(YYYYMMDD, 기본값=오늘) 기준 가장 최근 거래일을 반환한다."""
    as_of = as_of or now_kst().strftime("%Y%m%d")
    days = get_trading_days(as_of, 1)
    return days[-1]


# ---------------------------------------------------------------------------
# 시장 스냅샷 수집
# ---------------------------------------------------------------------------
def _fetch_ohlcv_one_day(date: str, markets=None) -> pd.DataFrame:
    """지정한 날짜의 전 종목 OHLCV + 등락률 스냅샷 (KOSPI+KOSDAQ 통합)."""
    markets = markets or config.MARKETS
    frames = []
    for m in markets:
        df = stock.get_market_ohlcv(date, market=m)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["시장"] = m
        df["날짜"] = date
        frames.append(df)
        time.sleep(0.2)  # 과도한 연속 요청 방지
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    out.index.name = "티커"
    return out.reset_index()


def get_market_history(end_date: str, lookback_days: int = None) -> pd.DataFrame:
    """end_date 를 포함해 최근 lookback_days 거래일치 전 종목 OHLCV 이력을 수집한다.

    반환 컬럼: 티커, 날짜, 시가, 고가, 저가, 종가, 거래량, 거래대금, 등락률, 시장
    """
    lookback_days = lookback_days or (config.VOLUME_LOOKBACK_DAYS + max(config.MA_LONG, config.MA_SHORT) + 2)
    days = get_trading_days(end_date, lookback_days)
    logger.info("수집 대상 거래일: %s ~ %s (%d일)", days[0], days[-1], len(days))

    frames = []
    for d in days:
        logger.info("  - %s 시세 수집 중...", d)
        df = _fetch_ohlcv_one_day(d)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def get_market_cap_snapshot(date: str, markets=None) -> pd.DataFrame:
    """지정일 기준 시가총액/상장주식수 스냅샷."""
    markets = markets or config.MARKETS
    frames = []
    for m in markets:
        df = stock.get_market_cap(date, market=m)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["시장"] = m
        frames.append(df)
        time.sleep(0.2)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames)
    out.index.name = "티커"
    return out.reset_index()


def get_ticker_ohlcv_history(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    """특정 종목 하나의 [start_date, end_date] 구간 일봉 OHLCV 이력을 반환한다.

    패턴 매칭(pattern_matcher.py)처럼 특정 종목의 장기(예: 3년) 이력이 필요할 때 사용한다.
    전종목 스냅샷을 매일 반복 조회하는 것보다 종목당 API 호출 1회로 끝나 훨씬 효율적이다.

    반환 컬럼: 날짜(index), 시가, 고가, 저가, 종가, 거래량, (거래대금, 등락률은 버전에 따라 포함될 수 있음)
    """
    # 주의: pykrx 의 adjusted=True(기본값)는 KRX가 아니라 네이버 차트 데이터를 가져온다.
    # 백테스트는 KRX 정규장 원시 시세(+등락률로 액면분할 등 제외)로 검증했으므로, 똑같이
    # KRX 원시 시세(adjusted=False)를 쓴다. 조회 기간 제한에 대비해 1년 단위로 나눠 조회.
    frames = []
    s = datetime.strptime(start_date, "%Y%m%d")
    e_all = datetime.strptime(end_date, "%Y%m%d")
    while s <= e_all:
        e = min(s + timedelta(days=364), e_all)
        try:
            part = stock.get_market_ohlcv_by_date(s.strftime("%Y%m%d"), e.strftime("%Y%m%d"), ticker, adjusted=False)
        except Exception as ex:
            logger.warning("종목 이력 조회 실패 (%s %s~%s): %s", ticker, s.date(), e.date(), ex)
            return pd.DataFrame()   # 일부 기간이 빠진 이력으로 판정하면 안 되므로 전체 실패 처리
        if part is not None and not part.empty:
            frames.append(part)
        s = e + timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "날짜"
    return df


_NAME_CACHE = {}


def get_ticker_name(ticker: str) -> str:
    if ticker in _NAME_CACHE:
        return _NAME_CACHE[ticker]
    try:
        name = stock.get_market_ticker_name(ticker)
    except Exception:
        name = ticker
    _NAME_CACHE[ticker] = name or ticker
    return _NAME_CACHE[ticker]


# ---------------------------------------------------------------------------
# 파생 지표 계산
# ---------------------------------------------------------------------------
def build_snapshot_with_indicators(end_date: str = None) -> pd.DataFrame:
    """15:30 종가 스크리닝에 쓸 최종 데이터셋을 만든다.

    - 최근 N거래일 시세 이력 수집
    - 종목별 5일선/20일선, 최근 20일 평균거래량(당일 제외), 당일 거래량 배수,
      당일 종가강도((종가-저가)/(고가-저가)) 계산
    - 시가총액 결합
    - 종목명 결합
    """
    end_date = end_date or get_latest_trading_day()
    hist = get_market_history(end_date)
    if hist.empty:
        raise RuntimeError("시세 이력을 가져오지 못했습니다.")

    hist["날짜"] = pd.to_datetime(hist["날짜"], format="%Y%m%d")
    hist = hist.sort_values(["티커", "날짜"])

    rows = []
    last_date = hist["날짜"].max()

    for ticker, g in hist.groupby("티커"):
        g = g.sort_values("날짜")
        today = g[g["날짜"] == last_date]
        if today.empty:
            continue
        today = today.iloc[-1]

        prior = g[g["날짜"] < last_date]
        avg_vol_20 = prior["거래량"].tail(config.VOLUME_LOOKBACK_DAYS).mean() if not prior.empty else float("nan")
        ma_short = g["종가"].tail(config.MA_SHORT).mean()
        ma_long = g["종가"].tail(config.MA_LONG).mean() if len(g) >= config.MA_LONG else float("nan")

        high, low, close = today["고가"], today["저가"], today["종가"]
        close_strength = (close - low) / (high - low) if (high - low) > 0 else 1.0

        volume_ratio = today["거래량"] / avg_vol_20 if avg_vol_20 and avg_vol_20 > 0 else float("nan")

        rows.append({
            "티커": ticker,
            "시장": today["시장"],
            "날짜": last_date.strftime("%Y%m%d"),
            "시가": today["시가"],
            "고가": high,
            "저가": low,
            "종가": close,
            "거래량": today["거래량"],
            "거래대금": today["거래대금"],
            "등락률": today["등락률"],
            "평균거래량20": avg_vol_20,
            "거래량배수": volume_ratio,
            "종가강도": close_strength,
            "MA5": ma_short,
            "MA20": ma_long,
            "MA정배열": bool(ma_short > ma_long) if pd.notna(ma_long) else None,
        })

    snap = pd.DataFrame(rows)
    if snap.empty:
        return snap

    cap = get_market_cap_snapshot(end_date)
    if not cap.empty:
        cap_cols = cap[["티커", "시가총액", "상장주식수"]]
        snap = snap.merge(cap_cols, on="티커", how="left")

    snap["종목명"] = snap["티커"].apply(get_ticker_name)
    return snap
