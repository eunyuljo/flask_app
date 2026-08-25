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
from app.event_store import EventStoreError
from app.accounts import list_accounts, by_customer, get_account, AccountError
from app.agent_core import diagnose as run_diagnose, AgentNotConfigured
from app.lambda_client import invoke_normalizer, LambdaInvokeError
from app.incident import past_for_many, past_incidents, IncidentError
from app import handoff
from app.runbook import find_many, find as find_runbook, RunbookError, OUTCOMES
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

    # 기본값을 '미확인만' 으로 두지 않는다. 목록을 열었는데 아무것도 없으면
    # 알람이 안 들어오는 것과 구분이 안 된다. 대신 미확인 수를 위에 크게 띄운다.
    unacked_only = request.args.get("unacked") == "1"
    severity = request.args.get("severity", "")
    if severity not in ("critical", "error", "warning", "info"):
        severity = ""

    events, store_error, unacked = [], None, None
    try:
        events = event_store.recent(
            20, unacked_only=unacked_only, severity=severity or None
        )
        unacked = event_store.unacked_count()
    except EventStoreError as e:
        # DB 가 없으면 목록은 못 보여주지만 제출 폼은 떠야 한다.
        store_error = str(e)

    # 어댑터가 못 읽은 발신자. 목록 안에 섞여 있으면 20건 밖으로 밀려
    # 아무도 못 보므로 위에 따로 세워 둔다. 못 읽은 알람이 조용히 쌓이는
    # 것이 이 화면에서 제일 위험한 상태다.
    unparsed = None
    try:
        unparsed = event_store.unparsed_summary()
    except EventStoreError:
        # 목록도 못 읽는 상황이면 이미 위에서 store_error 로 말했다.
        pass

    # 이벤트마다 find() 를 부르면 20건에 질의가 20번 나간다. 한 번에 가져온다.
    fingerprints = [e["record"].get("fingerprint", "") for e in events]

    runbooks = {}
    try:
        runbooks = find_many(fingerprints)
    except RunbookError:
        # 런북은 없어도 알람 화면은 떠야 한다. 조용히 건너뛴다.
        pass

    # 이 알람 종류가 관련됐던 지난 장애. 확정된 사후 보고서가 쌓일수록
    # 이 자리가 채워진다.
    past = {}
    try:
        past = past_for_many(fingerprints)
    except IncidentError:
        pass

    # 알람에서 작업/장애로 넘어갈 때 들고 갈 값. 링크를 템플릿에서
    # 조립하면 받는 쪽이 무엇을 기대하는지 HTML 안에 숨는다.
    handoffs = {
        e["record"]["event_id"]: {
            "work": handoff.for_work(e["record"]),
            "incident": handoff.for_incident(e["record"]),
        }
        for e in events
    }

    return render_template(
        "alarm.html",
        events=events,
        runbooks=runbooks,
        handoffs=handoffs,
        outcomes=OUTCOMES,
        # 런북 실행을 기록할 때 어느 고객사 일이었는지 함께 남기기 위한 표.
        # 이벤트에는 계정만 있고 고객사가 없다.
        customer_of={a["account_id"]: a.get("customer", "") for a in accounts},
        past=past,
        mode=current_app.config["LAMBDA_MODE"],
        function_name=current_app.config["LAMBDA_FUNCTION_NAME"],
        invocation_type=current_app.config["LAMBDA_INVOCATION_TYPE"],
        grouped=by_customer(accounts),
        account_error=account_error,
        store_error=store_error,
        diagnosed=diagnosed,
        diagnoses=_DIAGNOSES,
        unacked=unacked,
        unparsed=unparsed,
        unacked_only=unacked_only,
        severity=severity,
    )


def _process(payload, invocation_type=None):
    """Lambda 를 호출하고 결과를 저장소에 기록한다. 뷰 두 곳에서 함께 쓴다."""
    result = invoke_normalizer(payload, invocation_type)

    # 비동기 호출은 결과 본문이 없으므로 기록할 레코드도 없다.
    if result.get("async"):
        return result

    # 예전에는 여기서 메모리 저장소에도 사본을 넣었다. 이제 목록이 DB 를
    # 읽으므로 필요 없다 - Lambda 가 이미 적재했다.
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

    # 계정을 실어 보낸다. 이게 없으면 이 이벤트는 어느 고객사에도 안 잡힌다 -
    # 고객사 현황의 알람 칸, SLA, 리포트, 노이즈의 고객사 필터가 전부
    # account_id 로 묶는다. 화면에서 넣은 알람만 그 집계에서 빠지는 것은
    # 화면을 못 믿게 만드는 종류의 구멍이다.
    #
    # 등록된 계정 중에서만 고르게 한다(폼이 select 다). 손으로 치게 두면
    # 오타가 조용히 미귀속을 만든다.
    account_id = request.form.get("account_id", "").strip()
    if account_id:
        payload["account_id"] = account_id

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

        # 목록은 이제 DB 를 읽는다. 적재를 건너뛰었으면 아래에 나타나지 않는다.
        # 이걸 안 알려주면 "보냈는데 왜 안 보이지" 로 헤매게 된다.
        store = result.get("store", {})
        if not store.get("stored"):
            flash(
                f"이 이벤트는 DB 에 적재되지 않아 아래 목록에 나타나지 않습니다 "
                f"({store.get('reason', '이유 미상')}). "
                "Lambda 쪽 DATABASE_URL 을 설정하세요.",
                "error",
            )
    else:
        flash(f"정규화 실패: {result.get('error')}", "error")

    return redirect(url_for("alarm.index"))


