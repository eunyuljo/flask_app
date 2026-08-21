# app/views/resources.py
# 리소스 스냅샷 비교 화면. 두 시점의 인프라 상태 차이를 보여준다.
# app/__init__.py 에서 url_prefix="/resources" 로 등록된다.

from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash, current_app
)

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
