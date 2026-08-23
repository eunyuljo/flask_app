# app/views/runbook.py
# 런북(알람 종류별 대응 절차) 블루프린트. url_prefix="/runbook" 으로 등록된다.
# 알람 화면에서 "이 알람에 절차 쓰기" 로 넘어오고, 여기서 목록과 편집을 한다.

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash
)

from app import runbook
from app.accounts import list_accounts, by_customer, AccountError
from app.runbook import RunbookError, OUTCOMES

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
    """런북 목록. 각 런북이 실제로 쓰이고 있는지 함께 보여준다."""
    error, items, usage = None, [], None
    try:
        items = runbook.recent()
    except RunbookError as e:
        error = str(e)

    # 사용 현황은 따로 잡는다. 여기서 실패했을 때 usage 를 빈 dict 로 두면
    # 화면이 모든 런북에 "아직 쓰인 적 없음" 이라고 적는다. 그건 사실이
    # 아니라 '읽지 못했다' 이고, 둘을 섞으면 멀쩡한 절차가 죽은 문서로
    # 보인다(컴플라이언스에서 '위반 0건' 과 '점검 못 함' 을 나눈 것과 같다).
    if items:
        try:
            usage = runbook.usage([r["id"] for r in items])
        except RunbookError:
            usage = None

    return render_template("runbook.html", items=items, usage=usage, error=error)


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
        runs=[],
        outcomes=OUTCOMES,
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

    # 이 절차를 따른 기록. 절차를 고치러 온 사람이 가장 먼저 봐야 할 것이
    # "지난번에 이게 왜 안 됐는지" 다.
    #
    # 읽지 못하면 None 이다. 빈 목록으로 두면 화면이 "따른 적 없다" 고
    # 단언하는데, 그건 모르는 것과 다르다.
    try:
        history = runbook.runs(runbook_id, item["fingerprint"], limit=20)
    except RunbookError:
        history = None

    return render_template("runbook_edit.html", item=item, customers=_customers(),
                           runs=history, outcomes=OUTCOMES)


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
            runs=[],
            outcomes=OUTCOMES,
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


# 최종 URL: /runbook/<번호>/run
@runbook_bp.route("/<int:runbook_id>/run", methods=["POST"])
def run(runbook_id):
    """이 절차를 따랐다는 기록을 남긴다.

    알람 화면과 런북 화면 양쪽에서 같은 폼이 이리로 온다. 저장한 뒤에는
    referrer 로 돌아간다 - 알람 목록에서 눌렀는데 런북 목록으로 튕기면
    처리하던 알람을 다시 찾아야 한다.
    """
    # back 은 폼에 실려 온 값이라 사용자가 무엇이든 넣을 수 있다.
    # 이 앱 안의 경로일 때만 쓴다 - 그러지 않으면 '기록' 버튼 하나로
    # 바깥 주소로 보내는 발판이 된다.
    back = request.form.get("back", "")
    if not back.startswith("/") or back.startswith("//"):
        back = url_for("runbook.index")
    try:
        runbook.record_run(
            runbook_id,
            outcome=request.form.get("outcome", ""),
            ran_by=session.get("username", ""),
            note=request.form.get("note", ""),
            minutes=request.form.get("minutes", 0),
            account_id=request.form.get("account_id", ""),
            event_id=request.form.get("event_id", ""),
            customer=request.form.get("customer", ""),
        )
        flash("런북 실행을 기록했습니다.", "success")
    except RunbookError as e:
        flash(str(e), "error")
    return redirect(back)


# 최종 URL: /runbook/runs
@runbook_bp.route("/runs")
def runs():
    """실행 기록 전체. 어떤 절차가 쓰이고 어떤 절차가 말썽인지 본다."""
    error, items, stats = None, [], None
    try:
        items = runbook.runs(limit=200)
        stats = runbook.summary()
    except RunbookError as e:
        error = str(e)
    return render_template("runbook_runs.html", items=items, stats=stats,
                           outcomes=OUTCOMES, error=error)
