# app.py
# Flask 애플리케이션의 진입점. create_app() 팩토리 함수로 앱 객체를 생성하고,
# views 패키지에 정의된 블루프린트들을 여기서 한 곳에 모아 등록한다.

from flask import Flask

# 블루프린트 객체를 import 한다.
# views/main.py 안의 main_bp, views/auth.py 안의 auth_bp 를 가져오는 것.
from views.main import main_bp
from views.auth import auth_bp


def create_app():
    """애플리케이션 팩토리(Application Factory) 패턴.

    모듈을 import 하는 순간 app 객체가 만들어지는 방식(전역 app = Flask(__name__))이 아니라,
    '함수를 호출할 때' 앱을 만들어 돌려주는 방식이다.
    이렇게 하면 테스트용 앱 / 개발용 앱 / 운영용 앱을 서로 다른 설정으로 여러 개 만들 수 있고,
    순환 import(app.py -> views -> app.py) 문제도 피하기 쉬워진다.
    """
    app = Flask(__name__)

    # session 과 flash 는 둘 다 '서명된 쿠키'를 사용하기 때문에 secret_key 가 반드시 필요하다.
    # 이 값이 없으면 로그인(session) 도, flash 메시지도 RuntimeError 를 내며 동작하지 않는다.
    # 실제 서비스에서는 코드에 박아두지 말고 환경변수 등에서 읽어와야 한다.
    app.secret_key = "dev-secret-key-change-me"

    # ------------------------------------------------------------------
    # 블루프린트(Blueprint) 등록
    # ------------------------------------------------------------------
    # 블루프린트는 "라우트들을 미리 모아둔 묶음"이다.
    # views/main.py, views/auth.py 에서 @main_bp.route(...) 로 라우트를 정의해도
    # 그 시점에는 아직 앱에 붙은 게 아니다. 아래 register_blueprint() 를 호출하는
    # 순간에 비로소 앱의 URL 라우팅 테이블에 실제로 등록된다.
    #
    # [url_prefix 동작 방식]
    # register_blueprint(bp, url_prefix="/auth") 를 주면,
    # 그 블루프린트 안의 모든 route 경로 앞에 "/auth" 가 자동으로 붙는다.
    #
    #   auth.py 에서:  @auth_bp.route("/login")   ->  실제 URL: /auth/login
    #   auth.py 에서:  @auth_bp.route("/logout")  ->  실제 URL: /auth/logout
    #
    # 즉 블루프린트 파일 안에서는 "/auth" 를 직접 쓰지 않는다.
    # prefix 를 어디에 붙일지는 등록하는 쪽(app.py)이 결정하기 때문에,
    # 나중에 "/auth" 를 "/account" 로 바꾸고 싶으면 아래 한 줄만 고치면 된다.
    #
    # [엔드포인트 이름 규칙]
    # 블루프린트에 속한 뷰 함수의 엔드포인트 이름은 "블루프린트이름.함수이름" 이 된다.
    # 이 '블루프린트이름'은 Blueprint("main", ...) 처럼 생성자 첫 번째 인자로 준 이름이다.
    #   main_bp = Blueprint("main", ...) + def index()  ->  url_for("main.index")
    #   auth_bp = Blueprint("auth", ...) + def login()  ->  url_for("auth.login")
    # 템플릿에서 링크를 만들 때 이 이름을 그대로 쓴다.

    # main 블루프린트: url_prefix 를 주지 않았으므로 경로가 그대로 사용된다.
    #   main.py 의 @main_bp.route("/")  ->  실제 URL: /
    app.register_blueprint(main_bp)

    # auth 블루프린트: url_prefix="/auth" 를 주었으므로 모든 경로 앞에 /auth 가 붙는다.
    #   auth.py 의 @auth_bp.route("/login")  ->  실제 URL: /auth/login
    app.register_blueprint(auth_bp, url_prefix="/auth")

    return app


# create_app() 을 호출해서 실제 앱 객체를 만든다.
# `flask run` 명령은 app.py 안의 `app` 이라는 이름의 변수를 자동으로 찾아 실행한다.
app = create_app()


if __name__ == "__main__":
    # `python app.py` 로 직접 실행했을 때 개발 서버를 띄운다.
    # debug=True 이면 코드를 저장할 때마다 서버가 자동 재시작되고, 에러 화면이 자세히 나온다.
    app.run(debug=True, port=5000)
