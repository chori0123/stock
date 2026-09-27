# -*- coding: utf-8 -*-
"""
APScheduler 기반 자동 실행 스케줄러.

- 평일 15:35  : 정규장 종가 스크리닝 (거래소 데이터 반영 시차 감안, 15:30 대신 15:35 실행)
- 평일 18:30  : NXT 매수 신호 생성 (18:00~19:00 구간 중간)
- 평일 08:30  : 전일 매수 종목 익일 매도 신호 생성 (08:00~09:00 구간 중간)

실행: python main.py schedule
"""

import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import signal_generator

logger = logging.getLogger(__name__)


def job_screen():
    try:
        path = signal_generator.run_screen_and_save()
        signal_generator.print_summary(path)
    except Exception:
        logger.exception("15:30 스크리닝 작업 실패")


def job_buy_signal():
    try:
        path = signal_generator.generate_buy_signals()
        signal_generator.print_summary(path)
    except Exception:
        logger.exception("매수 신호 생성 작업 실패")


def job_sell_signal():
    try:
        path = signal_generator.generate_sell_signals()
        signal_generator.print_summary(path)
    except Exception:
        logger.exception("매도 신호 생성 작업 실패")


def start():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # 평일(월~금) 15:35 - 정규장 종가 스크리닝
    scheduler.add_job(
        job_screen, CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
        id="screen_15_30", misfire_grace_time=600,
    )
    # 평일 18:30 - NXT 매수 신호
    scheduler.add_job(
        job_buy_signal, CronTrigger(day_of_week="mon-fri", hour=18, minute=30),
        id="buy_signal", misfire_grace_time=600,
    )
    # 평일 08:30 - 익일 매도 신호
    scheduler.add_job(
        job_sell_signal, CronTrigger(day_of_week="mon-fri", hour=8, minute=30),
        id="sell_signal", misfire_grace_time=600,
    )

    logger.info("스케줄러 시작 (KST 기준) - 15:35 스크리닝 / 18:30 매수신호 / 08:30 매도신호")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("스케줄러 종료")


if __name__ == "__main__":
    start()
