# views/main.py
# 메인(공개) 페이지를 담당하는 블루프린트. url_prefix 없이 등록되므로 "/" 가 그대로 최상위 URL이 된다.

from flask import Blueprint, render_template, session

# Blueprint 객체 생성
#   첫 번째 인자 "main"  : 블루프린트의 이름. url_for("main.index") 의 앞부분이 된다.
#   두 번째 인자 __name__ : 이 블루프린트가 어느 모듈에 있는지 Flask 에게 알려주는 값.
#                          템플릿/정적 파일 경로를 찾는 기준점으로 쓰인다.
main_bp = Blueprint("main", __name__)


# 이 파일 안에서는 "/" 라고만 쓴다.
# app.py 에서 url_prefix 없이 등록했으므로 최종 URL 도 그대로 "/" 가 된다.
# 엔드포인트 이름은 "블루프린트이름.함수이름" 규칙에 따라 "main.index" 가 된다.
@main_bp.route("/")
def index():
    """인덱스 페이지. 로그인 여부에 따라 다른 내용을 보여준다."""
    # 로그인할 때 session 에 저장해 둔 사용자 이름을 꺼낸다.
    # 로그인하지 않았다면 키가 없으므로 기본값 None 이 반환된다.
    username = session.get("username")
    return render_template("index.html", username=username)
