# app/views/resources.py
# 리소스 스냅샷 비교 화면. 두 시점의 인프라 상태 차이를 보여준다.
# app/__init__.py 에서 url_prefix="/resources" 로 등록된다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash, current_app
)

from datetime import datetime, timezone
from urllib.parse import quote

from flask import Response

from app import alarm_advice, alarm_link, compliance, graph, inventory
from app.alarm_advice import AdviceError
from app.compliance import ComplianceError
from app.customer import names as customer_names, CustomerError
from app.inventory import InventoryError
from app.resources import diff, list_snapshots, psycopg_uri, ResourceError

resources_bp = Blueprint("resources", __name__)


@resources_bp.before_request
def require_login():
    if not session.get("username"):
        flash("리소스 화면을 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


def _int_arg(name):
    """쿼리스트링에서 정수를 꺼낸다. 숫자가 아니면 None."""
    raw = request.args.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@resources_bp.route("/")
def index():
    base_id = _int_arg("base")
    target_id = _int_arg("target")

    result = None
    snapshots = []
    error = None
    try:
        uri = psycopg_uri()
        snapshots = list_snapshots(uri)
        result = diff(uri, base_id, target_id)
    except ResourceError as e:
        error = str(e)
    except ImportError:
        error = "psycopg 가 설치되어 있지 않습니다. pip install -r requirements.txt 를 실행하세요."
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            error = f"DB 에 접속하지 못했습니다: {e}"
        else:
            raise

    return render_template(
        "resources.html",
        result=result,
        snapshots=snapshots,
        error=error,
    )


# ----------------------------------------------------------------------
# 인벤토리 — 지금 무엇이 떠 있는가
# ----------------------------------------------------------------------
# 새 블루프린트를 만들지 않고 여기 붙였다. 같은 자료(resources 테이블)를
# 다른 각도로 보는 것뿐이다.
#   /resources/          무엇이 바뀌었나 (두 시점의 차이)
#   /resources/inventory 무엇이 있나 (지금 목록)


def _inventory_args():
    return {
        "account_id": request.args.get("account", ""),
        "region": request.args.get("region", ""),
        "resource_type": request.args.get("type", ""),
        "q": request.args.get("q", "").strip(),
        "missing_tag": request.args.get("missing_tag", ""),
    }


@resources_bp.route("/inventory")
def inventory_page():
    """지금 떠 있는 리소스 목록."""
    args = _inventory_args()
    error, result, facets, scopes = None, None, None, []
    try:
        scopes = inventory.scopes()
        facets = inventory.facets(args["account_id"] or None, args["region"] or None)
        result = inventory.current(**{k: v or None if k != "q" else v
                                      for k, v in args.items()})
    except InventoryError as e:
        error = str(e)

    # 이 리소스에 최근 어떤 알람이 왔나. 어댑터가 CloudWatch 차원을 읽기
    # 전에는 물을 수 없던 질문이다.
    #
    # 못 읽어도 목록은 떠야 한다. 다만 '알람 0건' 으로 보이면 안 되므로
    # 못 읽었다는 사실을 화면에 넘긴다.
    alarms, alarms_checked = {}, False
    if result:
        try:
            alarms = alarm_link.seen(
                account_ids=[args["account_id"]] if args["account_id"] else None)
            alarms_checked = True
        except alarm_link.LinkError:
            pass

    return render_template(
        "inventory.html",
        error=error, result=result, facets=facets, scopes=scopes,
        alarms=alarms, alarms_checked=alarms_checked, **args,
    )


@resources_bp.route("/inventory/download.xlsx")
def inventory_download():
    """지금 목록을 엑셀로. 인수인계와 감사에서 매번 요구되는 형태다."""
    from app.inventory_xlsx import build, ExcelNotAvailable

    args = _inventory_args()
    try:
        result = inventory.current(
            **{k: v or None if k != "q" else v for k, v in args.items()},
            limit=20000,
        )
        buf = build(result, args)
    except (InventoryError, ExcelNotAvailable) as e:
        flash(str(e), "error")
        return redirect(url_for("resources.inventory_page", **args))

    stamp = max((s["collected_at"] for s in result["snapshots"]), default=None)
    stamp = stamp.strftime("%Y%m%d") if stamp else "unknown"
    name = f"리소스목록-{args['account_id'] or '전체'}-{stamp}.xlsx"
    ascii_name = f"inventory-{args['account_id'] or 'all'}-{stamp}.xlsx"
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(name)}"
    )
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


