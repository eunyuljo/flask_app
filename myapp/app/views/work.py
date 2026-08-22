# app/views/work.py
# 작업 기록(변경 증적) 블루프린트. app/__init__.py 에서 url_prefix="/work" 로 등록된다.
# 작업 전/후 스냅샷을 찍고, 그 차이를 고객사에 낼 증적 문서로 뽑는다.

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    session, flash, current_app, Response
)

from app import audit, users, work
from app.accounts import list_accounts, get_account, by_customer, AccountError
from app.aws_session import get_env, is_demo, SessionError
from app.collect import demo_resources, aws_resources, CollectError
from app.evidence import to_markdown
from app.resources import save_snapshot, diff, psycopg_uri, ResourceError
from app.work import WorkError, STATUS_LABEL

work_bp = Blueprint("work", __name__)


@work_bp.before_request
def require_login():
    """작업 기록은 로그인한 사용자만."""
    if not session.get("username"):
        flash("작업 기록을 쓰려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _take_snapshot(account, region, note):
    """그 계정의 리소스를 한 벌 수집해 스냅샷으로 저장하고 snapshot_id 를 돌려준다.

    데모 계정은 drift=0 으로 수집한다. 작업 전후로 두 번 찍는 게 목적인데,
    합성 데이터가 저 혼자 바뀌면 '작업 때문에 바뀐 것' 과 구분할 수 없어
    증적이 통째로 의미를 잃기 때문이다.
    """
    uri = psycopg_uri()
    account_id = account["account_id"]

    if is_demo(account):
        items = demo_resources(uri, account_id, region, drift=0)
        source = "demo"
    else:
        env = get_env(account, region)
        items, real_account_id = aws_resources(region, env)
        # 실제로 들어간 계정이 고른 계정과 다르면 증적이 엉뚱한 계정 것이 된다.
        if real_account_id != account_id:
            raise CollectError(
                f"자격증명이 가리키는 계정이 다릅니다: "
                f"선택 {account_id}, 실제 {real_account_id}"
            )
        source = "aws"

    return save_snapshot(uri, items, account_id=account_id, region=region,
                         source=source, note=note)


def _diff_for(item):
    """작업 기록에 붙은 두 스냅샷의 차이. 아직 둘 다 없으면 None."""
    if not (item["before_snapshot_id"] and item["after_snapshot_id"]):
        return None, None
    try:
        return diff(psycopg_uri(),
                    base_id=item["before_snapshot_id"],
                    target_id=item["after_snapshot_id"]), None
    except ResourceError as e:
        return None, str(e)


# 최종 URL: /work/
@work_bp.route("/")
def index():
    """작업 기록 목록과 새 작업 폼."""
    error = None
    items, accounts = [], []
    try:
        items = work.recent(30)
    except WorkError as e:
        error = str(e)
    try:
        accounts = list_accounts()
    except AccountError as e:
        error = error or str(e)

    # 승인 대기를 목록 위에 따로 둔다. 승인자가 가장 먼저 보는 것이고,
    # 섞여 있으면 자기가 눌러야 할 것이 있는지 훑어야 알게 된다.
    waiting = []
    try:
        waiting = work.pending()
    except WorkError:
        pass

    return render_template(
        "work.html",
        items=items,
        waiting=waiting,
        grouped=by_customer(accounts),
        labels=STATUS_LABEL,
        error=error,
        can_approve=users.can(session.get("role"), "admin"),
        me=session.get("username", ""),
    )


# 최종 URL: /work/new
@work_bp.route("/new", methods=["POST"])
def new():
    """작업 기록을 만든다."""
    account_id = request.form.get("account_id", "")
    region = request.form.get("region", "")

    try:
        account = get_account(account_id)
        if account is None:
            raise WorkError("계정을 고르세요.")

        # 리전은 그 계정에 등록된 것 중에서만.
        regions = account.get("regions") or []
        if region not in regions:
            region = regions[0] if regions else ""

        work_id = work.create(
            title=request.form.get("title", ""),
            customer=account["customer"],
            account_id=account_id,
            region=region,
            operator=session.get("username", ""),
            ticket=request.form.get("ticket", ""),
            request=request.form.get("request", ""),
            expected=request.form.get("expected", ""),
            requested_by=session.get("username", ""),
            rollback=request.form.get("rollback", ""),
            window_start=_when(request.form.get("window_start", "")),
            window_end=_when(request.form.get("window_end", "")),
        )
    except (WorkError, AccountError) as e:
        flash(str(e), "error")
        return redirect(url_for("work.index"))

    return redirect(url_for("work.detail", work_id=work_id))


# 최종 URL: /work/<번호>
@work_bp.route("/<int:work_id>")
def detail(work_id):
    """작업 하나의 상세. 스냅샷 찍기 / 차이 보기 / 증적 확정."""
    try:
        item = work.get(work_id)
    except WorkError as e:
        flash(str(e), "error")
        return redirect(url_for("work.index"))

    if item is None:
        flash("작업 기록을 찾지 못했습니다.", "error")
        return redirect(url_for("work.index"))

    rdiff, diff_error = _diff_for(item)
    return render_template(
        "work_detail.html",
        item=item,
        rdiff=rdiff,
        diff_error=diff_error,
        labels=STATUS_LABEL,
        window=work.window_state(item),
        can_approve=users.can(session.get("role"), "admin"),
        me=session.get("username", ""),
    )


# 최종 URL: /work/<번호>/snapshot
@work_bp.route("/<int:work_id>/snapshot", methods=["POST"])
def snapshot(work_id):
    """작업 전 또는 작업 후 스냅샷을 찍어 붙인다."""
    phase = request.form.get("phase", "")
    try:
        item = work.get(work_id)
        if item is None:
            raise WorkError("작업 기록을 찾지 못했습니다.")

        account = get_account(item["account_id"])
        if account is None:
            raise WorkError(
                f"계정 {item['account_id']} 이 등록 목록에 없습니다. "
                "삭제되었거나 비활성화되었을 수 있습니다."
            )

        # 수집하기 전에 상태부터 본다. 순서가 반대면, 거부될 요청인데도
        # 이미 고객사 계정에 조회를 다 날린 뒤가 되고 쓸모없는 스냅샷이 남는다.
        # (attach_snapshot 이 UPDATE ... WHERE status 로 다시 확인하므로,
        #  동시에 두 번 눌렀을 때 둘 다 통과하는 일은 그쪽에서 막힌다.)
        work.check_transition(item, phase)

        # 작업창을 벗어났으면 막지 않고 표시만 남긴다. 막으면 급할 때
        # 이 도구를 통째로 우회하고, 그러면 증적이 아예 안 남는다.
        if phase == "before":
            state = work.window_state(item)
            if state["has_window"] and not state["inside"]:
                work.mark_out_of_window(work_id)
                flash(f"작업창을 벗어나 시작했습니다. {state['note']} "
                      "증적에 그대로 남습니다.", "error")

        label = "작업 전" if phase == "before" else "작업 후"
        snapshot_id = _take_snapshot(account, item["region"],
                                     f"작업 #{work_id} {label}")
        work.attach_snapshot(work_id, phase, snapshot_id)
        flash(f"{label} 스냅샷 #{snapshot_id} 을 저장했습니다.", "success")
    except (WorkError, CollectError, SessionError) as e:
        flash(str(e), "error")

    return redirect(url_for("work.detail", work_id=work_id))


# 최종 URL: /work/<번호>/close
@work_bp.route("/<int:work_id>/close", methods=["POST"])
def close(work_id):
    """증적을 확정한다."""
    try:
        work.close(work_id, request.form.get("note", ""))
        flash("증적을 확정했습니다.", "success")
    except WorkError as e:
        flash(str(e), "error")
    return redirect(url_for("work.detail", work_id=work_id))


# 최종 URL: /work/<번호>/evidence.md
@work_bp.route("/<int:work_id>/evidence.md")
def evidence(work_id):
    """증적 문서를 Markdown 파일로 내려받는다."""
    try:
        item = work.get(work_id)
    except WorkError as e:
        flash(str(e), "error")
        return redirect(url_for("work.index"))

    if item is None:
        flash("작업 기록을 찾지 못했습니다.", "error")
        return redirect(url_for("work.index"))

    rdiff, _ = _diff_for(item)
    text = to_markdown(item, rdiff)

    ticket = item["ticket"] or f"work-{item['id']}"
    safe = "".join(c for c in ticket if c.isalnum() or c in "-_")
    return Response(
        text,
        # charset 을 두 번 붙이지 않도록 mimetype 만 준다.
        mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="evidence-{safe}.md"'},
    )


def _when(raw):
    """<input type="datetime-local"> 값을 UTC 로 읽는다.

    브라우저는 시간대 없이 "2026-08-22T14:30" 을 보낸다. 어디 시각인지
    적혀 있지 않으므로 이 앱의 다른 모든 시각과 같이 UTC 로 읽는다.
    화면에도 UTC 라고 밝힌다 - 안 밝히면 각자 자기 시간대로 읽는다.
    """
    from datetime import datetime, timezone

    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        raise WorkError(f"시각 형식을 읽지 못했습니다: {raw}")


def _require_approver():
    """승인은 관리자만. 메뉴를 숨기는 것이 아니라 여기서 막는다."""
    if not users.can(session.get("role"), "admin"):
        flash("승인은 관리자만 할 수 있습니다.", "error")
        return False
    return True


@work_bp.route("/<int:work_id>/approve", methods=["POST"])
def approve(work_id):
    """작업을 승인한다."""
    if not _require_approver():
        return redirect(url_for("work.detail", work_id=work_id))
    try:
        work.approve(work_id, session.get("username", ""),
                     request.form.get("note", ""))
    except WorkError as e:
        flash(str(e), "error")
        return redirect(url_for("work.detail", work_id=work_id))

    audit.record(
        action="work_approval", outcome="ok",
        summary=f"작업 승인: #{work_id}",
        detail=request.form.get("note", "")[:300],
    )
    flash("승인했습니다. 이제 작업 전 스냅샷을 찍을 수 있습니다.", "success")
    return redirect(url_for("work.detail", work_id=work_id))


@work_bp.route("/<int:work_id>/reject", methods=["POST"])
def reject(work_id):
    """작업을 반려한다."""
    if not _require_approver():
        return redirect(url_for("work.detail", work_id=work_id))
    try:
        work.reject(work_id, session.get("username", ""),
                    request.form.get("note", ""))
    except WorkError as e:
        flash(str(e), "error")
        return redirect(url_for("work.detail", work_id=work_id))

    audit.record(
        action="work_approval", outcome="rejected",
        summary=f"작업 반려: #{work_id}",
        detail=request.form.get("note", "")[:300],
    )
    flash("반려했습니다.", "success")
    return redirect(url_for("work.detail", work_id=work_id))
