# -*- coding: utf-8 -*-
"""
전략 타임라인에 맞춰 3단계 산출물을 생성한다.

  1) 15:30  run_screen_and_save()   -> data/candidates_YYYYMMDD.json
  2) 18~19시 generate_buy_signals()  -> data/buy_signals_YYYYMMDD.json
  3) 익일 08~09시 generate_sell_signals() -> data/sell_signals_YYYYMMDD.json

NXT(대체거래소) 는 저녁 시간대(오후 8시~) 및 익일 아침(오전 8시~) 시간외 매매가 가능한
거래소이지만, 실시간 NXT 호가는 증권사 API(예: 한국투자증권 KIS Developers, 키움 OpenAPI+)
계좌 연동 없이는 무료로 가져올 수 없다. 따라서 이 프로그램은 "매매 신호(후보 종목 + 참고가)"
까지만 자동 생성하고, 실제 주문은 사용자가 증권사 MTS/HTS에서 NXT 시세를 확인한 뒤 직접
실행하는 것을 전제로 한다.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import config
import data_fetcher
import news_fetcher
import screener
import telegram_notifier

logger = logging.getLogger(__name__)

os.makedirs(config.DATA_DIR, exist_ok=True)

# GitHub Pages(docs/) 로 정적 호스팅할 때 프런트엔드가 고정된 파일명으로 읽어갈 수 있도록
# "최신 결과"를 docs/data/latest_*.json 에도 함께 남겨둔다. GitHub Actions 워크플로우가
# 이 파일들을 커밋/푸시하면, 별도 서버 없이도 GitHub Pages가 그대로 서빙해준다.
WEB_DATA_DIR = os.path.join(config.BASE_DIR, "docs", "data")


def _path(kind: str, date: str) -> str:
    return os.path.join(config.DATA_DIR, f"{kind}_{date}.json")


def _save_json(path: str, payload: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    logger.info("저장 완료: %s", path)


def _publish_web_copy(kind: str, payload: dict):
    """서버 없이 GitHub Pages 등 정적 호스팅에서 바로 읽을 수 있도록 고정 파일명으로도 저장.

    실패해도(예: docs/ 디렉터리 쓰기 권한 없음) 전체 파이프라인을 막지 않는다.
    """
    try:
        os.makedirs(WEB_DATA_DIR, exist_ok=True)
        path = os.path.join(WEB_DATA_DIR, f"latest_{kind}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        logger.info("웹앱용 최신 데이터 저장 완료: %s", path)
    except Exception as e:
        logger.warning("웹앱용 데이터(docs/data) 저장 실패 (무시하고 계속 진행): %s", e)


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_validation():
    """backtest.py 가 저장한 '현재 전략' 검증 통계 (없으면 None)."""
    path = os.path.join(config.DATA_DIR, "model.json")
    if not os.path.exists(path):
        return None
    try:
        return _load_json(path).get("live_strategy")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1) 15:30 스크리닝
# ---------------------------------------------------------------------------
def run_screen_and_save(end_date: str = None) -> str:
    result = screener.run_screen(end_date)
    date = end_date or data_fetcher.get_latest_trading_day()

    payload = {
        "date": date,
        "generated_at": data_fetcher.now_kst().isoformat(),
        "stage": "screen_15_30",
        # 시장지수가 20일선 위인가 (아래면 전략상 매수하지 않음)
        "market_up": bool(result["시장추세"].iloc[0]) if (not result.empty and "시장추세" in result) else None,
        "candidates": result.to_dict(orient="records") if not result.empty else [],
    }
    path = _path("candidates", date)
    _save_json(path, payload)
    _publish_web_copy("screen", payload)
    telegram_notifier.send_message(telegram_notifier.format_screen_message(payload))
    return path


# ---------------------------------------------------------------------------
# 2) 18:00~19:00 NXT 매수 신호
# ---------------------------------------------------------------------------
def generate_buy_signals(date: str = None) -> str:
    date = date or data_fetcher.get_latest_trading_day()
    cand_path = _path("candidates", date)
    if not os.path.exists(cand_path):
        raise FileNotFoundError(
            f"{cand_path} 가 없습니다. 먼저 run_screen_and_save() (15:30 스크리닝)를 실행하세요."
        )
    cand = _load_json(cand_path)
    candidates = [c for c in cand.get("candidates", []) if c.get("추천")][: config.TOP_K]
    validation = load_validation()

    buy_list = []
    for c in candidates:
        close = c.get("종가")
        buy_list.append({
            "티커": c["티커"],
            "종목명": c.get("종목명"),
            "정규장종가": close,
            "참고매수가": close,  # NXT 실시간 호가 미연동 -> 정규장 종가를 참고가로 사용
            # 백테스트: 평상시(2016~2022) 기대수익 약 +0.2% -> 종가보다 0.2% 비싸게 사면 기대수익 0
            "매수상한가": int(close * (1 + config.MAX_BUY_PREMIUM)) if close else None,
            "등락률": c.get("등락률"),
            "거래량배수": c.get("거래량배수"),
            "패턴확인": c.get("패턴확인"),
            "패턴승률": c.get("패턴승률"),
            "패턴매칭수": c.get("패턴매칭수"),
            "비고": "NXT 호가가 매수상한가보다 높으면 매수하지 마세요. 실제 호가는 HTS/MTS에서 확인.",
        })

    market_up = cand.get("market_up")
    if buy_list:
        note = ("검증된 조건(기술적 조건 + 종목 자체 과거패턴 + 시장 상승추세)을 통과한 종목입니다. "
                "NXT 매도호가가 '매수상한가'(종가+0.1%) 이하일 때만 매수하세요. 종목당 비중은 20% 이하로.")
    elif market_up is False and config.MARKET_TREND_FILTER:
        note = ("시장지수가 20일 이동평균 아래입니다. 과거 11년간 이런 날은 이 전략의 기대수익이 "
                "0 근처였고 손실폭이 컸으므로 오늘은 매수하지 않습니다.")
    else:
        note = "오늘은 검증된 조건을 통과한 종목이 없습니다. 전략상 매수하지 않는 날입니다."
    if validation and (not validation.get("edge_confirmed", True) or validation.get("recent_warning")):
        note = "⚠️ 최근 백테스트에서 이 전략의 유효성이 약해졌습니다. 매수 보류를 권합니다. " + note

    payload = {
        "date": date,
        "generated_at": data_fetcher.now_kst().isoformat(),
        "stage": "buy_signal_18_00_19_00",
        "window": config.BUY_SIGNAL_WINDOW,
        "buy_candidates": buy_list,
        "market_up": market_up,
        "validation": validation,
        "note": note,
    }
    path = _path("buy_signals", date)
    _save_json(path, payload)
    _publish_web_copy("buy", payload)
    telegram_notifier.send_message(telegram_notifier.format_buy_message(payload))
    return path


# ---------------------------------------------------------------------------
# 3) 익일 08:00~09:00 매도 신호
# ---------------------------------------------------------------------------
def generate_sell_signals(buy_date: str = None, sell_date: str = None) -> str:
    """buy_date 에 생성된 매수 신호 종목들에 대해 익일 장초 매도 신호를 만든다.

    - 기본 전략: 익일 시가(장 시작 직후) 매도
    - 부가 기능: 매도일 아침까지의 DART 공시를 다시 조회해 악재 공시가 있으면 '우선 매도' 플래그
    """
    buy_date = buy_date or data_fetcher.get_latest_trading_day(
        (data_fetcher.now_kst() - timedelta(days=1)).strftime("%Y%m%d")
    )
    sell_date = sell_date or data_fetcher.now_kst().strftime("%Y%m%d")

    buy_path = _path("buy_signals", buy_date)
    if not os.path.exists(buy_path):
        raise FileNotFoundError(
            f"{buy_path} 가 없습니다. 먼저 generate_buy_signals() 를 실행하세요."
        )
    buy = _load_json(buy_path)
    buy_candidates = buy.get("buy_candidates", [])
    tickers = [b["티커"] for b in buy_candidates]

    disclosures = news_fetcher.fetch_dart_disclosures(sell_date)
    disc_scores = news_fetcher.score_disclosures(disclosures, tickers)

    sell_list = []
    for b in buy_candidates:
        t = b["티커"]
        d = disc_scores.get(t, {"score": 0.0, "hits": []})
        priority_sell = d["score"] < 0
        sell_list.append({
            "티커": t,
            "종목명": b.get("종목명"),
            "매수기준가(전일종가)": b.get("정규장종가"),
            "전략": "익일 장초 매도 (08:00~09:00, NXT 또는 정규장 동시호가 활용)",
            "익일공시점수": d["score"],
            "익일공시히트": d["hits"],
            "우선매도권고": priority_sell,
            "비고": (
                "장 시작 전 악재성 공시가 감지되어 우선 매도를 권고합니다." if priority_sell
                else "특이 악재 공시는 감지되지 않았습니다. 계획대로 장초 매도를 진행하세요."
            ),
        })

    payload = {
        "buy_date": buy_date,
        "sell_date": sell_date,
        "generated_at": data_fetcher.now_kst().isoformat(),
        "stage": "sell_signal_08_00_09_00",
        "window": config.SELL_SIGNAL_WINDOW,
        "sell_candidates": sell_list,
        "note": (
            "익일 08:00~09:00 사이 NXT 시간외 매매 또는 정규장 개장 동시호가를 활용해 "
            "매도하는 것을 기본 전략으로 합니다. 우선매도권고=true 인 종목은 장 시작 전 "
            "악재 공시가 있었으므로 우선적으로 처리하는 것을 검토하세요."
        ),
    }
    path = _path("sell_signals", sell_date)
    _save_json(path, payload)
    _publish_web_copy("sell", payload)
    telegram_notifier.send_message(telegram_notifier.format_sell_message(payload))
    return path


# ---------------------------------------------------------------------------
# 사람이 읽기 편한 요약 출력
# ---------------------------------------------------------------------------
def print_summary(path: str):
    data = _load_json(path)
    stage = data.get("stage")
    print(f"\n=== {stage} ({path}) ===")
    key = {
        "screen_15_30": "candidates",
        "buy_signal_18_00_19_00": "buy_candidates",
        "sell_signal_08_00_09_00": "sell_candidates",
    }.get(stage)
    items = data.get(key, []) if key else []
    if not items:
        print("해당 조건을 만족하는 종목이 없습니다.")
        return
    for i, it in enumerate(items, 1):
        name = it.get("종목명", "")
        ticker = it.get("티커", "")
        print(f"{i:2d}. [{ticker}] {name}  -> {json.dumps(it, ensure_ascii=False)}")
