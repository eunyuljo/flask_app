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

from app import event_store
from app.lambda_client import invoke_normalizer, LambdaInvokeError

alarm_bp = Blueprint("alarm", __name__)


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
    return render_template(
        "alarm.html",
        events=event_store.recent(20),
        mode=current_app.config["LAMBDA_MODE"],
        function_name=current_app.config["LAMBDA_FUNCTION_NAME"],
        invocation_type=current_app.config["LAMBDA_INVOCATION_TYPE"],
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
