# app/views/auth.py
# 로그인/로그아웃을 담당하는 블루프린트. app/__init__.py 에서 url_prefix="/auth" 로 등록되어
# 이 파일의 "/login", "/logout" 은 실제로 "/auth/login", "/auth/logout" 으로 서비스된다.

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)

# 블루프린트 이름은 "auth" -> 엔드포인트는 auth.login, auth.logout 이 된다.
auth_bp = Blueprint("auth", __name__)

# DB 없이 학습용으로 하드코딩한 계정 1개.
# 실제 서비스에서는 절대 이렇게 하지 말고, DB에 해시된 비밀번호를 저장해야 한다.
VALID_USERNAME = "admin"
VALID_PASSWORD = "1234"


# 주의: 여기에는 "/auth/login" 이 아니라 "/login" 이라고만 쓴다.
# "/auth" 는 app/__init__.py 의 register_blueprint(..., url_prefix="/auth") 가 자동으로 붙여준다.
# 만약 여기에 "/auth/login" 이라고 쓰면 최종 URL 이 "/auth/auth/login" 이 되어버린다.
#
# methods=["GET", "POST"] : 하나의 URL 로 두 가지 요청을 모두 처리한다.
#   GET  -> 로그인 폼(HTML) 을 보여준다
#   POST -> 폼이 제출한 아이디/비밀번호를 검사한다
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """로그인 폼 표시(GET) 와 로그인 처리(POST)."""
    if request.method == "POST":
        # request.form 은 <form method="post"> 로 전송된 값을 담고 있는 딕셔너리다.
        # 키는 <input> 태그의 name 속성과 같아야 한다.
        # .get(키, "") 을 쓰면 값이 없을 때 KeyError 대신 빈 문자열을 돌려준다.
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == VALID_USERNAME and password == VALID_PASSWORD:
            # 로그인 성공: session 에 사용자 이름을 저장한다.
            # session 은 서명된 쿠키에 저장되며, 다음 요청에서도 계속 유지된다.
            session["username"] = username

            # flash: "다음 요청 한 번에만 보여줄 메시지"를 넣어두는 기능.
            # 여기서 넣은 메시지는 redirect 로 이동한 페이지에서 한 번 출력된 뒤 사라진다.
            # 두 번째 인자 "success" 는 카테고리로, 템플릿에서 스타일을 구분할 때 쓴다.
            flash(f"{username}님, 로그인되었습니다.", "success")

            # 로그인 성공 시 메인 페이지로 이동한다.
            # url_for("main.index") 는 main 블루프린트의 index 함수가 담당하는 URL("/") 을 만들어준다.
            # 경로를 문자열로 직접 쓰지 않고 url_for 를 쓰면, 나중에 URL 규칙이 바뀌어도 코드를 고칠 필요가 없다.
            return redirect(url_for("main.index"))

        # 로그인 실패: 에러 메시지를 flash 하고 다시 로그인 폼을 보여준다.
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
        return redirect(url_for("auth.login"))

    # GET 요청이면 로그인 폼을 그려서 보여준다.
    return render_template("login.html")


# 최종 URL: /auth/logout  (url_prefix "/auth" + 이 파일의 "/logout")
@auth_bp.route("/logout")
def logout():
    """로그아웃 처리. session 에서 사용자 정보를 지운다."""
    # pop 의 두 번째 인자로 None 을 주면, 키가 없어도 에러 없이 그냥 넘어간다.
    session.pop("username", None)
    flash("로그아웃되었습니다.", "success")
    return redirect(url_for("main.index"))
