# app/views/customer.py
# 고객사 현황 블루프린트. url_prefix="/customer" 로 등록된다.
# 이 앱에 처음 생기는 '고객사 축' 화면이다. 나머지는 전부 기능 축이다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash,
    Response,
)

from datetime import datetime, timezone

from app import (access, audit, contacts, customer, customer_brief,
                 readiness, routines, standards)
from app.access import AccessError
from app.contacts import ContactError
from app.customer_brief import BriefError
from app.accounts import list_accounts, get_account, AccountError
from app.customer import CustomerError
from app.readiness import ReadinessError
from app.routines import RoutineError
from app.standards import StandardError

customer_bp = Blueprint("customer", __name__)


@customer_bp.before_request
def require_login():
    """고객사 현황은 로그인한 사용자만."""
    if not session.get("username"):
        flash("로그인이 필요합니다.", "error")
        return redirect(url_for("auth.login"))


# 최종 URL: /customer/
@customer_bp.route("/")
def index():
    """고객사를 고르고 그 현황을 본다."""
    error, all_names, data = None, [], None
    try:
        all_names = customer.names()
    except CustomerError as e:
        error = str(e)

    # 고른 고객사가 없으면 첫 번째를 보여준다. 빈 화면보다 낫다.
    selected = request.args.get("name") or (all_names[0] if all_names else "")

    if selected and selected not in all_names:
        flash(f"등록되지 않은 고객사입니다: {selected}", "error")
        selected = all_names[0] if all_names else ""

    if selected and not error:
        try:
            data = customer.overview(selected)
        except CustomerError as e:
            error = str(e)

    # 연락처는 이 화면에 붙인다. 새 메뉴를 만들지 않은 이유는 축이
    # 같아서다 - 여기가 이미 "고객사 하나를 놓고 보는" 자리다.
    # 못 읽어도 나머지 현황은 보여야 한다.
    people = []
    if selected:
        try:
            people = contacts.listing(selected)
        except ContactError:
            people = []

    return render_template(
        "customer.html",
        names=all_names, selected=selected, data=data, error=error,
        contacts=people, contact_kinds=contacts.KINDS,
    )


# 최종 URL: /customer/readiness
# 새 블루프린트를 만들지 않고 여기에 붙인 이유는 축이 같기 때문이다.
# 이 화면도 "고객사 하나를 놓고 본다".
@customer_bp.route("/readiness")
def readiness_page():
    """준비 상태. 고객사 하나를 깊게(기본) / 전부를 넓게(?view=all).

    두 화면으로 나눠 뒀다가 합쳤다. 점검 항목이 readiness.CHECKS 하나로
    같고 축만 달라서, 메뉴를 둘로 두면 "어느 쪽을 봐야 하지" 를 매번
    묻게 된다. 같은 자료의 두 가지 보기라면 탭이 맞다.
    """
    # 전부를 넓게 보는 쪽. 매트릭스는 고객사를 고르지 않는다.
    if request.args.get("view") == "all":
        error, data = None, None
        try:
            data = readiness.matrix(customer.names())
        except (CustomerError, ReadinessError) as e:
            error = str(e)
        return render_template(
            "customer_settings.html",
            data=data, error=error,
            status_label=readiness.STATUS_LABEL,
            level_label=readiness.LEVELS,
            endpoint_labels=readiness.ENDPOINT_LABELS,
        )

    error, all_names, results, summary, facts = None, [], [], None, None
    try:
        all_names = customer.names()
    except CustomerError as e:
        error = str(e)

    selected = request.args.get("name") or (all_names[0] if all_names else "")
    if selected and selected not in all_names:
        flash(f"등록되지 않은 고객사입니다: {selected}", "error")
        selected = all_names[0] if all_names else ""

    if selected and not error:
        try:
            results, facts = readiness.evaluate(selected)
            summary = readiness.summarize(results)
        except ReadinessError as e:
            error = str(e)

    return render_template(
        "customer_readiness.html",
        names=all_names, selected=selected, error=error,
        results=results, summary=summary, facts=facts,
        levels=readiness.LEVELS, labels=readiness.STATUS_LABEL,
        endpoint_labels=readiness.ENDPOINT_LABELS,
    )


# 최종 URL: /customer/access
@customer_bp.route("/access")
def access_page():
    """고객사 계정에 우리가 들어갈 수 있는가, 들어가는 방식이 안전한가."""
    error, rows = None, []
    try:
        # 비활성 계정도 본다. 계약이 끝나 꺼둔 계정에 우리 역할이 아직
        # 살아 있는지가 여기서 봐야 할 것 중 하나다.
        rows = access.overview(list_accounts(enabled_only=False))
    except AccountError as e:
        error = str(e)

    return render_template(
        "customer_access.html",
        rows=rows, error=error,
        level_label=access.LEVEL_LABEL,
        stale_days=access.STALE_DAYS,
    )


