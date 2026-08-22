# app/views/alarm.py
# 이벤트 접수/알람 도메인을 담당하는 블루프린트. 사용자가 이벤트를 제출하면 Lambda 로 넘겨
# 정규화 + DB 적재 + 알람 발송을 시키고, 그 결과를 화면에 보여준다. url_prefix="/alarm".

import json

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    current_app,
    jsonify,
)

from app import audit, event_store
from app.accounts import list_accounts, by_customer, get_account, AccountError
from app.agent_core import diagnose as run_diagnose, AgentNotConfigured
from app.lambda_client import invoke_normalizer, LambdaInvokeError
from app.runbook import find_many, find as find_runbook, RunbookError
from app.stats import fingerprint_history, StatsUnavailable

alarm_bp = Blueprint("alarm", __name__)

# 진단 결과 보관소. 화면에 보여주기 위한 것뿐이라 메모리에 둔다.
#   event_id -> {"text": ..., "commands": [...], "account": ..., "region": ...}
# POST 로 진단하고 GET 으로 결과를 보여준다(PRG). 새로고침이 진단을 다시
# 실행하면 그때마다 모델 호출 비용이 나가기 때문이다.
_DIAGNOSES = {}


@alarm_bp.before_request
def require_login():
    """이벤트 제출/조회는 로그인한 사용자만."""
    # API 엔드포인트는 브라우저 세션이 아니라 다른 서버가 부르는 곳이라 검사에서 뺀다.
    # request.endpoint 는 "alarm.ingest" 처럼 블루프린트 이름이 붙은 형태다.
    if request.endpoint == "alarm.ingest":
        return None
    if not session.get("username"):
        flash("로그인이 필요합니다.", "error")
        return redirect(url_for("auth.login"))


# 최종 URL: /alarm/
@alarm_bp.route("/")
def index():
    """이벤트 제출 폼과 최근 처리 결과를 보여준다."""
    # 진단 대상 계정 목록. DB 가 없으면 진단 버튼을 숨긴다.
    accounts, account_error = [], None
    try:
        accounts = list_accounts()
    except AccountError as e:
        account_error = str(e)

    # 방금 진단한 결과가 있으면 그 이벤트에만 펼쳐서 보여준다.
    diagnosed = request.args.get("diagnosed", "")

    events = event_store.recent(20)

    # 이벤트마다 find() 를 부르면 20건에 질의가 20번 나간다. 한 번에 가져온다.
    runbooks = {}
    try:
        runbooks = find_many([e["record"].get("fingerprint", "") for e in events])
    except RunbookError:
        # 런북은 없어도 알람 화면은 떠야 한다. 조용히 건너뛴다.
        pass

    return render_template(
        "alarm.html",
        events=events,
        runbooks=runbooks,
        mode=current_app.config["LAMBDA_MODE"],
        function_name=current_app.config["LAMBDA_FUNCTION_NAME"],
        invocation_type=current_app.config["LAMBDA_INVOCATION_TYPE"],
        grouped=by_customer(accounts),
        account_error=account_error,
        diagnosed=diagnosed,
        diagnoses=_DIAGNOSES,
    )


def _process(payload, invocation_type=None):
    """Lambda 를 호출하고 결과를 저장소에 기록한다. 뷰 두 곳에서 함께 쓴다."""
    result = invoke_normalizer(payload, invocation_type)

    # 비동기 호출은 결과 본문이 없으므로 기록할 레코드도 없다.
    if result.get("async"):
        return result

    if result.get("ok"):
        event_store.add(
            result["record"],
            {"store": result.get("store", {}), "alarm": result.get("alarm", {})},
        )
    return result


# 최종 URL: /alarm/send  (화면의 폼에서 POST)
@alarm_bp.route("/send", methods=["POST"])
def send():
    """폼으로 들어온 이벤트를 Lambda 에 넘긴다."""
    message = request.form.get("message", "").strip()
    if not message:
        flash("메시지를 입력해 주세요.", "error")
        return redirect(url_for("alarm.index"))

    # 일부러 별칭 필드명(msg/level/service)으로 보낸다.
    # 정규화가 실제로 무슨 일을 하는지 화면에서 비교해 볼 수 있도록 한 것이다.
    payload = {
        "msg": message,
        "level": request.form.get("severity", "info"),
        "service": request.form.get("source", "web"),
        "type": request.form.get("event_type", "manual"),
        "submitted_by": session.get("username"),
    }

    try:
        result = _process(payload)
    except LambdaInvokeError as e:
        flash(f"Lambda 호출에 실패했습니다: {e}", "error")
        return redirect(url_for("alarm.index"))

    if result.get("async"):
        flash("비동기로 전달했습니다. 결과는 CloudWatch 로그에서 확인하세요.", "success")
    elif result.get("ok"):
        record = result["record"]
        alarm = result.get("alarm", {})
        note = "알람 발송됨" if alarm.get("alarmed") else alarm.get("reason", "알람 없음")
        flash(f"처리 완료 - 심각도 {record['severity']} / {note}", "success")
    else:
        flash(f"정규화 실패: {result.get('error')}", "error")

    return redirect(url_for("alarm.index"))


