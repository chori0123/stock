# -*- coding: utf-8 -*-
"""
휴대폰에서 확인하는 모바일 대시보드 (PWA).

data/ 디렉터리에 저장된 가장 최신 candidates_*.json / buy_signals_*.json /
sell_signals_*.json 을 읽어 모바일 친화적인 화면으로 보여준다.

실행:
    python webapp/app.py
    (기본 포트 8000, PORT 환경변수로 변경 가능)

WEBAPP_PASSWORD 환경변수를 설정하면 HTTP Basic 인증이 활성화된다.
클라우드 서버 등 외부에서 접속 가능한 곳에 배포할 때는 반드시 설정할 것을 권장한다
(설정하지 않으면 URL을 아는 누구나 스크리닝 결과를 볼 수 있다).
"""

import glob
import json
import os
import sys
from functools import wraps

from flask import Flask, jsonify, request, Response, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    static_folder=os.path.join(APP_DIR, "static"),
    template_folder=os.path.join(APP_DIR, "templates"),
)

WEBAPP_USERNAME = os.environ.get("WEBAPP_USERNAME", "admin")
WEBAPP_PASSWORD = os.environ.get("WEBAPP_PASSWORD", "")


def _check_auth(username: str, password: str) -> bool:
    return username == WEBAPP_USERNAME and password == WEBAPP_PASSWORD


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not WEBAPP_PASSWORD:
            return f(*args, **kwargs)  # 비밀번호 미설정 시 인증 생략 (로컬 테스트 용도)
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return Response(
                "인증이 필요합니다.", 401,
                {"WWW-Authenticate": 'Basic realm="stock screener"'},
            )
        return f(*args, **kwargs)
    return decorated


def _latest_file(kind: str):
    """data/ 안에서 kind_YYYYMMDD.json 중 가장 최근 날짜 파일을 읽어 반환. 없으면 None."""
    pattern = os.path.join(config.DATA_DIR, f"{kind}_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    with open(files[-1], "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/")
@requires_auth
def index():
    return send_from_directory(app.template_folder, "index.html")


@app.route("/api/latest")
@requires_auth
def api_latest():
    return jsonify({
        "screen": _latest_file("candidates"),
        "buy": _latest_file("buy_signals"),
        "sell": _latest_file("sell_signals"),
    })


@app.route("/backtest")
@requires_auth
def backtest_report():
    docs = os.path.join(os.path.dirname(APP_DIR), "docs")
    if not os.path.exists(os.path.join(docs, "backtest.html")):
        return Response("아직 백테스트 결과가 없습니다. python backtest.py 를 먼저 실행하세요.", 404)
    return send_from_directory(docs, "backtest.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(
        app.static_folder, "manifest.json", mimetype="application/manifest+json"
    )


@app.route("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(app.static_folder, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    if not WEBAPP_PASSWORD:
        print("[경고] WEBAPP_PASSWORD 가 설정되어 있지 않습니다. 외부에 공개된 서버라면 "
              "누구나 결과를 볼 수 있으니, 배포 시 반드시 설정하세요.")
    app.run(host="0.0.0.0", port=port)
