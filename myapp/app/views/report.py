# app/views/report.py
# 기간별 운영 리포트 화면과 Markdown 내려받기.
# app/__init__.py 에서 url_prefix="/report" 로 등록된다.

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, Response,
)

from app.report import collect, to_markdown, resource_diff_summary, generate_summary, ReportError
from app.agent_core import check_config as agent_check

report_bp = Blueprint("report", __name__)

ALLOWED_DAYS = (1, 7, 30)


@report_bp.before_request
def require_login():
    if not session.get("username"):
        flash("리포트를 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _days():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7
    return days if days in ALLOWED_DAYS else 7


def _build(days, with_summary):
    """리포트 자료를 모은다. 요약은 요청했을 때만 만든다(API 호출이라 느리고 비용이 든다)."""
    data = collect(days)
    rdiff = resource_diff_summary()
    summary = None
    if with_summary:
        try:
            summary = generate_summary(data, rdiff)
        except Exception as e:
            # 요약에 실패해도 리포트 본문은 나와야 한다.
            summary = None
            flash(f"AI 요약을 만들지 못했습니다: {type(e).__name__}", "error")
    return data, rdiff, summary


@report_bp.route("/")
def index():
    days = _days()
    # ?summary=1 일 때만 에이전트를 부른다.
    want_summary = request.args.get("summary") == "1"

    try:
        data, rdiff, summary = _build(days, want_summary)
        error = None
    except ReportError as e:
        data, rdiff, summary, error = None, None, None, str(e)

    return render_template(
        "report.html",
        data=data,
        rdiff=rdiff,
        summary=summary,
        error=error,
        days=days,
        allowed_days=ALLOWED_DAYS,
        want_summary=want_summary,
        # 에이전트 설정이 없으면 요약 버튼을 비활성화한다.
        agent_ready=agent_check() is None,
    )


@report_bp.route("/download")
def download():
    """Markdown 파일로 내려받는다."""
    days = _days()
    want_summary = request.args.get("summary") == "1"

    try:
        data, rdiff, summary = _build(days, want_summary)
    except ReportError as e:
        flash(str(e), "error")
        return redirect(url_for("report.index", days=days))

    text = to_markdown(data, rdiff, summary)
    filename = f"report-{data['end'].strftime('%Y%m%d')}-{days}d.md"

    # Content-Disposition: attachment 를 주면 브라우저가 화면에 그리지 않고 저장한다.
    # mimetype 에 charset 을 직접 쓰면 Flask 가 또 붙여서
    # "text/markdown; charset=utf-8; charset=utf-8" 이 된다.
    # 종류만 주고 인코딩은 Flask 에 맡긴다.
    return Response(
        text,
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