# 최종 URL: /customer/access/probe
@customer_bp.route("/access/probe", methods=["POST"])
def access_probe():
    """이 계정에 실제로 들어가 본다.

    관리자만 누를 수 있다. AssumeRole 은 고객사 CloudTrail 에 남는 행위라,
    누가 언제 했는지 우리 쪽에도 남아야 한다.
    """
    from app import users

    if not users.can(session.get("role"), "admin"):
        flash("계정 접속 확인은 관리자만 할 수 있습니다.", "error")
        return redirect(url_for("customer.access_page"))

    account_id = request.form.get("account_id", "")
    region = request.form.get("region", "")

    try:
        account = get_account(account_id)
    except AccountError as e:
        flash(str(e), "error")
        return redirect(url_for("customer.access_page"))

    if account is None:
        flash(f"등록되지 않은 계정입니다: {account_id}", "error")
        return redirect(url_for("customer.access_page"))

    try:
        ok, detail = access.probe(account, region, session.get("username", ""))
    except AccessError as e:
        flash(str(e), "error")
        return redirect(url_for("customer.access_page"))

    # 고객사 계정을 건드린 기록. 콘솔·진단과 같은 자리에 남긴다.
    audit.record(
        action="account_probe", outcome="ok" if ok else "failed",
        account=account, region=region,
        summary=f"계정 접속 확인: {account_id} / {region}", detail=detail,
    )

    flash(f"{account_id} / {region}: {detail}", "success" if ok else "error")
    return redirect(url_for("customer.access_page"))


# 최종 URL: /customer/routines
@customer_bp.route("/routines")
def routines_page():
    """고객사에 약속한 주기 업무를 지키고 있는가."""
    error, items, counts = None, [], {}
    selected = request.args.get("customer", "")

    try:
        items = routines.listing(selected)
        counts = routines.summary(items)
    except RoutineError as e:
        error = str(e)

    try:
        all_names = customer.names()
    except CustomerError:
        all_names = []

    return render_template(
        "customer_routines.html",
        items=items, counts=counts, error=error,
        customers=all_names, selected=selected,
        presets=routines.PRESETS, state_label=routines.STATE_LABEL,
        alert_states=routines.ALERT_STATES,
    )


# 최종 URL: /customer/routines/add
@customer_bp.route("/routines/add", methods=["POST"])
def routines_add():
    """주기 업무를 등록한다."""
    try:
        routines.add(
            customer=request.form.get("customer", ""),
            name=request.form.get("name", ""),
            interval_days=request.form.get("interval_days", 0),
            why=request.form.get("why", ""),
        )
        flash("점검 항목을 등록했습니다.", "success")
    except RoutineError as e:
        flash(str(e), "error")
    return redirect(url_for("customer.routines_page",
                            customer=request.form.get("customer", "")))


# 최종 URL: /customer/routines/<번호>/done
@customer_bp.route("/routines/<int:routine_id>/done", methods=["POST"])
def routines_done(routine_id):
    """이번 주기 것을 했다고 남긴다."""
    try:
        routines.mark_done(routine_id, session.get("username", ""),
                           request.form.get("note", ""))
        flash("수행 기록을 남겼습니다.", "success")
    except RoutineError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("customer.routines_page"))


# 최종 URL: /customer/routines/<번호>/active
@customer_bp.route("/routines/<int:routine_id>/active", methods=["POST"])
def routines_active(routine_id):
    """멈추거나 다시 시작한다."""
    active = request.form.get("active") == "1"
    try:
        routines.set_active(routine_id, active)
        flash("멈췄습니다." if not active else "다시 시작했습니다.", "success")
    except RoutineError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("customer.routines_page"))


# /customer/settings 은 없어졌다. 준비도 화면의 ?view=all 탭으로 흡수했다.
# 점검 항목이 같은데 메뉴가 둘이면 "어느 쪽을 봐야 하지" 를 매번 묻게 된다.
# 예전 주소로 들어오는 링크(북마크·문서)를 위해 넘겨만 준다.
@customer_bp.route("/settings")
def settings_page():
    return redirect(url_for("customer.readiness_page", view="all"))


