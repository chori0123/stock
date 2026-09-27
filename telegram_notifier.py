# -*- coding: utf-8 -*-
"""
텔레그램 알림 모듈.

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되어 있지 않으면 조용히 아무것도 하지 않는다
(휴대폰 알림 없이 로컬/PWA로만 확인하는 사용자도 있을 수 있으므로).

봇 생성/채팅방 ID 확인 방법은 README.md "휴대폰에서 확인하기" 섹션 참고.
"""

import logging

import requests

import config

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def is_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_message(text: str) -> bool:
    """텔레그램으로 메시지를 보낸다. 설정이 없거나 실패해도 예외를 던지지 않는다
    (알림 실패가 스크리닝/신호 생성 자체를 막으면 안 되므로)."""
    if not is_configured():
        logger.info("TELEGRAM_BOT_TOKEN/CHAT_ID 미설정 - 텔레그램 알림 생략")
        return False
    url = TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN)
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("텔레그램 메시지 전송 실패: %s", e)
        return False


def _fmt_num(v, unit=""):
    if v is None:
        return "-"
    try:
        return f"{v:,.0f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def _wr_txt(c):
    wr = c.get("패턴승률")
    n = c.get("패턴매칭수") or 0
    return f"과거 동일패턴 {n}건 중 {wr*100:.0f}% 익일시가↑" if isinstance(wr, (int, float)) else "과거 동일패턴 없음"


def format_screen_message(payload: dict) -> str:
    date = payload.get("date", "")
    items = payload.get("candidates", [])
    rec = [c for c in items if c.get("추천")]
    if not items:
        return f"📊 <b>[{date}] 종가 스크리닝</b>\n기술적 조건을 만족하는 종목이 없습니다."
    mu = payload.get("market_up")
    lines = [f"📊 <b>[{date}] 종가 스크리닝</b>",
             "시장지수 20일선 " + ("위 ✅" if mu else "아래 ⛔ (오늘은 매수 쉬는 날)" if mu is False else "확인불가"),
             f"기술적 조건 통과 {len(items)}개 → <b>검증조건 통과 {len(rec)}개</b>"]
    for i, c in enumerate(rec, 1):
        lines.append(f"✅ <b>{c.get('종목명')}</b>({c.get('티커')}) {_fmt_num(c.get('종가'))}원 "
                     f"(+{c.get('등락률', 0):.1f}%) · {_wr_txt(c)}")
    bad = [c for c in items if c.get("패턴확인") and c.get("악재공시")]
    for c in bad:
        lines.append(f"🚫 {c.get('종목명')}: 패턴은 충족했으나 악재공시로 제외")
    if not rec:
        lines.append("오늘은 검증조건 통과 종목이 없어 매수하지 않는 날입니다.")
    return "\n".join(lines)


def format_buy_message(payload: dict) -> str:
    date = payload.get("date", "")
    items = payload.get("buy_candidates", [])
    v = payload.get("validation") or {}
    lines = [f"🌙 <b>[{date}] NXT 매수 신호</b>"]
    if v and (not v.get("edge_confirmed", True) or v.get("recent_warning")):
        lines.append("⚠️ 최근 백테스트에서 전략 유효성이 약해짐 - 매수 보류 권고")
    if payload.get("missing_input"):
        lines.append("⚠️ 15:30 스크리닝 결과가 없어 매수 신호를 만들지 못했습니다. 오늘은 매수하지 마세요.")
        return "\n".join(lines)
    if not items:
        if payload.get("market_up") is False:
            lines.append("시장지수가 20일선 아래 → 오늘은 매수하지 않습니다.")
        else:
            lines.append("검증조건 통과 종목 없음 → 오늘은 매수하지 않습니다.")
        return "\n".join(lines)
    for i, c in enumerate(items, 1):
        lines.append(f"{i}. <b>{c.get('종목명')}</b>({c.get('티커')}) 종가 {_fmt_num(c.get('정규장종가'))}원 "
                     f"→ <b>매수상한 {_fmt_num(c.get('매수상한가'))}원</b>\n   {_wr_txt(c)}")
    lines.append("\n※ NXT 매도호가가 매수상한(종가+0.1%) 이하일 때만 매수. 종목당 비중은 20% 이하로.")
    oos = v.get("oos") if v else None
    if oos and oos.get("trades"):
        lines.append(f"📈 백테스트(검증기간): 1회 평균 {oos['avg_net_pct']:+.2f}%, 승률 {oos['win_rate']*100:.0f}%, "
                     f"최대낙폭 {oos['max_drawdown_pct']}% (비용 차감, 미래 보장 아님)")
    return "\n".join(lines)


def format_sell_message(payload: dict) -> str:
    sell_date = payload.get("sell_date", "")
    items = payload.get("sell_candidates", [])
    if payload.get("missing_input"):
        return (f"🌅 <b>[{sell_date}] 장초 매도 신호</b>\n⚠️ 전 거래일({payload.get('buy_date')}) 매수 신호 기록이 없습니다.\n"
                "매수 신호 단계가 실행되지 않았거나 실패했습니다. 이 신호로 산 종목이 있다면 직접 장초에 매도하세요.")
    if not items:
        return f"🌅 <b>[{sell_date}] 장초 매도 신호</b>\n대상 종목이 없습니다."
    lines = [f"🌅 <b>[{sell_date}] 장초 매도 신호 ({len(items)}개, 08:00~09:00 확인)</b>"]
    for i, c in enumerate(items, 1):
        flag = " 🚨우선매도" if c.get("우선매도권고") else ""
        lines.append(f"{i}. <b>{c.get('종목명')}</b>({c.get('티커')}){flag}")
    return "\n".join(lines)
