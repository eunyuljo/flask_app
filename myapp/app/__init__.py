# app/__init__.py
# Flask 애플리케이션 패키지의 시작점. create_app() 팩토리 함수로 앱 객체를 생성하고,
# views 패키지에 정의된 블루프린트들을 여기서 한 곳에 모아 등록한다.
#
# 이 파일이 __init__.py 라서 app 디렉터리 자체가 하나의 파이썬 패키지가 된다.
# 그래서 바깥에서는 `from app import create_app` 한 줄로 앱을 만들 수 있다.

import os

from flask import Flask

# 환경별 설정 클래스들이 담긴 딕셔너리. config["development"] 처럼 이름으로 꺼내 쓴다.
from app.cli import register_cli
from app.config import config

# 블루프린트 객체를 import 한다.
# app/views/main.py 안의 main_bp, app/views/auth.py 안의 auth_bp 를 가져오는 것.
from app.views.main import main_bp
from app.views.auth import auth_bp
from app.views.agent import agent_bp
from app.views.alarm import alarm_bp
from app.views.admin import admin_bp
from app.views.dashboard import dashboard_bp
from app.views.explore import explore_bp
from app.views.resources import resources_bp
from app.views.report import report_bp
from app.views.console import console_bp
from app.views.work import work_bp
from app.views.runbook import runbook_bp
from app.views.handover import handover_bp
from app.views.incident import incident_bp
from app.views.noise import noise_bp
from app.views.customer import customer_bp
from app.views.compliance import compliance_bp