def _ingest_allowed():
    """알람 수집 API 를 부를 자격이 있는가.

    키를 하나도 설정하지 않았으면 열어둔다. 이 앱은 설정하지 않은 기능
    때문에 앱이 뜨지 않는 것을 피하려고 여기저기서 그렇게 하고 있다.
    다만 이 자리는 열어두면 아무나 알람을 넣어 당직자를 깨울 수 있어서,
    관리자 화면에 경고를 띄우고 앱 로그에도 남긴다.

    비교에 hmac.compare_digest 를 쓰는 이유: `==` 는 앞에서부터 비교하다
    다른 글자가 나오면 바로 멈춰서, 걸린 시간으로 키를 한 글자씩
    맞춰볼 여지를 준다. compare_digest 는 길이가 같으면 항상 같은 시간이 걸린다.
    """
    import hmac

    keys = current_app.config["INGEST_API_KEYS"]
    if not keys:
        current_app.logger.warning(
            "INGEST_API_KEYS 가 비어 있어 알람 수집 API 가 인증 없이 열려 있습니다"
        )
        return True

    # 표준 헤더가 없는 영역이라 둘 다 받는다.
    # Authorization: Bearer <키>  /  X-API-Key: <키>
    sent = request.headers.get("X-API-Key", "")
    if not sent:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            sent = auth[7:].strip()
    if not sent:
        return False

    # 양쪽을 바이트로 맞춰서 비교한다. 두 가지 이유가 겹쳐 있다.
    #
    # 1. compare_digest 는 str 을 받으면 ASCII 만 허용한다(아니면 TypeError).
    # 2. 헤더 값은 WSGI 규약에 따라 latin-1 로 디코딩되어 들어온다. 키에
    #    ASCII 가 아닌 글자가 있으면 여기서 글자가 깨진 채 도착한다.
    #    latin-1 로 다시 인코딩하면 브라우저가 보낸 원래 바이트가 나온다.
    #    (설정값 쪽은 os.environ 이 UTF-8 로 디코딩해 둔 것이라 utf-8 로 되돌린다.)
    sent_b = sent.encode("latin-1", "replace")
    return any(hmac.compare_digest(sent_b, k.encode("utf-8")) for k in keys)


# 최종 URL: /alarm/api/events  (다른 서버가 호출하는 JSON API)
@alarm_bp.route("/api/events", methods=["POST"])
def ingest():
    """외부 시스템이 JSON 으로 이벤트를 보내는 입구.

    화면용 라우트와 같은 블루프린트에 두되, 응답은 HTML 이 아니라 JSON 으로 돌려준다.
    브라우저 세션이 아니라 다른 서버가 부르는 곳이라 로그인 대신 API 키로 막는다.
    """
    if not _ingest_allowed():
        # 401: 인증이 필요한데 없거나 틀렸다는 뜻.
        # 어떤 키가 맞는지에 대한 힌트는 주지 않는다.
        return jsonify({"ok": False, "error": "인증이 필요합니다."}), 401

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
        flash("진단할 이벤트를 찾지 못했습니다.", "error")
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

    # 같은 알람이 관련됐던 지난 장애. 원인이 적힌 것만 온다.
    # 런북이 "이럴 땐 이렇게 하세요" 라면 이건 "지난번엔 이게 원인이었다" 다.
    try:
        history_incidents = past_incidents(event["fingerprint"], limit=3)
    except IncidentError:
        history_incidents = []

    try:
        text, commands = run_diagnose(event, account, region, history, book,
                                      history_incidents)
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


# 최종 URL: /alarm/<이벤트>/ack
@alarm_bp.route("/<event_id>/ack", methods=["POST"])
def ack(event_id):
    """이 알람을 확인했다고 표시한다.

    SLA 의 '최초 대응 시각' 이 여기서 나온다. 예전에는 감사 로그에서
    추론했는데, 그건 '그 계정을 들여다봤다' 이지 '이 알람을 처리했다' 가
    아니었다.
    """
    try:
        event_store.acknowledge(event_id, session.get("username", ""))
        flash("확인했습니다.", "success")
    except EventStoreError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("alarm.index"))


@alarm_bp.route("/<event_id>/unack", methods=["POST"])
def unack(event_id):
    """확인을 되돌린다. 잘못 눌렀을 때."""
    try:
        event_store.unacknowledge(event_id)
        flash("확인을 되돌렸습니다.", "success")
    except EventStoreError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("alarm.index"))