# 최종 URL: /alarm/api/events  (다른 서버가 호출하는 JSON API)
@alarm_bp.route("/api/events", methods=["POST"])
def ingest():
    """외부 시스템이 JSON 으로 이벤트를 보내는 입구.

    화면용 라우트와 같은 블루프린트에 두되, 응답은 HTML 이 아니라 JSON 으로 돌려준다.
    실제 서비스라면 여기에 API 키 검증을 붙여야 한다(지금은 학습용이라 열려 있다).
    """
    # silent=True 는 본문이 JSON 이 아닐 때 예외 대신 None 을 돌려준다.
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"ok": False, "error": "JSON 본문이 필요합니다."}), 400

    try:
        result = _process(payload)
    except LambdaInvokeError as e:
        # 502: 우리 잘못이 아니라 뒤에 있는 서비스(Lambda)가 응답하지 못한 상황
        return jsonify({"ok": False, "error": str(e)}), 502

    return jsonify(result), (200 if result.get("ok") else 400)


def _audit_diagnosis(event, account, region, commands, outcome):
    """진단 한 건을 감사 기록으로 남긴다.

    명령 하나하나가 아니라 '진단 한 번' 을 한 건으로 남긴다. 고객사 계정을
    건드린 행위이므로 콘솔에서 사람이 직접 친 명령과 같은 무게로 기록하되,
    actor_kind 로 사람과 모델을 구분한다. 이 구분이 없으면 나중에 감사
    로그에서 "모델이 무엇을 조회했나" 를 분리해낼 수 없다.
    """
    audit.record(
        action="ai_diagnose",
        outcome=outcome,
        summary=f"AI 진단: {event.get('message', '')[:120]}",
        detail=f"조회 {len(commands)}건",
        account=account,
        region=region,
        actor_kind="agent",
        meta={
            "target_event_id": event.get("event_id"),
            "fingerprint": event.get("fingerprint"),
            "commands": commands,
        },
    )


# 최종 URL: /alarm/diagnose
@alarm_bp.route("/diagnose", methods=["POST"])
def diagnose():
    """알람 하나를 AI 로 진단한다.

    조회 범위는 담당자가 고른 계정 하나뿐이다. 대화가 아니라 요청 한 번이므로
    앞선 진단의 내용이 다음 진단에 남지 않는다.
    """
    event_id = request.form.get("event_id", "")
    account_id = request.form.get("account_id", "")
    region = request.form.get("region", "")

    item = event_store.get(event_id)
    if item is None:
        flash("진단할 이벤트를 찾지 못했습니다. 목록이 밀려났을 수 있습니다.", "error")
        return redirect(url_for("alarm.index"))
    event = item["record"]

    try:
        account = get_account(account_id)
    except AccountError as e:
        flash(str(e), "error")
        return redirect(url_for("alarm.index"))

    if account is None:
        flash("계정을 고르세요.", "error")
        return redirect(url_for("alarm.index"))

    # 리전은 그 계정에 등록된 것 중에서만 고를 수 있다.
    regions = account.get("regions") or []
    if region not in regions:
        region = regions[0] if regions else ""

    # 발생 이력. DB 가 없어도 진단 자체는 진행하되, 이력이 없다는 사실을 모델에 알린다.
    try:
        history = fingerprint_history(event["fingerprint"])
    except StatsUnavailable:
        history = None

    # 이 알람에 등록된 대응 절차. 있으면 모델이 일반론 대신 이 절차를 따른다.
    # 고객사 전용 절차가 있으면 그것을, 없으면 공통 절차를 쓴다.
    try:
        book = find_runbook(event["fingerprint"], account["customer"])
    except RunbookError:
        book = None

    try:
        text, commands = run_diagnose(event, account, region, history, book)
    except AgentNotConfigured as e:
        flash(f"진단을 실행할 수 없습니다: {e}", "error")
        return redirect(url_for("alarm.index"))

    _DIAGNOSES[event_id] = {
        "text": text,
        "commands": commands,
        "account": account,
        "region": region,
        "history": history,
    }
    _audit_diagnosis(event, account, region, commands, "ok")

    return redirect(url_for("alarm.index", diagnosed=event_id))