# 최종 URL: /customer/contacts/add
@customer_bp.route("/contacts/add", methods=["POST"])
def contacts_add():
    """고객사 연락처를 등록한다."""
    name = request.form.get("customer", "")
    try:
        contacts.add(
            customer=name,
            name=request.form.get("name", ""),
            kind=request.form.get("kind", ""),
            email=request.form.get("email", ""),
            phone=request.form.get("phone", ""),
            note=request.form.get("note", ""),
        )
        flash("연락처를 등록했습니다.", "success")
    except ContactError as e:
        flash(str(e), "error")
    return redirect(url_for("customer.index", name=name))


# 최종 URL: /customer/contacts/<번호>/active
@customer_bp.route("/contacts/<int:contact_id>/active", methods=["POST"])
def contacts_active(contact_id):
    """연락처를 내리거나 되살린다. 지우지는 않는다."""
    try:
        contacts.set_active(contact_id, request.form.get("active") == "1")
        flash("연락처를 갱신했습니다.", "success")
    except ContactError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("customer.index"))


# 최종 URL: /customer/standards
@customer_bp.route("/standards")
def standards_page():
    """이 고객사와 합의한 구성은 무엇이고, 지켜지고 있는가.

    컴플라이언스가 '누구에게나 통하는 모범사례' 라면 여기는 '이 고객사와
    합의한 것' 이다. 필수 태그는 여기서 정한 것이 컴플라이언스의 기본값을
    이긴다.
    """
    error, result, counts = None, None, None
    try:
        all_names = customer.names()
    except CustomerError as e:
        all_names, error = [], str(e)

    selected = request.args.get("customer") or (all_names[0] if all_names else "")

    if selected and not error:
        try:
            result = standards.evaluate(selected)
            counts = standards.summarize(result)
        except StandardError as e:
            error = str(e)

    return render_template(
        "customer_standards.html",
        customers=all_names, selected=selected,
        result=result, counts=counts, error=error,
        rules=standards.RULES,
    )


# 최종 URL: /customer/standards/save
@customer_bp.route("/standards/save", methods=["POST"])
def standards_save():
    """규칙을 등록하거나 고친다."""
    name = request.form.get("customer", "")
    try:
        standards.save(
            customer=name,
            rule=request.form.get("rule", ""),
            value=request.form.get("value", ""),
            note=request.form.get("note", ""),
        )
        flash("구성 표준을 저장했습니다.", "success")
    except StandardError as e:
        flash(str(e), "error")
    return redirect(url_for("customer.standards_page", customer=name))


# 최종 URL: /customer/standards/<번호>/delete
@customer_bp.route("/standards/<int:standard_id>/delete", methods=["POST"])
def standards_delete(standard_id):
    """규칙을 지운다."""
    try:
        standards.remove(standard_id)
        flash("규칙을 지웠습니다.", "success")
    except StandardError as e:
        flash(str(e), "error")
    return redirect(request.referrer or url_for("customer.standards_page"))


# 최종 URL: /customer/brief
@customer_bp.route("/brief")
def brief_page():
    """이 고객사에 대해 아는 것을 한 장으로.

    담당자가 바뀔 때 넘겨야 할 것이 화면 여섯 개에 흩어져 있어서, 실제
    인수인계 때는 사람이 화면을 돌며 다시 정리한다. 자료는 이미 다 있다.
    """
    error, text, all_names = None, "", []
    try:
        all_names = customer.names()
    except CustomerError as e:
        error = str(e)

    selected = request.args.get("customer") or (all_names[0] if all_names else "")

    if selected and not error:
        try:
            text = customer_brief.to_markdown(customer_brief.collect(selected))
        except BriefError as e:
            error = str(e)

    return render_template(
        "customer_brief.html",
        customers=all_names, selected=selected, text=text, error=error,
    )


# 최종 URL: /customer/brief.md
@customer_bp.route("/brief.md")
def brief_download():
    """인수인계 문서를 Markdown 으로 내려받는다.

    받는 사람이 이 앱을 쓰지 않을 수도 있고, 위키나 티켓에 그대로
    붙일 수 있어야 한다.
    """
    from urllib.parse import quote

    name = request.args.get("customer", "")
    try:
        text = customer_brief.to_markdown(customer_brief.collect(name))
    except BriefError as e:
        flash(str(e), "error")
        return redirect(url_for("customer.brief_page", customer=name))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"인수인계_{name}_{stamp}.md"
    return Response(
        text,
        mimetype="text/markdown; charset=utf-8",
        headers={
            # 한글 파일명은 latin-1 헤더에 그대로 못 들어간다. RFC 5987 로 낸다.
            "Content-Disposition":
                "attachment; filename=handover.md; "
                f"filename*=UTF-8''{quote(filename)}",
        },
    )
