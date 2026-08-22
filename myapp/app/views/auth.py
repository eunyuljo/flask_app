# app/views/auth.py
# 로그인/로그아웃을 담당하는 블루프린트. app/__init__.py 에서 url_prefix="/auth" 로 등록되어
# 이 파일의 "/login", "/logout" 은 실제로 "/auth/login", "/auth/logout" 으로 서비스된다.
#
# 계정 검사 자체는 app/users.py 가 한다. 이 파일은 "요청을 받아서 세션에 넣는" 일만 한다.
# 예전에는 여기에 아이디/비밀번호가 하드코딩되어 있었다.

from flask import (
    Blueprint,
    current_app,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)

from app import users

# 블루프린트 이름은 "auth" -> 엔드포인트는 auth.login, auth.logout 이 된다.
auth_bp = Blueprint("auth", __name__)


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

        user = users.authenticate(username, password)
        if user:
            # 로그인 성공. 세션은 서명된 쿠키에 저장되며 다음 요청에서도 유지된다.
            #
            # 이전 세션에 남아 있던 값을 먼저 지운다. 로그인할 때마다 세션을
            # 새로 시작해야, 로그인 전에 심어둔 쿠키를 그대로 이어받는
            # 세션 고정(session fixation) 공격을 막을 수 있다.
            session.clear()
            session["username"] = user["username"]
            # 역할을 세션에 넣어두면 화면마다 DB 를 다시 보지 않아도 된다.
            # 대신 역할을 바꾼 뒤에는 다시 로그인해야 반영된다.
            session["role"] = user["role"]
            if user.get("bootstrap"):
                session["bootstrap"] = True
            # permanent 세션이라야 PERMANENT_SESSION_LIFETIME(만료 시간)이 적용된다.
            # 이걸 켜지 않으면 브라우저를 닫을 때까지 무제한으로 유지된다.
            session.permanent = True

            # flash: "다음 요청 한 번에만 보여줄 메시지"를 넣어두는 기능.
            # 여기서 넣은 메시지는 redirect 로 이동한 페이지에서 한 번 출력된 뒤 사라진다.
            # 두 번째 인자 "success" 는 카테고리로, 템플릿에서 스타일을 구분할 때 쓴다.
            flash(f"{user['username']}님, 로그인되었습니다.", "success")

            # 로그인 성공 시 메인 페이지로 이동한다.
            # url_for("main.index") 는 main 블루프린트의 index 함수가 담당하는 URL("/") 을 만들어준다.
            # 경로를 문자열로 직접 쓰지 않고 url_for 를 쓰면, 나중에 URL 규칙이 바뀌어도 코드를 고칠 필요가 없다.
            return redirect(url_for("main.index"))

        # 로그인 실패: 에러 메시지를 flash 하고 다시 로그인 폼을 보여준다.
        # "아이디가 없다" 와 "비밀번호가 틀렸다" 를 구분해서 알려주지 않는다.
        # 구분해 주면 어떤 아이디가 존재하는지 알려주는 셈이 된다.
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
        return redirect(url_for("auth.login"))

    # GET 요청이면 로그인 폼을 그려서 보여준다.
    return render_template(
        "login.html",
        # 계정이 하나도 없으면 화면에서 그 사실을 알려준다.
        # 이 검사는 로그인 화면에서만 한다(매 요청마다 DB 를 보지 않기 위해).
        bootstrap=users.bootstrap_mode(),
        bootstrap_username=users.BOOTSTRAP_USERNAME,
        lifetime=current_app.config["PERMANENT_SESSION_LIFETIME"],
    )


# 최종 URL: /auth/logout  (url_prefix "/auth" + 이 파일의 "/logout")
@auth_bp.route("/logout")
def logout():
    """로그아웃 처리. 세션을 통째로 비운다."""
    # pop 으로 키를 하나씩 지우면 나중에 세션에 값을 추가했을 때 빠뜨리기 쉽다.
    session.clear()
    flash("로그아웃되었습니다.", "success")
    return redirect(url_for("main.index"))
