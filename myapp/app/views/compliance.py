# app/views/compliance.py
# 컴플라이언스 점검 화면. 이미 찍어둔 리소스 스냅샷을 기준으로
# 인프라가 모범사례를 지키고 있는지 본다.
# app/__init__.py 에서 url_prefix="/compliance" 로 등록된다.
#
# 점검 로직은 app/compliance.py 에 있다. 여기서는 어느 스냅샷을 볼지 고르고,
# 결과를 화면에 맞게 묶는 일만 한다.

from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash
)

from app import audit, compliance
from app.compliance import ComplianceError

compliance_bp = Blueprint("compliance", __name__)


@compliance_bp.before_request
def require_login():
    if not session.get("username"):
        flash("컴플라이언스 화면을 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


@compliance_bp.route("/")
def index():
    """계정+리전을 고르고 그 최신 스냅샷을 점검한다."""
    account_id = request.args.get("account", "")
    region = request.args.get("region", "")

    targets, error = [], None
    try:
        targets = compliance.latest_snapshots()
    except ComplianceError as e:
        error = str(e)

    # 고르지 않았으면 첫 번째를 본다. 빈 화면부터 보여주면
    # "무엇을 골라야 하는지" 를 한 번 더 생각하게 만든다.
    chosen = None
    for t in targets:
        if (t["account_id"] == account_id and t["region"] == region) or not account_id:
            chosen = t
            break

    violations, summary, snapshot, points, repeats, since = [], None, None, [], [], {}
    if chosen and not error:
        try:
            violations, snapshot = compliance.evaluate(
                chosen["snapshot_id"], chosen["account_id"]
            )
            summary = compliance.summarize(violations)
            points = compliance.timeline(chosen["account_id"], chosen["region"])
            repeats = compliance.recurring(points)
            since = compliance.first_seen(points)
        except ComplianceError as e:
            error = str(e)

    # 항목별로 묶는다. 같은 위반이 리소스 열 개에 걸쳐 있으면
    # 줄 열 개보다 "이 항목에 열 개" 가 읽기 쉽다.
    grouped = []
    for check in compliance.CHECKS:
        hits = [v for v in violations if v["check_id"] == check["id"]]
        if hits:
            grouped.append({
                "check": check,
                "hits": hits,
                "live": [h for h in hits if not h["excused"]],
            })

    return render_template(
        "compliance.html",
        targets=targets,
        chosen=chosen,
        error=error,
        grouped=grouped,
        summary=summary,
        snapshot=snapshot,
        points=list(reversed(points)),   # 화면에는 최신순
        repeats=repeats,
        since=since,
        checks=compliance.CHECKS,
        severities=compliance.SEVERITIES,
    )


@compliance_bp.route("/exceptions")
def exceptions():
    """예외 목록. 만료된 것도 함께 보여준다."""
    account_id = request.args.get("account", "")
    items, error = [], None
    try:
        items = compliance.all_exceptions(account_id or None)
    except ComplianceError as e:
        error = str(e)

    targets = []
    try:
        targets = compliance.latest_snapshots()
    except ComplianceError:
        pass

    return render_template(
        "compliance_exceptions.html",
        items=items, error=error, account_id=account_id,
        targets=targets, checks=compliance.CHECKS,
    )


@compliance_bp.route("/exceptions/add", methods=["POST"])
def add_exception():
    """예외를 등록한다. 사유와 기간이 반드시 있어야 한다."""
    account_id = request.form.get("account_id", "").strip()
    check_id = request.form.get("check_id", "").strip()
    resource_id = request.form.get("resource_id", "").strip()
    reason = request.form.get("reason", "")

    try:
        days = int(request.form.get("days", "30"))
    except ValueError:
        days = 30
    days = max(1, min(days, 365))   # 1년을 넘는 예외는 사실상 영구 면제다
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    try:
        compliance.add_exception(
            account_id, check_id, resource_id, reason, expires_at,
            approved_by=session.get("username", ""),
        )
    except ComplianceError as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("compliance.index"))

    # 위반을 목록에서 지우는 행위라 기록을 남긴다. 사유와 승인자가
    # 표에도 남지만, 감사 로그에서 "누가 무엇을 덮었나" 를 시간순으로
    # 훑을 수 있어야 한다.
    audit.record(
        action="compliance_exception", outcome="ok",
        summary=f"예외 등록: {check_id} / {resource_id or '계정 전체'}",
        detail=f"{days}일, 사유: {reason.strip()[:200]}",
        account={"account_id": account_id},
    )
    flash(f"예외를 등록했습니다. {days}일 뒤에 자동으로 풀립니다.", "success")
    return redirect(request.referrer or url_for("compliance.index"))


@compliance_bp.route("/exceptions/<int:exception_id>/delete", methods=["POST"])
def delete_exception(exception_id):
    try:
        compliance.drop_exception(exception_id)
    except ComplianceError as e:
        flash(str(e), "error")
        return redirect(url_for("compliance.exceptions"))

    audit.record(
        action="compliance_exception", outcome="ok",
        summary=f"예외 삭제: #{exception_id}",
    )
    flash("예외를 지웠습니다.", "success")
    return redirect(url_for("compliance.exceptions"))
