# app/views/customer.py
# 고객사 현황 블루프린트. url_prefix="/customer" 로 등록된다.
# 이 앱에 처음 생기는 '고객사 축' 화면이다. 나머지는 전부 기능 축이다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash
)

from app import customer, readiness
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
