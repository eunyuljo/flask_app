# app/views/incident.py
# 장애 사후 보고서(RCA) 블루프린트. url_prefix="/incident" 로 등록된다.
# 타임라인은 앱이 모으고, 원인과 조치는 사람이 쓴다.
#
# AI 초안은 그 "사람이 쓴다" 를 대신하지 않는다. 빈 칸을 채워서 보여줄 뿐,
# 저장 버튼을 누르는 것은 사람이다. 모델이 쓴 글이 확인 없이 고객사
# 보고서로 나가면 안 된다.

from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, Response, current_app
)

from app import handoff, incident
from app.accounts import list_accounts, get_account, by_customer, AccountError
from app.incident import (
    IncidentError, STATUS_LABEL, FIELD_LABEL, NARRATIVE_FIELDS,
    CUSTOMER_FIELDS, CUSTOMER_FIELD_LABEL, CUSTOMER_STATUS_LABEL,
)
from app.rca import to_markdown, to_customer_markdown, to_jira
from app import audit

incident_bp = Blueprint("incident", __name__)

# AI 가 만든 초안 보관소. 저장하기 전 단계라 DB 에 넣지 않는다.
#   incident_id -> {"impact":..., ..., "uncertain": [...]}
# 사람이 저장을 누르면 그때 incidents 테이블로 들어가고 여기서 지운다.
# 재시작하면 사라지는데, 그건 문제가 아니다 - 저장 안 한 초안은
# 남아 있을 이유가 없다.
_RCA_DRAFTS = {}

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
        # 알람 화면에서 넘어왔으면 폼이 채워진 채로 열린다.
        prefill=handoff.take(request.args, handoff.INCIDENT_KEYS),
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

    # 알람 화면에서 넘어왔으면 그 지문을 바로 못박는다.
    #
    # 지금까지 장애-알람 연결은 전부 확정 시점의 시간 겹침 추론이었다.
    # 여기서 들어오는 것은 사람이 "이 알람 때문에 쓴다" 고 누른 것이라
    # 추론과 섞이면 안 된다(origin 으로 구분해 저장한다).
    #
    # 연결에 실패해도 보고서 생성은 되돌리지 않는다. 사람이 방금 입력한
    # 것을 부수적인 일 때문에 날리는 편이 더 나쁘다.
    fingerprint = request.form.get("fingerprint", "")
    if fingerprint:
        try:
            incident.link_origin(incident_id, fingerprint,
                                 request.form.get("title", ""))
        except IncidentError as e:
            flash(f"보고서는 만들었지만 알람 연결에 실패했습니다: {e}", "error")

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

    # 사람이 지목한 지문. 나머지는 시간 겹침으로 딸려온 것이라 화면에서
    # 구분한다. 못 읽어도 상세 화면은 떠야 한다.
    try:
        origins = incident.origin_fingerprints(incident_id)
    except IncidentError:
        origins = set()

    # 저장 전 초안이 있으면 빈 칸에만 채워서 보여준다.
    # 사람이 이미 쓴 칸은 건드리지 않는다 - 초안 버튼을 잘못 눌렀다고
    # 써둔 문장이 날아가면 안 된다.
    draft = _RCA_DRAFTS.get(incident_id)
    shown = {f: item[f] for f in NARRATIVE_FIELDS}
    filled = []
    if draft:
        for field in NARRATIVE_FIELDS:
            if not shown[field].strip() and draft.get(field, "").strip():
                shown[field] = draft[field]
                filled.append(field)

    return render_template(
        "incident_detail.html",
        item=item, data=data, error=error, related=related, origins=origins,
        labels=STATUS_LABEL, field_labels=FIELD_LABEL,
        customer_labels=CUSTOMER_STATUS_LABEL,
        customer_field_labels=CUSTOMER_FIELD_LABEL,
        draft=draft, shown=shown, filled=filled,
        jira_base=current_app.config.get("JIRA_BASE_URL", ""),
    )


# 최종 URL: /incident/<번호>/save
@incident_bp.route("/<int:incident_id>/save", methods=["POST"])
def save(incident_id):
    """사람이 쓴 칸을 저장한다."""
    fields = {f: request.form.get(f, "").strip() for f in NARRATIVE_FIELDS}
    try:
        incident.update_narrative(incident_id, fields)
        # 저장했으면 초안은 할 일을 마쳤다. 남겨두면 다음에 이 화면을
        # 열었을 때 저장한 내용 위에 또 초안이 얹힌 것처럼 보인다.
        _RCA_DRAFTS.pop(incident_id, None)
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


# ----------------------------------------------------------------------
# AI 초안
# ----------------------------------------------------------------------
# 알람 진단과 달리 AWS 를 조회하지 않는다. 장애는 이미 지난 일이고
# 근거는 전부 DB 에 있다. 그래서 계정을 고를 필요도 없다.


