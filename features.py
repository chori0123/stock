# -*- coding: utf-8 -*-
"""
백테스트와 실시간 스크리닝이 "완전히 같은 코드"로 지표를 계산하도록 모아둔 모듈.

입력 panel (종목×날짜 일봉) 컬럼:
    ticker, date(YYYYMMDD 문자열), market, open, high, low, close, volume, value, chg, cap

모든 지표는 해당 날짜 "종가 시점까지" 알 수 있는 정보만 사용한다 (미래 정보 누수 없음).
타깃(target_*)은 다음 거래일 시가 기준 수익률로, 백테스트에서만 사용한다.
"""

import numpy as np
import pandas as pd

import config

# 캔들 패턴 구간 (몸통수익률 / 갭) - 시장 전체에서 같은 패턴을 모아 통계를 낼 때 사용
BODY_EDGES = [-np.inf, -0.03, -0.005, 0.005, 0.03, np.inf]   # 5단계
GAP_EDGES = [-np.inf, -0.01, 0.01, np.inf]                    # 3단계

# 모델 입력용 구간 (원-핫)
BUCKETS = {
    "chg": [-np.inf, -5, -2, 0, 2, 5, 10, 20, 29, np.inf],          # 당일 등락률(%)
    "vol_ratio": [-np.inf, 0.5, 1, 2, 3, 5, 10, np.inf],            # 20일 평균 대비 거래량 배수
    "close_strength": [-np.inf, 0.25, 0.5, 0.75, 0.9, np.inf],      # (종가-저가)/(고가-저가)
    "upper_wick": [-np.inf, 0.005, 0.02, 0.05, np.inf],             # 윗꼬리 / 종가
    "dist_high20": [-np.inf, -0.2, -0.1, -0.03, -1e-9, np.inf],     # 20일 최고종가 대비 위치 (0 = 신고가 마감)
    "cap": [-np.inf, 1e11, 3e11, 1e12, 1e13, np.inf],               # 시가총액(원)
}

# 종목별(자기 자신의 과거) 패턴 매칭 파라미터 - pattern_matcher.py 와 동일한 정의
PS_WINDOW = 3
PS_THRESHOLD = config.PATTERN_SIMILARITY_THRESHOLD   # 0.04


def own_pattern_confirmed(count, wins):
    """백테스트로 검증된 조건: 과거 유사패턴 MIN_PATTERN_MATCHES건 이상 & 익일 시가상승 비율 기준 이상."""
    count = np.asarray(count, float)
    wins = np.asarray(wins, float)
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.where(count > 0, wins / np.where(count > 0, count, 1), 0.0)
    return (count >= config.MIN_PATTERN_MATCHES) & (rate >= config.PATTERN_WIN_RATE_THRESHOLD)


def _group_rolling(df, col, window, func, min_periods, shift=0):
    s = df.groupby("ticker", sort=False)[col]
    if shift:
        s = s.shift(shift).groupby(df["ticker"], sort=False)
    r = getattr(s.rolling(window, min_periods=min_periods), func)()
    return r.reset_index(level=0, drop=True).sort_index()


