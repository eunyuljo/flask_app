# app/health.py
# 연동이 실제로 되는가.
#
# ── 설정됨 ≠ 작동함 ─────────────────────────────────────────────────
# 관리자 화면은 Slack/Jira 를 "설정됨 / 미설정" 으로만 보여줬다. 설정됐다고
# 되는 게 아니다. 토큰이 만료되고, 웹훅이 지워지고, 채널에서 앱이 빠진다.
# 그런 일은 SLA 위반 알림이 안 갈 때 알게 된다 - 가장 나쁜 때다.
#
# ── 눌러서 확인한다 ─────────────────────────────────────────────────
# 화면을 열 때마다 자동으로 부르지 않는다. Slack 에 메시지를 보내고 Jira 를
# 두드리는 일이라, 화면 새로고침 한 번에 그게 나가면 안 된다.
# 사람이 버튼을 눌렀을 때만 나간다.
#
# ── 보내는 것은 최소한으로 ──────────────────────────────────────────
# Slack 은 실제로 메시지를 보내야 확인이 된다(웹훅은 조회 API 가 없다).
# 그래서 "점검 중" 이라고 밝히는 짧은 메시지를 보낸다. Jira 는 이슈를
# 만들지 않고 내 계정 정보만 읽는다 - 점검 때문에 티켓이 쌓이면 안 된다.

import time

from flask import current_app

from app import db


def _timed(fn):
    """점검 하나를 돌리고 (성공 여부, 설명, 걸린 시간) 을 돌려준다.

    예외를 여기서 잡는다. 점검 하나가 터졌다고 화면 전체가 500 이 되면,
    정작 다른 연동이 멀쩡한지도 못 보게 된다.

    감싸는 자리가 여기라는 게 중요하다. 각 점검 함수가 스스로 try 를 쓰면,
    나중에 추가하는 사람이 빠뜨렸을 때 조용히 깨진다. 되는데 느린 것도
    문제라서 시간도 함께 잰다.
    """
    started = time.time()
    try:
        ok, detail = fn()
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}",
                "ms": round((time.time() - started) * 1000)}
    return {"ok": bool(ok), "detail": str(detail),
            "ms": round((time.time() - started) * 1000)}


# ----------------------------------------------------------------------
# 개별 점검
# ----------------------------------------------------------------------