@incident_bp.route("/<int:incident_id>/rca/draft", methods=["POST"])
def rca_draft(incident_id):
    """타임라인을 근거로 원인 분석 초안을 만든다.

    저장하지 않는다. 화면의 빈 칸에 채워서 보여주고, 사람이 저장을
    눌러야 남는다.
    """
    from app.agent_core import draft_rca, AgentNotConfigured

    try:
        item, data, error = _load(incident_id)
        if item is None:
            raise IncidentError(error)
        if data is None:
            raise IncidentError(error or "타임라인을 모으지 못했습니다.")
        if item["status"] != "draft":
            raise IncidentError("이미 제출된 보고서에는 초안을 만들지 않습니다.")
    except IncidentError as e:
        flash(str(e), "error")
        return redirect(url_for("incident.detail", incident_id=incident_id))

    # 같은 알람으로 났던 지난 장애를 함께 넘긴다. 재발인지 아닌지가
    # 원인 분석에서 가장 크게 갈리는 지점이다.
    past = []
    seen = set()
    for kind in (data.get("by_kind") or [])[:5]:
        for other in incident.past_incidents(kind["fingerprint"], limit=2,
                                             exclude_id=item["id"]):
            if other["id"] not in seen:
                seen.add(other["id"])
                past.append(other)

    runbooks = []
    try:
        from app import runbook

        for kind in (data.get("by_kind") or [])[:3]:
            found = runbook.find(kind["fingerprint"], item.get("customer", ""))
            if found:
                runbooks.append(found)
    except Exception:
        # 런북이 없어도 초안은 만들 수 있다. 여기서 막지 않는다.
        runbooks = []

    try:
        draft = draft_rca(item, data, past=past, runbooks=runbooks)
    except AgentNotConfigured as e:
        flash(str(e), "error")
        return redirect(url_for("incident.detail", incident_id=incident_id))
    except Exception as e:
        flash(f"초안을 만들지 못했습니다: {e}", "error")
        return redirect(url_for("incident.detail", incident_id=incident_id))

    _RCA_DRAFTS[incident_id] = draft

    # 모델이 만든 문서라는 것을 기록에 남긴다. 고객사 계정을 건드린 것은
    # 아니지만, 나중에 "이 문장 누가 썼나" 를 물었을 때 답할 수 있어야 한다.
    audit.record(
        action="rca_draft", outcome="ok",
        summary=f"사후 보고서 초안: #{item['id']} {item['title'][:80]}",
        detail=f"근거 이벤트 {data.get('event_count', 0)}건, 지난 장애 {len(past)}건",
        account={"customer": item.get("customer", ""),
                 "account_id": item.get("account_id", "")},
        region=item.get("region", ""),
        actor_kind="agent",
        meta={"incident_id": item["id"],
              "uncertain": draft.get("uncertain", [])},
    )

    flash("초안을 만들었습니다. 빈 칸에 채워 두었으니 확인하고 저장하세요. "
          "저장하지 않으면 남지 않습니다.", "success")
    return redirect(url_for("incident.detail", incident_id=incident_id))


@incident_bp.route("/<int:incident_id>/rca/discard", methods=["POST"])
def rca_discard(incident_id):
    """초안을 버린다."""
    if _RCA_DRAFTS.pop(incident_id, None):
        flash("초안을 버렸습니다.", "success")
    return redirect(url_for("incident.detail", incident_id=incident_id))


@incident_bp.route("/<int:incident_id>/jira", methods=["POST"])
def to_jira_issue(incident_id):
    """사후 보고서를 Jira 이슈로 넘긴다.

    제출된 보고서만 넘긴다. 초안 상태로 넘기면 나중에 내용이 바뀌는데
    Jira 쪽은 그대로 남아, 두 곳의 내용이 갈린다.
    """
    from app import jira as jira_mod
    from app.jira import JiraError, JiraNotConfigured

    try:
        item, data, _error = _load(incident_id)
        if item is None:
            raise IncidentError("장애 기록을 찾지 못했습니다.")
        if item["status"] != "published":
            raise IncidentError(
                "제출된 보고서만 Jira 로 넘길 수 있습니다. "
                "초안 상태로 넘기면 나중에 내용이 갈립니다."
            )
        if (item.get("jira_key") or "").strip():
            raise IncidentError(f"이미 Jira 이슈가 있습니다: {item['jira_key']}")
    except IncidentError as e:
        flash(str(e), "error")
        return redirect(url_for("incident.detail", incident_id=incident_id))

    summary, description = to_jira(item, data)
    labels = ["incident", "rca"]
    if item.get("customer"):
        # Jira 라벨에는 공백을 못 넣는다.
        labels.append(item["customer"].replace(" ", "-"))

    try:
        key = jira_mod.create_issue(summary, description, labels=labels)
    except JiraNotConfigured as e:
        flash(str(e), "error")
        return redirect(url_for("incident.detail", incident_id=incident_id))
    except JiraError as e:
        flash(f"Jira 이슈를 만들지 못했습니다: {e}", "error")
        return redirect(url_for("incident.detail", incident_id=incident_id))

    try:
        incident.set_jira_key(incident_id, key)
    except IncidentError as e:
        # 이슈는 이미 만들어졌다. 여기서 실패하면 키가 어디에도 안 남으므로
        # 화면에라도 알려야 한다 - 안 그러면 다음 사람이 또 만든다.
        flash(f"Jira {key} 를 만들었지만 기록에 남기지 못했습니다: {e}", "error")
        return redirect(url_for("incident.detail", incident_id=incident_id))

    audit.record(
        action="incident_jira", outcome="ok",
        summary=f"사후 보고서 Jira 등록: #{item['id']} -> {key}",
        account={"customer": item.get("customer", ""),
                 "account_id": item.get("account_id", "")},
        meta={"incident_id": item["id"], "jira_key": key},
    )
    flash(f"Jira {key} 로 넘겼습니다.", "success")
    return redirect(url_for("incident.detail", incident_id=incident_id))
