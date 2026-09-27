# -*- coding: utf-8 -*-
"""
뉴스 / 공시 기반 점수 산출 모듈.

1) DART(전자공시) Open API - 당일 공시 제목에서 호재/악재 키워드를 찾아 점수화
   (무료 API 키 필요: https://opendart.fss.or.kr)
2) 네이버 금융 종목뉴스 페이지 크롤링 - 당일 뉴스 제목에서 호재/악재 키워드를 찾아 점수화

DART API 키가 없거나 요청이 실패해도 전체 파이프라인이 죽지 않도록,
모든 함수는 실패 시 빈 결과(0점)를 반환하고 로그만 남긴다.
"""

import logging
import time

import requests
from bs4 import BeautifulSoup

import config

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
NAVER_NEWS_URL = "https://finance.naver.com/item/news_news.naver"


# ---------------------------------------------------------------------------
# DART 공시
# ---------------------------------------------------------------------------
def fetch_dart_disclosures(date: str) -> list:
    """date(YYYYMMDD) 하루치 전체 공시 목록을 가져온다 (모든 시장/법인 포함).

    DART_API_KEY 가 설정되어 있지 않으면 빈 리스트를 반환한다.
    """
    if not config.DART_API_KEY:
        logger.warning("DART_API_KEY 가 설정되어 있지 않아 공시 점수를 건너뜁니다.")
        return []

    all_items = []
    page_no = 1
    while True:
        params = {
            "crtfc_key": config.DART_API_KEY,
            "bgn_de": date,
            "end_de": date,
            "page_no": page_no,
            "page_count": 100,
        }
        try:
            resp = requests.get(DART_LIST_URL, params=params, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("DART 공시 조회 실패 (page %d): %s", page_no, e)
            break

        status = data.get("status")
        if status == "013":  # 조회된 데이터가 없음
            break
        if status not in ("000",):
            logger.error("DART API 오류: %s / %s", status, data.get("message"))
            break

        items = data.get("list", [])
        all_items.extend(items)

        total_page = data.get("total_page", 1)
        if page_no >= total_page:
            break
        page_no += 1
        time.sleep(0.2)

    return all_items


def score_disclosures(disclosures: list, tickers: list) -> dict:
    """공시 목록에서 후보 티커에 해당하는 것만 골라 키워드 점수를 매긴다.

    반환: {티커: {"score": float, "hits": [보고서명, ...]}}
    """
    ticker_set = set(tickers)
    result = {t: {"score": 0.0, "hits": []} for t in tickers}

    for item in disclosures:
        stock_code = (item.get("stock_code") or "").strip()
        if not stock_code or stock_code not in ticker_set:
            continue
        report_nm = item.get("report_nm", "")

        pos_hit = any(kw in report_nm for kw in config.POSITIVE_DISCLOSURE_KEYWORDS)
        neg_hit = any(kw in report_nm for kw in config.NEGATIVE_DISCLOSURE_KEYWORDS)

        delta = 0.0
        if pos_hit:
            delta += 1.0
        if neg_hit:
            delta -= 1.5  # 악재는 더 강하게 감점

        if delta != 0.0:
            result[stock_code]["score"] += delta
            result[stock_code]["hits"].append(report_nm)

    return result


# ---------------------------------------------------------------------------
# 네이버 금융 뉴스
# ---------------------------------------------------------------------------
def fetch_naver_news_titles(ticker: str, max_items: int = 20) -> list:
    """네이버 금융 종목뉴스 탭에서 최근 뉴스 제목 리스트를 가져온다."""
    try:
        resp = requests.get(
            NAVER_NEWS_URL, params={"code": ticker, "page": 1}, headers=HEADERS, timeout=10
        )
        resp.raise_for_status()
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")
        titles = [a.get_text(strip=True) for a in soup.select("td.title a")]
        return titles[:max_items]
    except Exception as e:
        logger.warning("네이버 뉴스 조회 실패 (%s): %s", ticker, e)
        return []


def score_news_for_ticker(ticker: str) -> dict:
    titles = fetch_naver_news_titles(ticker)
    score = 0.0
    hits = []
    for t in titles:
        pos = any(kw in t for kw in config.POSITIVE_NEWS_KEYWORDS)
        neg = any(kw in t for kw in config.NEGATIVE_NEWS_KEYWORDS)
        if pos:
            score += 0.5
            hits.append(t)
        if neg:
            score -= 0.7
            hits.append(t)
    return {"score": score, "hits": hits, "titles": titles}


def build_news_scores(tickers: list, date: str) -> dict:
    """후보 종목들에 대한 공시 점수 + 뉴스 점수를 합쳐서 반환한다.

    반환: {티커: {"news_score": float, "disclosure_hits": [...], "news_hits": [...]}}
    """
    disclosures = fetch_dart_disclosures(date)
    disc_scores = score_disclosures(disclosures, tickers)

    out = {}
    for t in tickers:
        naver = score_news_for_ticker(t)
        time.sleep(0.15)
        disc = disc_scores.get(t, {"score": 0.0, "hits": []})
        out[t] = {
            "news_score": disc["score"] + naver["score"],
            "disclosure_score": disc["score"],
            "disclosure_hits": disc["hits"],
            "news_hits": naver["hits"],
        }
    return out