def check_db():
    """DB 에 붙고 스키마가 있는가."""
    import psycopg

    # db.connect() 를 쓰지 않는다. 두 가지가 다르다.
    #   * 여기는 타임아웃이 필요하다. 점검이 걸려서 안 끝나면 '운영 상태'
    #     화면 전체가 멈춘다 - 죽었는지 보러 온 화면이 같이 죽는 셈이다.
    #   * 여기는 예외를 도메인 에러로 바꾸면 안 된다. 부르는 쪽이 원래
    #     예외를 받아서 '무엇이 왜 안 되는가' 로 만든다.
    # 접속 문자열만 db.uri() 에서 가져온다.
    with psycopg.connect(db.uri(), connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.events')")
        if cur.fetchone()[0] is None:
            return False, "붙었지만 events 테이블이 없습니다. init-db 가 필요합니다."
        cur.execute("SELECT count(*) FROM events")
        return True, f"이벤트 {cur.fetchone()[0]}건"


def check_lambda():
    """이벤트 정규화 경로가 도는가.

    local 모드면 핸들러를 그대로 부른다. aws 모드면 실제 Lambda 를
    부르는 대신 설정만 본다 - 점검 때문에 가짜 이벤트가 적재되면 안 된다.
    """
    mode = current_app.config["LAMBDA_MODE"]
    if mode != "local":
        name = current_app.config["LAMBDA_FUNCTION_NAME"]
        if not name:
            return False, "LAMBDA_FUNCTION_NAME 이 비어 있습니다."
        return True, (
            f"aws 모드({name}). 실제 호출은 하지 않았습니다 - "
            "점검 때문에 가짜 이벤트가 적재되면 안 됩니다."
        )

    from api.normalize_handler import normalize

    record = normalize({"message": "연동 점검", "severity": "info",
                        "source": "health-check"})
    if record.get("severity") != "info":
        return False, f"정규화 결과가 이상합니다: {record}"
    return True, f"local 모드. 지문 {record.get('fingerprint', '')[:12]}"


def check_slack():
    """Slack 웹훅이 살아 있는가.

    웹훅은 조회 API 가 없어서 실제로 보내야 확인이 된다. 받는 사람이
    무엇인지 알 수 있게 점검이라고 밝히는 짧은 메시지를 보낸다.
    """
    from app import slack
    from app.slack import SlackError, SlackNotConfigured

    try:
        slack.post(
            "🔧 연동 점검입니다. 이 메시지는 관리자 화면에서 "
            "누군가 '연동 점검' 을 눌러서 나갔습니다.",
            purpose="sla",
        )
    except SlackNotConfigured as e:
        return False, str(e)
    except SlackError as e:
        return False, str(e)
    return True, "메시지를 보냈습니다. 채널에 도착했는지 확인하세요."


def check_jira():
    """Jira 자격증명이 살아 있는가.

    이슈를 만들지 않는다. 점검 때문에 티켓이 쌓이면 아무도 이 버튼을
    안 누르게 된다. 대신 '내 계정' 을 읽어서 인증만 확인한다.
    """
    import base64
    import json
    import urllib.error
    import urllib.request

    from app import jira

    if not jira.is_configured():
        return False, "Jira 설정이 없습니다: " + ", ".join(jira._missing())

    c = jira._config()
    credential = base64.b64encode(
        f"{c['email']}:{c['token']}".encode("utf-8")
    ).decode("ascii")
    request = urllib.request.Request(
        f"{c['base_url']}/rest/api/3/myself",
        headers={"Accept": "application/json",
                 "Authorization": f"Basic {credential}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            who = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"인증에 실패했습니다({e.code}). 토큰이 만료됐을 수 있습니다."
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"연결하지 못했습니다: {e.reason}"

    return True, (
        f"{who.get('displayName', '?')} 로 인증됨 · "
        f"프로젝트 {c['project']}"
    )


def check_agent():
    """AI 경로 설정이 갖춰졌는가.

    모델을 실제로 부르지는 않는다. 점검 한 번에 토큰 비용이 나가면
    사람들이 안 누른다. 설정만 본다.
    """
    from app.agent_core import check_config

    problem = check_config()
    if problem:
        return False, problem
    cfg = current_app.config
    return True, (
        f"{cfg['AGENT_PROVIDER']} / {cfg['AGENT_MODEL']}. "
        "실제 호출은 하지 않았습니다 - 점검 한 번에 비용이 나가면 안 됩니다."
    )


def check_aws():
    """AWS CLI 가 설치되어 있는가.

    자격증명까지는 보지 않는다. 그건 고객사 계정마다 다르고,
    콘솔 화면에서 계정을 고를 때 확인된다.
    """
    import shutil
    import subprocess

    path = shutil.which("aws")
    if not path:
        return False, (
            "aws 명령을 찾을 수 없습니다. 콘솔 화면이 exec_failed 로만 끝납니다."
        )
    try:
        proc = subprocess.run([path, "--version"], capture_output=True,
                              text=True, timeout=15, shell=False)
    except subprocess.TimeoutExpired:
        return False, "aws --version 이 15초 안에 끝나지 않았습니다."
    version = (proc.stdout or proc.stderr or "").strip().split("\n")[0]
    return True, version or path

    return _timed(run)


# 점검 항목. 화면에서 하나씩 눌러서 돌린다.
#   sends: 바깥으로 무언가를 보내는가. 보내는 것은 화면에서 미리 알린다.
CHECKS = [
    {"id": "db", "label": "데이터베이스", "fn": check_db, "sends": False,
     "why": "이벤트·작업·장애가 전부 여기 있습니다."},
    {"id": "lambda", "label": "이벤트 정규화", "fn": check_lambda, "sends": False,
     "why": "알람이 들어오는 입구입니다."},
    {"id": "agent", "label": "AI 경로", "fn": check_agent, "sends": False,
     "why": "알람 진단과 보고서 초안이 이걸 씁니다."},
    {"id": "aws", "label": "AWS CLI", "fn": check_aws, "sends": False,
     "why": "콘솔 화면이 이 명령을 실행합니다."},
    {"id": "slack", "label": "Slack", "fn": check_slack, "sends": True,
     "why": "SLA 위반과 당직 호출이 이 길로 나갑니다."},
    {"id": "jira", "label": "Jira", "fn": check_jira, "sends": False,
     "why": "에스컬레이션 티켓과 사후 보고서가 이 길로 나갑니다."},
]

CHECKS_BY_ID = {c["id"]: c for c in CHECKS}


def run_check(check_id):
    """하나만 돌린다. 없는 항목이면 None.

    _timed 로 감싸는 자리가 여기 하나뿐이다. 점검 함수는 (성공 여부, 설명)
    만 돌려주면 되고, 예외 처리와 시간 재기는 신경 쓰지 않는다.
    """
    check = CHECKS_BY_ID.get(check_id)
    if check is None:
        return None
    return {**check, "result": _timed(check["fn"])}
