# app/views/incident.py
# 장애 사후 보고서(RCA) 블루프린트. url_prefix="/incident" 로 등록된다.
# 타임라인은 앱이 모으고, 원인과 조치는 사람이 쓴다.

from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, Response
)

from app import incident
from app.accounts import list_accounts, get_account, by_customer, AccountError
from app.incident import (
    IncidentError, STATUS_LABEL, FIELD_LABEL, NARRATIVE_FIELDS,
    CUSTOMER_FIELDS, CUSTOMER_FIELD_LABEL, CUSTOMER_STATUS_LABEL,
)
from app.rca import to_markdown, to_customer_markdown

incident_bp = Blueprint("incident", __name__)

SEVERITIES = ("critical", "error", "warning", "info")


@incident_bp.before_request
def require_login():
    """사후 보고서는 로그인한 사용자만."""
    if not session.get("username"):
        flash("사후 보고서를 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _parse_time(value):
    """<input type="datetime-local"> 값을 UTC 시각으로 바꾼다.

    브라우저는 타임존 없는 문자열("2026-08-21T14:30")을 보낸다.
    이 앱은 화면과 DB 가 전부 UTC 기준이므로 UTC 로 읽는다.
    폼에도 UTC 라고 적어둔다 - 말없이 로컬 시각으로 해석하면
    타임라인이 몇 시간씩 어긋난 채로 맞아 보인다.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        raise IncidentError(f"시각 형식을 알 수 없습니다: {value}")


# 최종 URL: /incident/
@incident_bp.route("/")
def index():
    """사후 보고서 목록과 새 보고서 폼."""
    error, items, accounts = None, [], []
    try:
        items = incident.recent()
    except IncidentError as e:
        error = str(e)
    try:
        accounts = list_accounts()
    except AccountError:
        pass

    return render_template(
        "incident.html",
        items=items,
        grouped=by_customer(accounts),
        severities=SEVERITIES,
        labels=STATUS_LABEL,
        error=error,
    )


# 최종 URL: /incident/new
@incident_bp.route("/new", methods=["POST"])
def new():
    """장애 기록을 만든다."""
    account_id = request.form.get("account_id", "")
    region = request.form.get("region", "")
    customer = ""

    try:
        # 계정을 골랐으면 고객사와 리전을 그 계정에서 가져온다.
        if account_id:
            account = get_account(account_id)
            if account is None:
                raise IncidentError("계정을 찾지 못했습니다.")
            customer = account["customer"]
            regions = account.get("regions") or []
            if region not in regions:
                region = regions[0] if regions else ""

        severity = request.form.get("severity", "error")
        if severity not in SEVERITIES:
            severity = "error"

        # "pay-api, api-gw" 처럼 쉼표로 받는다. 빈 값은 버린다.
        sources = [x.strip() for x in request.form.get("sources", "").split(",") if x.strip()]

        incident_id = incident.create(
            sources=sources,
            title=request.form.get("title", ""),
            started_at=_parse_time(request.form.get("started_at", "")),
            ended_at=_parse_time(request.form.get("ended_at", "")),
            severity=severity,
            customer=customer,
            account_id=account_id,
            region=region,
            author=session.get("username", ""),
        )
    except (IncidentError, AccountError) as e:
        flash(str(e), "error")
        return redirect(url_for("incident.index"))

    return redirect(url_for("incident.detail", incident_id=incident_id))


def _load(incident_id):
    """기록과 조립된 타임라인을 함께 가져온다."""
    item = incident.get(incident_id)
    if item is None:
        return None, None, "장애 기록을 찾지 못했습니다."
    try:
        return item, incident.assemble(item), None
    except IncidentError as e:
        return item, None, str(e)


# 최종 URL: /incident/<번호>
@incident_bp.route("/<int:incident_id>")
def detail(incident_id):
    """보고서 상세. 타임라인 확인 + 서술 작성."""
    try:
        item, data, error = _load(incident_id)
    except IncidentError as e:
        flash(str(e), "error")
        return redirect(url_for("incident.index"))

    if item is None:
        flash(error, "error")
        return redirect(url_for("incident.index"))

    # 이 장애의 알람 종류가 관련됐던 다른 장애. 반복되는 문제인지 보인다.
    related = []
    if data:
        seen = set()
        for kind in data.get("by_kind", [])[:5]:
            for other in incident.past_incidents(
                kind["fingerprint"], limit=2, exclude_id=item["id"]
            ):
                if other["id"] not in seen:
                    seen.add(other["id"])
                    related.append(other)

    return render_template(
        "incident_detail.html",
        item=item, data=data, error=error, related=related,
        labels=STATUS_LABEL, field_labels=FIELD_LABEL,
        customer_labels=CUSTOMER_STATUS_LABEL,
        customer_field_labels=CUSTOMER_FIELD_LABEL,
    )


# 최종 URL: /incident/<번호>/save
@incident_bp.route("/<int:incident_id>/save", methods=["POST"])
def save(incident_id):
    """사람이 쓴 칸을 저장한다."""
    fields = {f: request.form.get(f, "").strip() for f in NARRATIVE_FIELDS}
    try:
        incident.update_narrative(incident_id, fields)
        flash("저장했습니다.", "success")
    except IncidentError as e:
        flash(str(e), "error")
    return redirect(url_for("incident.detail", incident_id=incident_id))


# 최종 URL: /incident/<번호>/publish
@incident_bp.route("/<int:incident_id>/publish", methods=["POST"])
def publish(incident_id):
    """보고서를 제출 상태로 바꾼다."""
    try:
        incident.publish(incident_id)
        flash("보고서를 제출 상태로 바꿨습니다. 이제 내용을 고칠 수 없습니다.", "success")
    except IncidentError as e:
        flash(str(e), "error")
    return redirect(url_for("incident.detail", incident_id=incident_id))


# 최종 URL: /incident/<번호>/report.md
@incident_bp.route("/<int:incident_id>/report.md")
def report(incident_id):
    """사후 보고서를 Markdown 으로 내려받는다."""
    try:
        item, data, error = _load(incident_id)
    except IncidentError as e:
        flash(str(e), "error")
        return redirect(url_for("incident.index"))

    if item is None or data is None:
        flash(error or "타임라인을 모으지 못했습니다.", "error")
        return redirect(url_for("incident.index"))

    return Response(
        to_markdown(item, data),
        # charset 을 두 번 붙이지 않도록 mimetype 만 준다.
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="rca-{item["id"]}.md"'},
    )


# ----------------------------------------------------------------------
# 고객 제출본
# ----------------------------------------------------------------------
# 내부 RCA 와 기록은 공유하고 출력만 나눈다.

# 최종 URL: /incident/<번호>/customer/draft
@incident_bp.route("/<int:incident_id>/customer/draft", methods=["POST"])
def customer_draft(incident_id):
    """고객 제출본 초안을 채운다. 이미 쓴 내용은 덮어쓰지 않는다."""
    try:
        item, data, error = _load(incident_id)
        if item is None:
            raise IncidentError(error)
        if data is None:
            raise IncidentError(error or "타임라인을 모으지 못했습니다.")

        draft = incident.draft_customer(item, data)
        # 사람이 이미 손댄 칸은 건드리지 않는다. 초안 버튼을 잘못 눌렀다고
        # 써둔 문장이 날아가면 안 된다.
        fields = {k: v for k, v in draft.items() if not item[k].strip() and v.strip()}
        if not fields:
            flash("채울 빈 칸이 없습니다. 이미 작성된 내용은 덮어쓰지 않습니다.", "error")
        else:
            incident.update_customer(incident_id, fields)
            flash(f"{len(fields)}개 칸에 초안을 채웠습니다. 내용을 다듬어 주세요.", "success")
    except IncidentError as e:
        flash(str(e), "error")

    return redirect(url_for("incident.detail", incident_id=incident_id))


# 최종 URL: /incident/<번호>/customer/save
@incident_bp.route("/<int:incident_id>/customer/save", methods=["POST"])
def customer_save(incident_id):
    """고객 제출본을 저장한다."""
    fields = {f: request.form.get(f, "").strip() for f in CUSTOMER_FIELDS}
    try:
        incident.update_customer(incident_id, fields)
        flash("고객 제출본을 저장했습니다.", "success")
    except IncidentError as e:
        flash(str(e), "error")
    return redirect(url_for("incident.detail", incident_id=incident_id))


# 최종 URL: /incident/<번호>/customer/send
@incident_bp.route("/<int:incident_id>/customer/send", methods=["POST"])
def customer_send(incident_id):
    """고객 제출본을 제출 상태로 바꾼다."""
    try:
        incident.send_customer(incident_id)
        flash("고객사 제출 상태로 바꿨습니다. 이제 내용을 고칠 수 없습니다.", "success")
    except IncidentError as e:
        flash(str(e), "error")
    return redirect(url_for("incident.detail", incident_id=incident_id))


# 최종 URL: /incident/<번호>/customer.md
@incident_bp.route("/<int:incident_id>/customer.md")
def customer_report(incident_id):
    """고객사에 낼 보고서를 Markdown 으로 내려받는다.

    내부 RCA(report.md)와 달리 근거 표가 통째로 빠진다.
    타임라인 원본·알람 요약·리소스 변경 목록은 우리가 조사한 과정이지
    고객이 받을 문서가 아니다.
    """
    try:
        item = incident.get(incident_id)
    except IncidentError as e:
        flash(str(e), "error")
        return redirect(url_for("incident.index"))

    if item is None:
        flash("장애 기록을 찾지 못했습니다.", "error")
        return redirect(url_for("incident.index"))

    return Response(
        to_customer_markdown(item),
        # charset 을 두 번 붙이지 않도록 mimetype 만 준다.
        mimetype="text/markdown",
        headers={
            "Content-Disposition":
                f'attachment; filename="incident-report-{item["id"]}.md"'
        },
    )
