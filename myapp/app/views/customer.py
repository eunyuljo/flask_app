# app/views/customer.py
# 고객사 현황 블루프린트. url_prefix="/customer" 로 등록된다.
# 이 앱에 처음 생기는 '고객사 축' 화면이다. 나머지는 전부 기능 축이다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash
)

from app import access, audit, customer, readiness
from app.access import AccessError
from app.accounts import list_accounts, get_account, AccountError
from app.customer import CustomerError
from app.readiness import ReadinessError

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

    return render_template(
        "customer.html",
        names=all_names, selected=selected, data=data, error=error,
    )


# 최종 URL: /customer/readiness
# 새 블루프린트를 만들지 않고 여기에 붙인 이유는 축이 같기 때문이다.
# 이 화면도 "고객사 하나를 놓고 본다".
@customer_bp.route("/readiness")
def readiness_page():
    """이 고객사를 받을 준비가 됐는가."""
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
