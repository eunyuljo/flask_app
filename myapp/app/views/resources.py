# app/views/resources.py
# 리소스 스냅샷 비교 화면. 두 시점의 인프라 상태 차이를 보여준다.
# app/__init__.py 에서 url_prefix="/resources" 로 등록된다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash, current_app
)

from urllib.parse import quote

from flask import Response

from app import inventory
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

    return render_template(
        "inventory.html",
        error=error, result=result, facets=facets, scopes=scopes, **args,
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