# 최종 URL: /resources/impact
@resources_bp.route("/impact")
def impact_page():
    """이 리소스를 건드리면 무엇이 딸려 오나.

    '의존 관계도' 라고 부르지 않는다. 지금 수집기가 훑는 것은 세 종류뿐이고,
    그걸로 전체 관계도를 그렸다고 하면 화면에 안 보이는 것을 없는 것으로
    읽게 된다. 물음을 좁히는 대신 그 물음에는 정확히 답한다.
    """
    error, snap, snapshots = None, None, []
    selected_snapshot = request.args.get("snapshot_id", type=int)
    target = request.args.get("resource_id", "")

    try:
        snapshots = compliance.latest_snapshots(limit=50)
    except ComplianceError as e:
        error = str(e)

    if snapshots and not selected_snapshot:
        selected_snapshot = snapshots[0]["snapshot_id"]

    if selected_snapshot and not error:
        try:
            with compliance._connect() as conn, conn.cursor() as cur:
                snap = compliance.load_snapshot(cur, selected_snapshot)
        except Exception as e:                   # noqa: BLE001
            error = f"스냅샷을 읽지 못했습니다: {e}"

    result, choices, islands = None, [], []
    if snap is not None:
        choices = graph.nodes(snap)
        islands = graph.islands(snap)
        if target:
            result = graph.impact(snap, target)

    return render_template(
        "resources_impact.html",
        snapshots=snapshots, selected_snapshot=selected_snapshot,
        choices=choices, islands=islands, target=target,
        result=result, blind=graph.BLIND, error=error,
    )


# 최종 URL: /resources/alarm-advice
@resources_bp.route("/alarm-advice")
def alarm_advice_page():
    """이 고객사 리소스에 어떤 알람을 걸어야 하는가.

    걸려 있는지는 보지 않는다. 실제 알람은 모니터링 서버에 있고 이 도구는
    그 설정을 읽지 않는다. 그래서 '없음' 이라고 말하지 않는다.
    """
    error, data, names = None, None, []
    try:
        names = customer_names()
    except CustomerError as e:
        error = str(e)

    selected = request.args.get("customer") or (names[0] if names else "")

    if selected and not error:
        try:
            data = alarm_advice.for_customer(selected)
        except AdviceError as e:
            error = str(e)

    return render_template(
        "resources_alarm_advice.html",
        customers=names, selected=selected, data=data, error=error,
        levels=alarm_advice.LEVELS,
    )


# 최종 URL: /resources/alarm-advice.xlsx
@resources_bp.route("/alarm-advice.xlsx")
def alarm_advice_download():
    """권고 표를 엑셀로 내려받는다.

    이 표를 읽을 사람은 이 앱을 쓰지 않는다. 모니터링 서버 담당자가
    목록을 받아 하나씩 설정하는 것이라, 화면으로만 두면 스크린샷을 찍어
    보내게 된다.
    """
    from app.alarm_advice_xlsx import build as build_xlsx, ExcelNotAvailable

    name = request.args.get("customer", "")
    try:
        data = alarm_advice.for_customer(name)
        blob = build_xlsx(data)
    except (AdviceError, ExcelNotAvailable) as e:
        flash(str(e), "error")
        return redirect(url_for("resources.alarm_advice_page", customer=name))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"알람권고_{name}_{stamp}.xlsx"
    return Response(
        blob,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            # 한글 파일명은 latin-1 헤더에 그대로 못 들어간다. RFC 5987 로 낸다.
            "Content-Disposition":
                "attachment; filename=alarm-advice.xlsx; "
                f"filename*=UTF-8''{quote(filename)}",
        },
    )
