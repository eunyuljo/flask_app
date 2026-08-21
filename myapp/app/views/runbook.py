# app/views/runbook.py
# 런북(알람 종류별 대응 절차) 블루프린트. url_prefix="/runbook" 으로 등록된다.
# 알람 화면에서 "이 알람에 절차 쓰기" 로 넘어오고, 여기서 목록과 편집을 한다.

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash
)

from app import runbook
from app.accounts import list_accounts, by_customer, AccountError
from app.runbook import RunbookError

runbook_bp = Blueprint("runbook", __name__)


@runbook_bp.before_request
def require_login():
    """런북은 로그인한 사용자만."""
    if not session.get("username"):
        flash("런북을 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _customers():
    """고객사 목록. DB 가 없으면 빈 목록으로 두고 공통 런북만 쓰게 한다."""
    try:
        return sorted(by_customer(list_accounts()))
    except AccountError:
        return []


# 최종 URL: /runbook/
@runbook_bp.route("/")
def index():
    """런북 목록."""
    error, items = None, []
    try:
        items = runbook.recent()
    except RunbookError as e:
        error = str(e)
    return render_template("runbook.html", items=items, error=error)


# 최종 URL: /runbook/new
@runbook_bp.route("/new")
def new():
    """새 런북 작성 화면.

    지문은 알람 화면에서 넘어온 쿼리스트링으로 채운다.
    사람이 16자리 해시를 손으로 옮겨 적게 하면 안 된다.
    """
    return render_template(
        "runbook_edit.html",
        item={
            "id": None,
            "fingerprint": request.args.get("fingerprint", ""),
            "customer": "",
            "title": request.args.get("title", ""),
            "body": "",
            "sample": request.args.get("sample", ""),
        },
        customers=_customers(),
    )


# 최종 URL: /runbook/<번호>/edit
@runbook_bp.route("/<int:runbook_id>/edit")
def edit(runbook_id):
    """기존 런북 수정 화면."""
    try:
        item = runbook.get(runbook_id)
    except RunbookError as e:
        flash(str(e), "error")
        return redirect(url_for("runbook.index"))

    if item is None:
        flash("런북을 찾지 못했습니다.", "error")
        return redirect(url_for("runbook.index"))

    return render_template("runbook_edit.html", item=item, customers=_customers())


# 최종 URL: /runbook/save
@runbook_bp.route("/save", methods=["POST"])
def save():
    """작성/수정 내용을 저장한다. 지문+고객사가 같으면 덮어쓴다."""
    try:
        runbook.save(
            fingerprint=request.form.get("fingerprint", ""),
            title=request.form.get("title", ""),
            body=request.form.get("body", ""),
            author=session.get("username", ""),
            customer=request.form.get("customer", ""),
            sample=request.form.get("sample", ""),
        )
        flash("런북을 저장했습니다.", "success")
    except RunbookError as e:
        flash(str(e), "error")
        # 입력을 날리지 않도록 작성 화면으로 되돌린다.
        return render_template(
            "runbook_edit.html",
            item={
                "id": None,
                "fingerprint": request.form.get("fingerprint", ""),
                "customer": request.form.get("customer", ""),
                "title": request.form.get("title", ""),
                "body": request.form.get("body", ""),
                "sample": request.form.get("sample", ""),
            },
            customers=_customers(),
        )

    return redirect(url_for("runbook.index"))


# 최종 URL: /runbook/<번호>/delete
@runbook_bp.route("/<int:runbook_id>/delete", methods=["POST"])
def delete(runbook_id):
    """런북을 지운다."""
    try:
        runbook.delete(runbook_id)
        flash("런북을 지웠습니다.", "success")
    except RunbookError as e:
        flash(str(e), "error")
    return redirect(url_for("runbook.index"))