def compute_features(panel: pd.DataFrame, trading_days=None) -> pd.DataFrame:
    """panel -> 지표/패턴/타깃 컬럼이 추가된 DataFrame (ticker, date 정렬)."""
    df = panel.sort_values(["ticker", "date"]).reset_index(drop=True).copy()
    for c in ["open", "high", "low", "close", "volume", "value", "chg", "cap"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)

    g = df.groupby("ticker", sort=False)
    df["traded"] = (df["volume"] > 0) & (df["open"] > 0) & (df["close"] > 0)

    # --- 거래량 배수: 당일 거래량 / 직전 20거래일 평균 (당일 제외)
    df["avg_vol20"] = _group_rolling(df, "volume", 20, "mean", 15, shift=1)
    df["vol_ratio"] = df["volume"] / df["avg_vol20"].replace(0, np.nan)

    # --- 종가강도
    rng = df["high"] - df["low"]
    df["close_strength"] = np.where(rng > 0, (df["close"] - df["low"]) / rng.where(rng > 0, 1), 1.0)

    # --- 이동평균 정배열 (당일 포함)
    df["ma5"] = _group_rolling(df, "close", 5, "mean", 5)
    df["ma20"] = _group_rolling(df, "close", 20, "mean", 20)
    df["ma_align"] = (df["ma5"] > df["ma20"]).astype(float)
    df.loc[df["ma20"].isna(), "ma_align"] = np.nan

    # --- 윗꼬리, 20일 최고종가 대비 위치
    df["upper_wick"] = (df["high"] - df[["open", "close"]].max(axis=1)) / df["close"].replace(0, np.nan)
    df["max_close20"] = _group_rolling(df, "close", 20, "max", 20)
    df["dist_high20"] = df["close"] / df["max_close20"] - 1

    df["limit_up"] = (df["chg"] >= 29.0).astype(float)
    df["kosdaq"] = (df["market"] == "KOSDAQ").astype(float)

    # --- 캔들 패턴 벡터 [몸통1, 갭2, 몸통2, 갭3, 몸통3] (마지막 날 = 당일)
    prev_close = g["close"].shift(1)
    df["body"] = np.where(df["open"] > 0, df["close"] / df["open"].where(df["open"] > 0, 1) - 1, np.nan)
    df["gap"] = np.where((df["open"] > 0) & (prev_close > 0), df["open"] / prev_close - 1, np.nan)
    g = df.groupby("ticker", sort=False)
    df["p_b1"] = g["body"].shift(2)
    df["p_g2"] = g["gap"].shift(1)
    df["p_b2"] = g["body"].shift(1)
    df["p_g3"] = df["gap"]
    df["p_b3"] = df["body"]
    pcols = ["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]
    has_pat = df[pcols].notna().all(axis=1)
    bi = lambda s: np.digitize(s.fillna(0).values, BODY_EDGES[1:-1])
    gi = lambda s: np.digitize(s.fillna(0).values, GAP_EDGES[1:-1])
    key = (pd.Series(bi(df["p_b1"])).astype(str) + pd.Series(gi(df["p_g2"])).astype(str)
           + pd.Series(bi(df["p_b2"])).astype(str) + pd.Series(gi(df["p_g3"])).astype(str)
           + pd.Series(bi(df["p_b3"])).astype(str))
    df["pattern_key"] = np.where(has_pat, key, None)

    # --- 타깃: 오늘 종가 매수 -> 다음 거래일 시가 매도
    df["next_open"] = g["open"].shift(-1)
    df["next_close"] = g["close"].shift(-1)
    df["next_chg"] = g["chg"].shift(-1)
    df["next_volume"] = g["volume"].shift(-1)
    df["next_date"] = g["date"].shift(-1)

    valid = df["traded"] & (df["next_open"] > 0) & (df["next_volume"] > 0)
    if trading_days is not None:
        td = sorted(trading_days)
        nxt = dict(zip(td[:-1], td[1:]))
        valid &= df["date"].map(nxt) == df["next_date"]          # 거래정지로 날짜가 건너뛴 경우 제외
    # 액면분할/병합/권리락 등으로 기준가가 바뀐 날 제외 (원시 가격이라 가짜 갭이 생김)
    implied_prev = df["next_close"] / (1 + df["next_chg"] / 100.0)
    valid &= (implied_prev / df["close"] - 1).abs() < 0.005
    df["target_valid"] = valid.fillna(False)
    df["target_gross"] = np.where(df["target_valid"], df["next_open"] / df["close"] - 1, np.nan)
    df["next_up"] = np.where(df["target_valid"], (df["next_open"] > df["close"]).astype(float), np.nan)
    return df


def per_stock_pattern_stats(vectors: np.ndarray, next_up: np.ndarray,
                            threshold: float = PS_THRESHOLD):
    """사용자 아이디어(같은 종목 자신의 과거에서 같은 3일 패턴 찾기)의 통계를 누수 없이 계산.

    vectors: (n,5) 날짜순 패턴 벡터 (NaN 허용), next_up: (n,) 각 날짜 다음날 시가 상승 여부(NaN=미확정)
    반환: count[t], wins[t] - t일 종가 시점에 알 수 있는 과거 사례만 사용 (s <= t-1).
    """
    n = len(vectors)
    if n == 0:
        return np.zeros(0, int), np.zeros(0, int)
    V = np.asarray(vectors, float)
    up = np.asarray(next_up, float)
    ok = ~np.isnan(V).any(axis=1) & ~np.isnan(up)
    Vf = np.nan_to_num(V)
    diff = Vf[:, None, :] - Vf[None, :, :]
    D = np.sqrt((diff ** 2).sum(axis=2))
    M = (D <= threshold) & ok[None, :] & ~np.isnan(V).any(axis=1)[:, None]
    M = np.tril(M, k=-1)
    count = M.sum(axis=1)
    wins = (M & (up == 1)[None, :]).sum(axis=1)
    return count, wins


def history_to_panel(hist: pd.DataFrame, ticker: str = "X") -> pd.DataFrame:
    """pykrx 종목별 일봉(한국어 컬럼, 날짜 인덱스) -> compute_features 입력 형식."""
    h = hist.sort_index()
    o, c = h["시가"].astype(float), h["종가"].astype(float)
    return pd.DataFrame({
        "ticker": ticker,
        "date": pd.to_datetime(h.index).strftime("%Y%m%d"),
        "market": "KOSPI",
        "open": o.values,
        "high": (h["고가"] if "고가" in h else pd.concat([o, c], axis=1).max(axis=1)).astype(float).values,
        "low": (h["저가"] if "저가" in h else pd.concat([o, c], axis=1).min(axis=1)).astype(float).values,
        "close": c.values,
        "volume": (h["거래량"].astype(float).values if "거래량" in h else np.ones(len(h))),
        "value": 0.0,
        # 거래소 등락률이 있으면 그대로 사용 -> 액면분할 등 기준가 변경일을 백테스트와 똑같이 걸러냄
        "chg": (h["등락률"].astype(float).values if "등락률" in h else (c.pct_change().fillna(0) * 100).values),
        "cap": 0.0,
    })


def last_day_pattern_matches(f: pd.DataFrame, threshold: float = None):
    """compute_features 결과(한 종목)의 마지막 날 패턴과 유사한 과거 사례 목록.

    per_stock_pattern_stats 의 마지막 행과 정확히 같은 정의 (s <= t-1 사례만 사용).
    """
    threshold = PS_THRESHOLD if threshold is None else threshold
    pcols = ["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]
    V = f[pcols].values.astype(float)
    up = f["next_up"].values.astype(float)
    t = len(f) - 1
    if t < 1 or np.isnan(V[t]).any():
        return []
    prior, prior_up = V[:t], up[:t]
    ok = ~np.isnan(prior).any(axis=1) & ~np.isnan(prior_up)
    d = np.sqrt(((np.nan_to_num(prior) - V[t]) ** 2).sum(axis=1))
    idx = np.where(ok & (d <= threshold))[0]
    return [{"date": f["date"].iloc[s], "distance": round(float(d[s]), 5),
             "next_day_up": bool(prior_up[s] == 1),
             "next_day_change_pct": round(float(f["target_gross"].iloc[s]) * 100, 2)} for s in idx]


def market_trend(panel: pd.DataFrame, ma_days: int = None) -> pd.Series:
    """날짜별 '시장지수(시가총액 가중 전 종목)가 N일 이동평균 위인가' (당일 종가 기준, 미래정보 없음).

    백테스트(2016~2022 탐색, 2023~2026 검증)에서 지수가 20일선 아래일 때는 이 전략의
    기대수익이 0 근처였고 손실폭이 컸다 -> 20일선 위일 때만 매수.
    """
    ma_days = ma_days or config.MARKET_MA_DAYS
    p = panel[["date", "chg", "cap"]].copy()
    p["chg"] = pd.to_numeric(p["chg"], errors="coerce")
    p["cap"] = pd.to_numeric(p["cap"], errors="coerce")
    p = p.dropna()
    p["w"] = p["chg"] / 100 * p["cap"]
    agg = p.groupby("date").agg(w=("w", "sum"), cap=("cap", "sum")).sort_index()
    idx = (1 + agg["w"] / agg["cap"]).cumprod()
    ma = idx.rolling(ma_days, min_periods=ma_days).mean()
    return (idx > ma).where(ma.notna())


def tax_rate(date: str) -> float:
    """증권거래세(매도 시, 코스피는 농특세 포함) 연도별 이력."""
    if date < "20190603": return 0.0030
    if date < "20210101": return 0.0025
    if date < "20230101": return 0.0023
    if date < "20240101": return 0.0020
    if date < "20250101": return 0.0018
    if date < "20260101": return 0.0015
    return 0.0020


def windowed_pattern_stats(mini: pd.DataFrame, targets: pd.DataFrame, lookback_days: int = None) -> pd.DataFrame:
    """실시간 프로그램과 같은 방식(각 날짜 기준 직전 N년 창)으로 종목별 과거패턴 통계 계산.

    mini: 전 종목·전 날짜의 [ticker, date, p_b1..p_b3, next_up, target_gross] (compute_features 결과)
    targets: 통계가 필요한 행(ticker, date). 반환: targets 인덱스 기준 ps_count, ps_wins, ps_mean
    """
    look = pd.Timedelta(days=lookback_days if lookback_days is not None
                        else int(config.PATTERN_LOOKBACK_YEARS * 365.25) + 10).to_timedelta64()
    pcols = ["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]
    out = pd.DataFrame({"ps_count": 0, "ps_wins": 0, "ps_mean": np.nan}, index=targets.index)
    tpos = targets.groupby("ticker").indices
    mini = mini.sort_values(["ticker", "date"])
    for tk, idx in mini.groupby("ticker", sort=False).indices.items():
        if tk not in tpos:
            continue
        g = mini.iloc[idx]
        V = g[pcols].values.astype(float)
        up = g["next_up"].values.astype(float)
        rt = g["target_gross"].values.astype(float)
        dts = pd.to_datetime(g["date"].values).values
        pos = {d: i for i, d in enumerate(g["date"].values)}
        okrow = ~np.isnan(V).any(axis=1) & ~np.isnan(up)
        for ti in tpos[tk]:
            ri = targets.index[ti]
            t = pos.get(targets.at[ri, "date"])
            if t is None or np.isnan(V[t]).any():
                continue
            lo = int(np.searchsorted(dts, dts[t] - look))
            if t - lo + 1 < config.PATTERN_MIN_HISTORY_DAYS:
                continue
            s = slice(lo, t)
            d = np.sqrt(((V[s] - V[t]) ** 2).sum(axis=1))
            m = okrow[s] & (d <= PS_THRESHOLD)
            n = int(m.sum())
            if n:
                out.at[ri, "ps_count"] = n
                out.at[ri, "ps_wins"] = int((up[s][m] == 1).sum())
                out.at[ri, "ps_mean"] = float(np.nanmean(rt[s][m]))
    return out


def add_per_stock_pattern(df: pd.DataFrame, rows_mask=None) -> pd.DataFrame:
    """compute_features 결과에 ps_count / ps_wins 컬럼을 추가 (종목별 과거 패턴 통계)."""
    df = df.copy()
    df["ps_count"] = 0
    df["ps_wins"] = 0
    pcols = ["p_b1", "p_g2", "p_b2", "p_g3", "p_b3"]
    need = df["ticker"].unique() if rows_mask is None else df.loc[rows_mask, "ticker"].unique()
    for t, idx in df[df["ticker"].isin(need)].groupby("ticker", sort=False).groups.items():
        sub = df.loc[idx]
        c, w = per_stock_pattern_stats(sub[pcols].values, sub["next_up"].values)
        df.loc[idx, "ps_count"] = c
        df.loc[idx, "ps_wins"] = w
    return df


LIMIT_UP_PCT = 29.0


def liquid_mask(df, min_price, min_value, min_cap, max_price=None):
    """실제로 종가 부근에 '살 수 있는' 종목만.

    - 상한가(+29% 이상) 마감 종목 제외: 매수 잔량이 쌓여 있어 종가/NXT 저녁에 체결이 거의 안 됨.
      (백테스트에서는 이런 종목의 익일 갭상승이 '가짜 수익'으로 잡힌다)
    - 신규상장 직후(과거 거래량 이력 15일 미만) 제외
    """
    m = df["traded"] & (df["close"] >= min_price) & (df["value"] >= min_value) & (df["cap"] >= min_cap)
    m &= df["chg"] < LIMIT_UP_PCT
    m &= df["avg_vol20"].notna()
    if max_price:
        m &= df["close"] <= max_price
    return m


def rules_mask(df, cfg):
    """현재 config 의 기술적 조건 (기존 screener.apply_technical_filters 와 동일)."""
    m = liquid_mask(df, cfg.MIN_PRICE, cfg.MIN_TRADING_VALUE, cfg.MIN_MARKET_CAP, cfg.MAX_PRICE)
    m &= (df["chg"] >= cfg.MIN_CHANGE_PCT) & (df["chg"] <= cfg.MAX_CHANGE_PCT)
    m &= df["vol_ratio"] >= cfg.MIN_VOLUME_RATIO
    m &= df["close_strength"] >= cfg.MIN_CLOSE_STRENGTH
    if cfg.REQUIRE_MA_ALIGNMENT:
        m &= df["ma_align"] == 1
    return m.fillna(False)
