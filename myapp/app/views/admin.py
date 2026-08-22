# app/views/admin.py
# 관리자 전용 대시보드 블루프린트. 이벤트 통계와 현재 설정을 한눈에 보여준다.
# url_prefix="/admin" 이며, 로그인만으로는 부족하고 관리자 계정이어야 들어올 수 있다.

from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    session,
    flash,
    current_app,
    abort,
    request,
)

from app import event_store

admin_bp = Blueprint("admin", __name__)


@admin_bp.before_request
def require_admin():
    """로그인 + 관리자 권한을 둘 다 확인한다.

    앞의 agent/alarm 블루프린트는 '로그인했는가'만 봤다.
    여기서는 한 단계 더 나아가 '누구인가'까지 본다.
    이렇게 블루프린트마다 접근 정책을 다르게 걸 수 있다는 게 도메인을 나누는 이점이다.
    """
    username = session.get("username")

    # 로그인 자체를 안 했으면 로그인 페이지로 안내한다.
    if not username:
        flash("관리자 페이지는 로그인이 필요합니다.", "error")
        return redirect(url_for("auth.login"))

    # 로그인은 했지만 권한이 없는 경우.
    # 이때는 로그인 페이지로 보내면 안 된다(로그인해도 해결되지 않으므로).
    # 403 Forbidden 이 맞는 응답이다.
    if username not in current_app.config["ADMIN_USERS"]:
        abort(403)


# 최종 URL: /admin/
@admin_bp.route("/")
def index():
    """관리자 대시보드."""
    cfg = current_app.config

    # 화면에 뿌릴 설정 요약.
    # 비밀 값(API 키, AWS 시크릿)은 '설정됨/미설정'만 보여주고 값 자체는 절대 노출하지 않는다.
    settings = [
        ("실행 환경", cfg["CONFIG_NAME"], None),
        ("디버그 모드", "켜짐" if cfg["DEBUG"] else "꺼짐", cfg["DEBUG"]),
        ("에이전트 경로", cfg["AGENT_PROVIDER"], None),
        ("에이전트 모델", cfg["AGENT_MODEL"], None),
        ("Anthropic 키", "설정됨" if cfg["ANTHROPIC_API_KEY"] else "미설정", not cfg["ANTHROPIC_API_KEY"]),
        ("Lambda 모드", cfg["LAMBDA_MODE"], None),
        ("Lambda 함수명", cfg["LAMBDA_FUNCTION_NAME"], None),
        ("Lambda 호출 방식", cfg["LAMBDA_INVOCATION_TYPE"], None),
        ("AWS 리전", cfg["AWS_REGION"], None),
        ("AWS 액세스 키", "설정됨" if cfg["AWS_ACCESS_KEY_ID"] else "미설정(IAM 역할 사용)", None),
        ("DB 접속", cfg["SQLALCHEMY_DATABASE_URI"].split("@")[-1], None),
    ]

    return render_template(
        "admin.html",
        stats=event_store.stats(),
        events=event_store.recent(10),
        settings=settings,
        routes=sorted(
            (r.rule, r.endpoint, ",".join(sorted(r.methods - {"HEAD", "OPTIONS"})))
            for r in current_app.url_map.iter_rules()
            if r.endpoint != "static"
        ),
    )


# 최종 URL: /admin/events/clear
@admin_bp.route("/events/clear", methods=["POST"])
def clear_events():
    """화면용 이벤트 사본을 비운다(Lambda 가 DB 에 넣은 원본은 그대로 남는다)."""
    event_store.clear()
    flash("이벤트 목록을 비웠습니다.", "success")
    return redirect(url_for("admin.index"))


# 최종 URL: /admin/audit
# 감사 로그는 관리자만 본다. 새 블루프린트를 만들지 않고 admin 에 붙인 이유는
# 접근 정책이 이미 여기와 같기 때문이다 - before_request 하나를 그대로 쓴다.
@admin_bp.route("/audit")
def audit_log():
    """고객사 계정을 건드린 기록."""
    from app import audit
    from app.audit import AuditError, OUTCOMES, ACTIONS, ACTOR_KINDS, ALERT_OUTCOMES

    # 필터는 허용 목록 안의 값만 받는다. 그대로 SQL 로 가지는 않지만
    # (%s 로 나간다), 알 수 없는 값으로 빈 화면을 내는 것보다 무시가 낫다.
    outcome = request.args.get("outcome") or None
    actor_kind = request.args.get("actor_kind") or None
    if outcome not in OUTCOMES:
        outcome = None
    if actor_kind not in ACTOR_KINDS:
        actor_kind = None

    items, total, error = [], None, None
    try:
        items = audit.recent(200, outcome=outcome, actor_kind=actor_kind)
        total = audit.summary()
    except AuditError as e:
        error = str(e)

    return render_template(
        "admin_audit.html",
        items=items, total=total, error=error,
        outcome=outcome, actor_kind=actor_kind,
        outcomes=OUTCOMES, actions=ACTIONS, actor_kinds=ACTOR_KINDS,
        alerts=ALERT_OUTCOMES,
    )
