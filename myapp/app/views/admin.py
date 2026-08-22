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

from app import audit, event_store, users

admin_bp = Blueprint("admin", __name__)


@admin_bp.before_request
def require_admin():
    """로그인 + 관리자 권한을 둘 다 확인한다.

    앞의 agent/alarm 블루프린트는 '로그인했는가'만 봤다.
    여기서는 한 단계 더 나아가 '누구인가'까지 본다.
    이렇게 블루프린트마다 접근 정책을 다르게 걸 수 있다는 게 도메인을 나누는 이점이다.
    """
    # 로그인 자체를 안 했으면 로그인 페이지로 안내한다.
    if not session.get("username"):
        flash("관리자 페이지는 로그인이 필요합니다.", "error")
        return redirect(url_for("auth.login"))

    # 로그인은 했지만 권한이 없는 경우.
    # 이때는 로그인 페이지로 보내면 안 된다(로그인해도 해결되지 않으므로).
    # 403 Forbidden 이 맞는 응답이다.
    #
    # 권한은 계정의 역할(role)로 판단한다. 예전에는 설정 파일에 적어둔
    # 이름 목록(ADMIN_USERS)과 비교했는데, 계정과 권한이 서로 다른 곳에
    # 저장되어 있어서 계정을 지워도 권한이 남는 문제가 있었다.
    if not users.can(session.get("role"), "admin"):
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

    # 이벤트는 DB 에서 읽는다. DB 가 없어도 나머지(설정, 라우트)는 보여야 한다.
    stats, events, store_error = None, [], None
    try:
        stats = event_store.stats()
        events = event_store.recent(10)
    except event_store.EventStoreError as e:
        store_error = str(e)

    return render_template(
        "admin.html",
        stats=stats,
        events=events,
        store_error=store_error,
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


# 최종 URL: /admin/users
# 계정 관리도 접근 정책이 같아서 admin 블루프린트에 붙였다.
@admin_bp.route("/users")
def users_page():
    """계정 목록."""
    items, error = [], None
    try:
        items = users.listing()
    except users.UserError as e:
        error = str(e)

    return render_template(
        "admin_users.html",
        items=items,
        error=error,
        roles=users.ROLES,
        me=session.get("username"),
    )


@admin_bp.route("/users/create", methods=["POST"])
def user_create():
    """계정을 만든다."""
    username = request.form.get("username", "")
    try:
        users.create(
            username,
            request.form.get("password", ""),
            request.form.get("role", "operator"),
        )
    except users.UserError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.users_page"))

    audit.record(
        action="user_manage", outcome="ok",
        summary=f"계정 생성: {username.strip()}",
    )
    # 계정이 생겼으면 더 이상 부트스트랩이 아니다. 세션에 남은 표시를 지워
    # 경고 배너가 사라지게 한다(다음 요청부터 반영된다).
    session.pop("bootstrap", None)
    flash(f"계정을 만들었습니다: {username.strip()}", "success")
    return redirect(url_for("admin.users_page"))


# 사용자 이름을 URL 이 아니라 폼 본문으로 받는다.
# 이름에 "/" 나 한글이 들어가면 URL 에서 다루기가 번거롭고
# (경로 변환기는 "/" 를 값으로 받지 않는다), 이름이 서버 로그에 남는다.
@admin_bp.route("/users/password", methods=["POST"])
def user_password():
    """비밀번호를 바꾼다."""
    username = request.form.get("username", "")
    try:
        users.set_password(username, request.form.get("password", ""))
    except users.UserError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.users_page"))

    audit.record(
        action="user_manage", outcome="ok",
        summary=f"비밀번호 변경: {username}",
    )
    flash(f"{username} 의 비밀번호를 바꿨습니다.", "success")
    return redirect(url_for("admin.users_page"))


@admin_bp.route("/users/enabled", methods=["POST"])
def user_enabled():
    """계정을 켜거나 끈다."""
    username = request.form.get("username", "")
    enabled = request.form.get("enabled") == "1"
    try:
        users.set_enabled(username, enabled)
    except users.UserError as e:
        flash(str(e), "error")
        return redirect(url_for("admin.users_page"))

    audit.record(
        action="user_manage", outcome="ok",
        summary=f"계정 {'사용' if enabled else '중지'}: {username}",
    )
    flash(f"{username} 계정을 {'켰습니다' if enabled else '껐습니다'}.", "success")
    return redirect(url_for("admin.users_page"))