def create_app(config_name=None):
    """애플리케이션 팩토리(Application Factory) 패턴.

    모듈을 import 하는 순간 app 객체가 만들어지는 방식(전역 app = Flask(__name__))이 아니라,
    '함수를 호출할 때' 앱을 만들어 돌려주는 방식이다.
    이렇게 하면 테스트용 앱 / 개발용 앱 / 운영용 앱을 서로 다른 설정으로 여러 개 만들 수 있고,
    순환 import(app.py -> views -> app.py) 문제도 피하기 쉬워진다.

    config_name: "development" / "testing" / "production" 중 하나.
                 생략하면 FLASK_CONFIG 환경변수를 보고, 그것도 없으면 "default"(개발용)를 쓴다.
    """
    app = Flask(__name__)

    # ------------------------------------------------------------------
    # 설정 로드
    # ------------------------------------------------------------------
    # 팩토리 패턴의 핵심 장점이 바로 이 부분이다.
    # 인자로 어떤 설정을 넘기느냐에 따라 완전히 다른 성격의 앱이 만들어진다.
    #   create_app("development") -> 디버그 켜짐, SQL 로그 출력
    #   create_app("testing")     -> 메모리 SQLite 사용 (테스트 코드에서 호출)
    #   create_app("production")  -> 디버그 꺼짐, 필수 환경변수 검사
    if config_name is None:
        config_name = os.environ.get("FLASK_CONFIG", "default")

    # from_object() 는 클래스에 정의된 '대문자 속성'만 골라서 app.config 에 복사한다.
    # 즉 SECRET_KEY, SQLALCHEMY_DATABASE_URI 는 들어가고, init_app 같은 메서드는 무시된다.
    # 이후 app.config["SECRET_KEY"] 처럼 딕셔너리로 꺼내 쓸 수 있다.
    app.config.from_object(config[config_name])

    # 어떤 환경으로 떴는지 나중에 확인할 수 있도록 이름 자체도 남겨둔다.
    # (관리자 대시보드에서 이 값을 보여준다.)
    app.config["CONFIG_NAME"] = config_name

    # 설정 클래스가 앱에 대해 추가로 할 일(운영 환경 필수값 검사 등)을 수행한다.
    config[config_name].init_app(app)

    # session 과 flash 는 둘 다 '서명된 쿠키'를 사용하기 때문에 secret_key 가 반드시 필요하다.
    # 이제 그 값은 위의 from_object() 를 통해 config.py 에서 들어온다.
    # (app.secret_key 와 app.config["SECRET_KEY"] 는 같은 값을 가리키는 두 이름이다.)

    # ------------------------------------------------------------------
    # 블루프린트(Blueprint) 등록
    # ------------------------------------------------------------------
    # 블루프린트는 "라우트들을 미리 모아둔 묶음"이다.
    # app/views/main.py, app/views/auth.py 에서 @main_bp.route(...) 로 라우트를 정의해도
    # 그 시점에는 아직 앱에 붙은 게 아니다. 아래 register_blueprint() 를 호출하는
    # 순간에 비로소 앱의 URL 라우팅 테이블에 실제로 등록된다.
    #
    # [url_prefix 동작 방식]
    # register_blueprint(bp, url_prefix="/auth") 를 주면,
    # 그 블루프린트 안의 모든 route 경로 앞에 "/auth" 가 자동으로 붙는다.
    #
    #   views/auth.py 에서:  @auth_bp.route("/login")   ->  실제 URL: /auth/login
    #   views/auth.py 에서:  @auth_bp.route("/logout")  ->  실제 URL: /auth/logout
    #
    # 즉 블루프린트 파일 안에서는 "/auth" 를 직접 쓰지 않는다.
    # prefix 를 어디에 붙일지는 등록하는 쪽(app/__init__.py)이 결정하기 때문에,
    # 나중에 "/auth" 를 "/account" 로 바꾸고 싶으면 아래 한 줄만 고치면 된다.
    #
    # [엔드포인트 이름 규칙]
    # 블루프린트에 속한 뷰 함수의 엔드포인트 이름은 "블루프린트이름.함수이름" 이 된다.
    # 이 '블루프린트이름'은 Blueprint("main", ...) 처럼 생성자 첫 번째 인자로 준 이름이다.
    #   main_bp = Blueprint("main", ...) + def index()  ->  url_for("main.index")
    #   auth_bp = Blueprint("auth", ...) + def login()  ->  url_for("auth.login")
    # 템플릿에서 링크를 만들 때 이 이름을 그대로 쓴다.

    # main 블루프린트: url_prefix 를 주지 않았으므로 경로가 그대로 사용된다.
    #   views/main.py 의 @main_bp.route("/")  ->  실제 URL: /
    app.register_blueprint(main_bp)

    # auth 블루프린트: url_prefix="/auth" 를 주었으므로 모든 경로 앞에 /auth 가 붙는다.
    #   views/auth.py 의 @auth_bp.route("/login")  ->  실제 URL: /auth/login
    app.register_blueprint(auth_bp, url_prefix="/auth")

    # agent 블루프린트: url_prefix="/agent" -> /agent/, /agent/ask, /agent/reset
    # 블루프린트를 하나 더 만들어 등록하는 것만으로 앱에 새 기능 영역이 통째로 붙는다.
    # main/auth 코드는 한 줄도 건드리지 않았다 — 이게 블루프린트를 쓰는 이유다.
    app.register_blueprint(agent_bp, url_prefix="/agent")

    # alarm 블루프린트: 이벤트 접수 -> Lambda 정규화 -> DB/알람.
    # 화면용 라우트(/alarm/)와 외부 시스템용 JSON API(/alarm/api/events)가 한 도메인에 함께 있다.
    app.register_blueprint(alarm_bp, url_prefix="/alarm")

    # admin 블루프린트: 관리자만 접근 가능한 대시보드.
    # 블루프린트마다 접근 정책을 다르게 걸 수 있다는 점을 보여준다.
    #   main  -> 누구나
    #   agent -> 로그인한 사람
    #   admin -> 관리자 계정만
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # dashboard 블루프린트: url_prefix="/dashboard"
    # 지표 화면을 admin 에 밀어넣지 않고 따로 뺐다. 관심사가 다르면 블루프린트를 나누는 게
    # 나중에 권한을 다르게 주거나 떼어내기 쉽다.
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")

    # explore 블루프린트: url_prefix="/explore"
    # 질의어로 이벤트를 뒤져보는 화면. 대시보드(정해진 지표)와 목적이 달라서 따로 뒀다.
    app.register_blueprint(explore_bp, url_prefix="/explore")

    # resources 블루프린트: url_prefix="/resources"
    # 인프라 상태 스냅샷 비교. 이벤트(무슨 일이 있었나)와 달리
    # 리소스는 상태(지금 어떻게 되어 있나)라 성격이 달라 따로 뒀다.
    app.register_blueprint(resources_bp, url_prefix="/resources")

    # report 블루프린트: url_prefix="/report"
    # 이벤트 집계와 리소스 변경을 한 장으로 묶어 보여주고 Markdown 으로 내보낸다.
    app.register_blueprint(report_bp, url_prefix="/report")

    # console 블루프린트: url_prefix="/console"
    # 고객사 계정을 골라 AWS CLI 읽기 전용 명령을 실행한다. 관리자 전용.
    app.register_blueprint(console_bp, url_prefix="/console")

    # work 블루프린트: url_prefix="/work"
    # 작업 전후로 스냅샷을 찍어 "요청한 것만 바뀌었다" 를 증적으로 남긴다.
    # resources 블루프린트가 '무엇이 바뀌었나' 를 보여준다면,
    # 여기는 거기에 '누가, 왜, 어떤 요청으로' 를 붙인다.
    app.register_blueprint(work_bp, url_prefix="/work")

    # runbook 블루프린트: url_prefix="/runbook"
    # 알람 종류(지문)별 대응 절차. alarm 안에 넣지 않고 따로 뺀 이유는
    # 알람은 '지나가는 사건' 이고 런북은 '쌓여서 자산이 되는 문서' 라
    # 수명과 다루는 방식이 다르기 때문이다.
    app.register_blueprint(runbook_bp, url_prefix="/runbook")

    # handover 블루프린트: url_prefix="/handover"
    # 당직 인계. report 블루프린트와 목적이 다르다.
    #   report   -> 고객사에 내는 것, 월 단위, 기간 대비 변화가 핵심
    #   handover -> 다음 당직자에게 넘기는 것, 시간 단위, 미해결 항목이 핵심
    app.register_blueprint(handover_bp, url_prefix="/handover")

    # incident 블루프린트: url_prefix="/incident"
    # 장애 사후 보고서. 이 앱에서 events / resource_snapshots / work_orders 가
    # 처음으로 한 화면에서 만나는 곳이다. 셋 다 시각이 찍혀 있는데 지금까지
    # 서로를 몰랐다 - 장애 조사는 그 셋을 한 시간축에 세워야 시작된다.
    app.register_blueprint(incident_bp, url_prefix="/incident")

    # noise 블루프린트: url_prefix="/noise"
    # 시끄러운 알람 순위와 지문별 억제 규칙. 실제 억제 판정은 여기가 아니라
    # api/normalize_handler.py 에서 한다 - 알람을 보내는 쪽이 Lambda 이므로.
    app.register_blueprint(noise_bp, url_prefix="/noise")

    # customer 블루프린트: url_prefix="/customer"
    # 이 앱에 처음 생기는 '고객사 축' 화면이다. 나머지 블루프린트는 전부
    # 기능 축(알람은 알람끼리, 작업은 작업끼리)이라, 고객사 하나의 상태를
    # 보려면 화면을 여섯 개 돌아야 했다.
    app.register_blueprint(customer_bp, url_prefix="/customer")

    # compliance 블루프린트: url_prefix="/compliance"
    # 리소스 스냅샷을 한 번 더 쓴다. 원래는 '무엇이 바뀌었나' 를 보려고
    # 찍던 것인데, 같은 자료로 '지금 상태가 기준에 맞나' 도 볼 수 있다.
    app.register_blueprint(compliance_bp, url_prefix="/compliance")

    # ------------------------------------------------------------------
    # CLI 명령 등록
    # ------------------------------------------------------------------
    # 블루프린트가 '웹 URL' 을 늘리는 것이라면, 이건 'flask 명령' 을 늘리는 것이다.
    # 등록해두면 `flask --app run init-db` 처럼 쓸 수 있다.
    register_cli(app)

    # ------------------------------------------------------------------
    # 조회 전용 계정 차단
    # ------------------------------------------------------------------
    # 블루프린트마다 POST 라우트가 흩어져 있어서, 각자 막게 하면 새 화면을
    # 만들 때마다 빠뜨리게 된다. 여기서 한 번에 막는다.
    #
    # 로그인한 사용자에게만 적용한다. 로그인하지 않은 요청(알람 수집 API 등)은
    # 각 블루프린트가 이미 자기 방식으로 막고 있다.
    @app.before_request
    def block_read_only():
        from urllib.parse import urlparse

        from flask import flash, redirect, request, session, url_for

        from app import users

        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        if not session.get("username"):
            return None
        # 로그아웃은 막으면 안 된다(로그인은 GET 폼 + POST 라 아래 검사에 걸린다).
        if (request.endpoint or "").startswith("auth."):
            return None
        if users.can(session.get("role"), "operator"):
            return None

        flash("조회 전용 계정이라 바꾸는 작업은 할 수 없습니다.", "error")
        # 원래 있던 화면으로 돌려보낸다.
        # referrer 는 브라우저가 보내는 값이라 그대로 믿고 redirect 하면
        # 남의 사이트로 튕겨 보낼 수 있다(open redirect). 같은 사이트일 때만 쓴다.
        back = request.referrer or ""
        if urlparse(back).netloc != urlparse(request.host_url).netloc:
            back = url_for("main.index")
        return redirect(back)

    # ------------------------------------------------------------------
    # 템플릿 공통 값
    # ------------------------------------------------------------------
    # context_processor 로 등록한 함수가 돌려주는 딕셔너리는 모든 템플릿에서
    # 변수처럼 쓸 수 있다. 화면마다 render_template 에 같은 값을 넘기지 않아도 된다.
    @app.context_processor
    def inject_roles():
        from app import users

        return {"role_names": users.ROLES}

    return app

# 주의: 여기서 app = create_app() 을 실행하지 않는다.
# 그렇게 하면 `from app import create_app` 처럼 import 만 해도 앱이 만들어져버려서,
# 테스트에서 다른 설정으로 앱을 새로 만들 수 없게 된다.
# 실제 앱 객체를 만드는 일은 진입점인 run.py 가 담당한다.
