# app/views/compliance.py
# 컴플라이언스 점검 화면. 이미 찍어둔 리소스 스냅샷을 기준으로
# 인프라가 모범사례를 지키고 있는지 본다.
# app/__init__.py 에서 url_prefix="/compliance" 로 등록된다.
#
# 점검 로직은 app/compliance.py 에 있다. 여기서는 어느 스냅샷을 볼지 고르고,
# 결과를 화면에 맞게 묶는 일만 한다.

from datetime import datetime, timedelta, timezone

from urllib.parse import quote

from flask import (
    Blueprint, Response, render_template, request, redirect, url_for, session, flash
)

from app import audit, compliance
from app.accounts import list_accounts, AccountError
from app.compliance import ComplianceError

compliance_bp = Blueprint("compliance", __name__)


@compliance_bp.before_request
def require_login():
    if not session.get("username"):
        flash("컴플라이언스 화면을 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _customers():
    """계정 ID -> 고객사 이름. 보고서에 계정 번호만 찍히면 누구 것인지 모른다."""
    try:
        return {a["account_id"]: a["customer"] for a in list_accounts(enabled_only=False)}
    except AccountError:
        return {}


def _report(target, names=None):
    """계정+리전 하나의 점검 결과를 한 덩어리로 모은다.

    화면과 엑셀이 같은 함수를 쓴다. 갈라놓으면 화면에는 있는데 보고서에는
    없는 항목이 생기고, 그건 화면을 믿을 수 없다는 뜻이 된다.
    """
    names = names if names is not None else _customers()
    violations, snap = compliance.evaluate(target["snapshot_id"], target["account_id"])
    points = compliance.timeline(target["account_id"], target["region"])
    return {
        "account_id": target["account_id"],
        "region": target["region"],
        "customer": names.get(target["account_id"], ""),
        "snapshot_id": target["snapshot_id"],
        "collected_at": target["collected_at"],
        "resources": len(snap),
        "collected_types": sorted(snap.collected),
        "coverage": compliance.coverage(snap),
        "violations": violations,
        "summary": compliance.summarize(violations, snap),
        "points": points,
        "repeats": compliance.recurring(points),
        "since": compliance.first_seen(points),
        "exceptions": compliance.exceptions(target["account_id"]),
    }


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

    report = None
    if chosen and not error:
        try:
            report = _report(chosen)
        except ComplianceError as e:
            error = str(e)

    violations = report["violations"] if report else []
    summary = report["summary"] if report else None
    coverage = report["coverage"] if report else None
    points = report["points"] if report else []
    repeats = report["repeats"] if report else []
    since = report["since"] if report else {}

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
        coverage=coverage,
        collected_types=report["collected_types"] if report else [],
        resources=report["resources"] if report else 0,
        points=list(reversed(points)),   # 화면에는 최신순
        repeats=repeats,
        since=since,
        checks=compliance.CHECKS,
        severities=compliance.SEVERITIES,
    )


@compliance_bp.route("/download.xlsx")
def download_xlsx():
    """점검 결과를 엑셀로 내려받는다.

    account 를 주면 그 계정만, 주지 않으면 전체를 한 권에 담는다.
    고객사 보고는 계정 하나로 끝나지만 월간 내부 보고는 전체를 나란히 놓고 본다.
    """
    from app.compliance_xlsx import build, ExcelNotAvailable

    account_id = request.args.get("account", "")
    region = request.args.get("region", "")

    try:
        targets = compliance.latest_snapshots()
    except ComplianceError as e:
        flash(str(e), "error")
        return redirect(url_for("compliance.index"))

    if account_id:
        targets = [
            t for t in targets
            if t["account_id"] == account_id and (not region or t["region"] == region)
        ]
    if not targets:
        flash("내려받을 점검 결과가 없습니다. 먼저 리소스를 수집하세요.", "error")
        return redirect(url_for("compliance.index"))

    names = _customers()
    try:
        reports = [_report(t, names) for t in targets]
        buf = build(reports)
    except ExcelNotAvailable as e:
        flash(str(e), "error")
        return redirect(url_for("compliance.index", account=account_id, region=region))
    except ComplianceError as e:
        flash(str(e), "error")
        return redirect(url_for("compliance.index", account=account_id, region=region))

    stamp = reports[0]["collected_at"].strftime("%Y%m%d")
    if account_id:
        label = reports[0]["customer"] or account_id
        name = f"컴플라이언스-{label}-{stamp}.xlsx"
    else:
        name = f"컴플라이언스-전체-{stamp}.xlsx"

    # 파일 이름에 한글이 들어간다. HTTP 헤더는 latin-1 이라 그대로 넣으면
    # 깨지거나 서버가 거부한다. RFC 5987 의 filename* 로 UTF-8 을 알려주고,
    # 그걸 모르는 옛 클라이언트를 위해 ASCII 이름도 함께 준다.
    ascii_name = f"compliance-{account_id or 'all'}-{stamp}.xlsx"
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(name)}"
    )

    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
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
