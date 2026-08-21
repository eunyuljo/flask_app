# app/views/handover.py
# 당직 인계 블루프린트. url_prefix="/handover" 로 등록된다.
# 리포트(고객사용, 월 단위)와 달리 내부용이고 시간 단위라 따로 뒀다.

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, Response
)

from app.handover import collect, to_markdown, HandoverError, WORK_LABEL

handover_bp = Blueprint("handover", __name__)

# 근무 구간 선택지. 8/12/24 는 실제 교대 주기다.
HOUR_CHOICES = (8, 12, 24)
DEFAULT_HOURS = 12


@handover_bp.before_request
def require_login():
    """인계 자료는 로그인한 사용자만."""
    if not session.get("username"):
        flash("인계 자료를 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _hours():
    """쿼리스트링의 시간 값을 정해진 선택지 안으로 가둔다.

    임의의 숫자를 받으면 hours=100000 같은 값이 그대로 SQL 에 들어가
    전체 테이블을 훑게 된다. 값은 %s 로 넘어가므로 주입은 안 되지만,
    질의가 무거워지는 것은 막아야 한다.
    """
    try:
        value = int(request.args.get("hours", DEFAULT_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return value if value in HOUR_CHOICES else DEFAULT_HOURS


# 최종 URL: /handover/
@handover_bp.route("/")
def index():
    """당직 인계 화면."""
    hours = _hours()
    data, error = None, None
    try:
        data = collect(hours)
    except HandoverError as e:
        error = str(e)

    return render_template(
        "handover.html",
        data=data,
        error=error,
        hours=hours,
        choices=HOUR_CHOICES,
        work_label=WORK_LABEL,
    )


# 최종 URL: /handover/download
@handover_bp.route("/download")
def download():
    """인계 내용을 Markdown 으로 내려받는다. 메신저에 붙여넣는 용도."""
    hours = _hours()
    try:
        data = collect(hours)
    except HandoverError as e:
        flash(str(e), "error")
        return redirect(url_for("handover.index"))

    stamp = data["generated_at"].strftime("%Y%m%d-%H%M")
    return Response(
        to_markdown(data),
        # charset 을 두 번 붙이지 않도록 mimetype 만 준다.
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="handover-{stamp}.md"'},
    )
