# app/views/explore.py
# 이벤트 탐색 화면. PromQL 을 흉내 낸 질의어를 입력받아 결과를 보여준다.
# app/__init__.py 에서 url_prefix="/explore" 로 등록된다.

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    current_app,
)

from app.query import run, QueryError, EXAMPLES, LABELS

explore_bp = Blueprint("explore", __name__)

ALLOWED_HOURS = (6, 24, 72, 168)
DEFAULT_QUERY = '{severity=~"critical|error"}'


@explore_bp.before_request
def require_login():
    if not session.get("username"):
        flash("탐색 화면을 보려면 먼저 로그인해 주세요.", "error")
        return redirect(url_for("auth.login"))


@explore_bp.route("/")
def index():
    """질의어를 받아 실행하고 결과를 그린다.

    질의어는 쿼리스트링(?q=)으로 받는다. 그래야 결과 화면 주소를 그대로
    복사해서 공유하거나 즐겨찾기에 넣을 수 있다.
    """
    text = request.args.get("q", DEFAULT_QUERY)

    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    if hours not in ALLOWED_HOURS:
        hours = 24

    result = None
    error = None
    try:
        uri = current_app.config["DATABASE_URI"].replace(
            "postgresql+psycopg://", "postgresql://"
        )
        result = run(uri, text, hours)
    except QueryError as e:
        # 문법 오류는 사용자가 고칠 수 있는 문제이므로 그대로 보여준다.
        error = str(e)
    except ImportError:
        error = "psycopg 가 설치되어 있지 않습니다. pip install -r requirements.txt 를 실행하세요."
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            error = f"DB 에 접속하지 못했습니다: {e}"
        else:
            raise

    return render_template(
        "explore.html",
        query=text,
        hours=hours,
        allowed_hours=ALLOWED_HOURS,
        result=result,
        error=error,
        examples=EXAMPLES,
        labels=sorted(LABELS),
    )
