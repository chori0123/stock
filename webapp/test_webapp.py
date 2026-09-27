# -*- coding: utf-8 -*-
"""webapp/app.py 를 Flask test client 로 검증 (실제 서버 구동/네트워크 불필요)."""
import os
import sys
import json
import shutil

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
sys.path.insert(0, os.path.join(PROJECT, "webapp"))

import config  # noqa: E402

TEST_DATA_DIR = os.path.join(PROJECT, "webapp", "_test_data")
shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
os.makedirs(TEST_DATA_DIR, exist_ok=True)
config.DATA_DIR = TEST_DATA_DIR

# 샘플 데이터 기록
screen_payload = {
    "date": "20260918", "generated_at": "2026-09-18T15:35:00",
    "stage": "screen_15_30",
    "candidates": [{
        "티커": "000001", "종목명": "가상전자", "종가": 13000, "등락률": 8.0,
        "거래량배수": 3.5, "종가강도": 0.9, "최종점수": 0.87,
        "패턴확인": True, "패턴승률": 0.8, "패턴매칭수": 5,
    }],
}
buy_payload = {
    "date": "20260918", "generated_at": "2026-09-18T18:30:00",
    "stage": "buy_signal_18_00_19_00", "note": "NXT 호가 꼭 확인",
    "buy_candidates": [{
        "티커": "000001", "종목명": "가상전자", "참고매수가": 13000, "최종점수": 0.87,
        "패턴확인": True, "패턴승률": 0.8,
    }],
}
sell_payload = {
    "buy_date": "20260918", "sell_date": "20260919", "generated_at": "2026-09-19T08:30:00",
    "stage": "sell_signal_08_00_09_00", "note": "장초 매도",
    "sell_candidates": [{
        "티커": "000001", "종목명": "가상전자", "전략": "익일 장초 매도", "우선매도권고": False,
    }],
}

with open(os.path.join(TEST_DATA_DIR, "candidates_20260918.json"), "w", encoding="utf-8") as f:
    json.dump(screen_payload, f, ensure_ascii=False)
with open(os.path.join(TEST_DATA_DIR, "buy_signals_20260918.json"), "w", encoding="utf-8") as f:
    json.dump(buy_payload, f, ensure_ascii=False)
with open(os.path.join(TEST_DATA_DIR, "sell_signals_20260919.json"), "w", encoding="utf-8") as f:
    json.dump(sell_payload, f, ensure_ascii=False)

import app as webapp_app  # noqa: E402
webapp_app.config.DATA_DIR = TEST_DATA_DIR  # app.py 가 import 한 config 모듈도 갱신

client = webapp_app.app.test_client()

# 1) 인증 없는 상태 (WEBAPP_PASSWORD 미설정) - 바로 접근 가능해야 함
r = client.get("/")
assert r.status_code == 200, r.status_code
assert "주식 스크리너" in r.get_data(as_text=True)
print("[PASS] GET / -> 200, PWA 셸 HTML 반환")

r = client.get("/api/latest")
assert r.status_code == 200
data = r.get_json()
assert data["screen"]["candidates"][0]["티커"] == "000001"
assert data["buy"]["buy_candidates"][0]["참고매수가"] == 13000
assert data["sell"]["sell_candidates"][0]["우선매도권고"] is False
print("[PASS] GET /api/latest -> 최신 3단계 JSON 정상 반환")

r = client.get("/manifest.json")
assert r.status_code == 200
assert r.headers["Content-Type"].startswith("application/manifest+json")
print("[PASS] GET /manifest.json -> PWA manifest 정상 서빙")

r = client.get("/static/icon-192.png")
assert r.status_code == 200
print("[PASS] GET /static/icon-192.png -> 아이콘 정상 서빙")

# 2) 비밀번호 설정 시 인증 요구되는지 확인
webapp_app.WEBAPP_PASSWORD = "secret123"
webapp_app.WEBAPP_USERNAME = "admin"

r = client.get("/")
assert r.status_code == 401, r.status_code
print("[PASS] WEBAPP_PASSWORD 설정 시 인증 없는 요청은 401")

import base64
creds = base64.b64encode(b"admin:secret123").decode()
r = client.get("/", headers={"Authorization": f"Basic {creds}"})
assert r.status_code == 200
print("[PASS] 올바른 Basic Auth 자격증명으로 200 응답")

r = client.get("/", headers={"Authorization": f"Basic {base64.b64encode(b'admin:wrong').decode()}"})
assert r.status_code == 401
print("[PASS] 잘못된 비밀번호는 401")

print("\n=== webapp 검증 통과 ===")
