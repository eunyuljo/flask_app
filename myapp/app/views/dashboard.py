# app/views/dashboard.py
# 이벤트 지표 대시보드 블루프린트. PostgreSQL 을 집계해서 보여준다.
# app/__init__.py 에서 url_prefix="/dashboard" 로 등록된다.

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)

from app.stats import collect, StatsUnavailable

dashboard_bp = Blueprint("dashboard", __name__)

# 조회 범위로 허용할 시간. 사용자가 준 값을 그대로 SQL 에 넣지 않고
# 이 목록에 있는지 먼저 확인한다(허용 목록 방식).
ALLOWED_HOURS = (6, 24, 72)


@dashboard_bp.before_request
def require_login():
    """대시보드는 로그인한 사용자만 볼 수 있다."""
    if not session.get("username"):
        flash("대시보드를 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


# 최종 URL: /dashboard/
@dashboard_bp.route("/")
def index():
    """집계 결과를 모아 대시보드를 그린다."""
    # 쿼리스트링 ?hours=6 처리. 숫자가 아니거나 허용 목록에 없으면 기본값 24 를 쓴다.
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    if hours not in ALLOWED_HOURS:
        hours = 24

    try:
        data = collect(hours=hours)
        problem = None
    except StatsUnavailable as e:
        # DB 가 없어도 페이지 자체는 뜨고, 무엇을 해야 하는지 알려준다.
        data = None
        problem = str(e)

    return render_template(
        "dashboard.html",
        data=data,
        problem=problem,
        hours=hours,
        allowed_hours=ALLOWED_HOURS,
    )
