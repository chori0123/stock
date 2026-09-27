# -*- coding: utf-8 -*-
"""
CLI 진입점.

사용 예:
  python main.py screen                     # 오늘(또는 최근 거래일) 15:30 종가 스크리닝 실행
  python main.py screen --date 20260918      # 특정일 스크리닝 실행
  python main.py buy                         # 오늘자 후보로 매수 신호 생성
  python main.py buy --date 20260918
  python main.py sell                        # 최근 매수 신호 기준 익일 매도 신호 생성
  python main.py sell --buy-date 20260918 --sell-date 20260919
  python main.py schedule                    # 스케줄러 상시 실행 (15:35/18:30/08:30 자동 실행)
"""

import argparse
import logging
import sys

import signal_generator


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    parser = argparse.ArgumentParser(description="정규장 종가 -> NXT 매수 -> 익일 장초 매도 전략 스크리너")
    sub = parser.add_subparsers(dest="command", required=True)

    p_screen = sub.add_parser("screen", help="15:30 종가 스크리닝 실행")
    p_screen.add_argument("--date", default=None, help="YYYYMMDD, 기본값=최근 거래일")

    p_buy = sub.add_parser("buy", help="NXT 매수 신호 생성")
    p_buy.add_argument("--date", default=None, help="스크리닝 실행일 YYYYMMDD")

    p_sell = sub.add_parser("sell", help="익일 장초 매도 신호 생성")
    p_sell.add_argument("--buy-date", default=None, help="매수 신호 생성일 YYYYMMDD")
    p_sell.add_argument("--sell-date", default=None, help="매도 신호 생성일(오늘) YYYYMMDD")

    sub.add_parser("schedule", help="스케줄러 상시 실행 (15:35/18:30/08:30)")
    sub.add_parser("telegram-test", help="텔레그램 연결 테스트 메시지 전송 (실패하면 원인 출력 후 오류 종료)")

    args = parser.parse_args()
    setup_logging()

    if args.command == "telegram-test":
        import telegram_notifier
        try:
            telegram_notifier.send_test_message()
            print("✅ 텔레그램 테스트 메시지 전송 성공 - 텔레그램 앱을 확인하세요.")
        except Exception as e:
            print(f"❌ 텔레그램 테스트 실패: {e}")
            sys.exit(1)
        return

    import config
    if args.command in ("screen", "buy", "sell", "schedule") and not (config.KRX_ID and config.KRX_PW):
        logging.getLogger("main").error(
            "KRX_ID / KRX_PW 가 설정되지 않았습니다. 2025-12-27부터 KRX 데이터는 로그인이 필수입니다. "
            "GitHub: Settings > Secrets and variables > Actions 에 KRX_ID, KRX_PW 를 등록하세요.")
        if args.command == "screen":
            sys.exit(1)

    if args.command == "screen":
        path = signal_generator.run_screen_and_save(args.date)
        signal_generator.print_summary(path)
    elif args.command == "buy":
        path = signal_generator.generate_buy_signals(args.date)
        signal_generator.print_summary(path)
    elif args.command == "sell":
        path = signal_generator.generate_sell_signals(args.buy_date, args.sell_date)
        signal_generator.print_summary(path)
    elif args.command == "schedule":
        import scheduler
        scheduler.start()


if __name__ == "__main__":
    main()
