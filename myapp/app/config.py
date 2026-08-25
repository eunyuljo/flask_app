# app/config.py
# 환경(개발/테스트/운영)별 설정값을 한곳에 모아두는 파일. DB 접속 정보와 secret_key 처럼
# 코드가 아니라 '환경에 따라 달라지는 값'을 여기서 관리하고, create_app() 이 골라서 읽어간다.

from datetime import timedelta
import os
from urllib.parse import quote_plus

# python-dotenv: 프로젝트 루트의 .env 파일을 읽어 os.environ 에 채워준다.
# 설치되어 있지 않아도 앱은 그대로 동작해야 하므로 try/except 로 감싼다.
try:
    from dotenv import load_dotenv

    # .env 는 app/ 안이 아니라 프로젝트 루트(myapp/)에 둔다.
    # config.py 는 app/ 안에 있으므로 한 단계 위로 올라가야 한다.
    #   app/config.py  ->  dirname = app/  ->  한 단계 위 = myapp/
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    # dotenv 가 없으면 그냥 넘어간다. 이때는 실제 환경변수만 사용된다.
    pass


def build_db_uri(driver, user, password, host, port, name):
    """개별 접속 정보를 DB URI 한 줄로 조립한다.

    최종 형태:  postgresql://아이디:비밀번호@호스트:포트/DB이름

    예전에는 postgresql+psycopg:// 였다. 그건 SQLAlchemy 가 드라이버를
    고르는 표기인데 이 앱은 psycopg 를 직접 쓴다. 그래서 모듈마다
    .replace("postgresql+psycopg://", "postgresql://") 를 하고 있었다.
    읽는 사람이 ORM 을 쓰는 줄 알게 되는 표기를 남길 이유가 없다.

    quote_plus() 로 감싸는 이유:
    비밀번호에 @ / : # 같은 문자가 들어 있으면 URI 구분자와 헷갈려서 접속이 깨진다.
    예) 비밀번호가 "p@ss" 이면 그냥 넣었을 때 호스트를 "ss" 로 잘못 읽는다.
    quote_plus 는 이런 문자를 %40 같은 안전한 형태로 바꿔준다.
    """
    return (
        f"{driver}://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}"
    )


class Config:
    """모든 환경이 공통으로 쓰는 기본 설정. 아래 클래스들이 이걸 상속해서 일부만 덮어쓴다."""

    # ------------------------------------------------------------------
    # 보안 키
    # ------------------------------------------------------------------
    # session 과 flash 가 쿠키에 서명할 때 쓰는 키.
    # os.environ.get("키", 기본값) -> 환경변수가 있으면 그 값을, 없으면 기본값을 쓴다.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

    # 로그인 세션이 살아 있는 시간. 이 시간이 지나면 쿠키가 만료되어
    # 다시 로그인해야 한다. auth.login 에서 session.permanent = True 를
    # 켜 두어야 이 값이 적용된다.
    PERMANENT_SESSION_LIFETIME = timedelta(
        hours=float(os.environ.get("SESSION_HOURS", "12"))
    )

    # 알람 수집 API(/alarm/api/events) 를 부를 때 필요한 키.
    # 쉼표로 여러 개를 넣을 수 있다(고객사별로 다른 키를 주기 위해).
    #
    # 비워두면 인증 없이 열린다. 이 앱은 "설정하지 않은 것은 오류가 아니다"
    # 를 원칙으로 삼기 때문인데, 이 항목만은 열어두면 아무나 알람을 밀어넣어
    # 당직자를 깨울 수 있다. 그래서 관리자 화면에서 크게 경고한다.
    INGEST_API_KEYS = {
        k.strip()
        for k in os.environ.get("INGEST_API_KEYS", "").split(",")
        if k.strip()
    }

    # ------------------------------------------------------------------
    # DB 접속 정보
    # ------------------------------------------------------------------
    # 접속에 필요한 값들을 항목별로 나눠서 읽는다.
    # 이렇게 쪼개두면 "호스트만 바꾸기" 같은 게 쉽고, 로그에 비밀번호를 빼고 찍기도 편하다.
    DB_DRIVER = os.environ.get("DB_DRIVER", "postgresql")
    DB_USER = os.environ.get("DB_USER", "flask_user")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "flask_password")
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_PORT = os.environ.get("DB_PORT", "5432")
    DB_NAME = os.environ.get("DB_NAME", "flask_app")

    # 최종 접속 문자열.
    # DATABASE_URL 환경변수가 통째로 주어지면 그걸 그대로 쓰고(운영 환경에서 흔한 방식),
    # 없으면 위의 항목들을 조합해서 만든다.
    #
    # 이름이 DATABASE_URI 였는데 이 앱에는 SQLAlchemy 가 없다.
    # psycopg 를 직접 쓴다. 설정 이름이 거짓말을 하면 읽는 사람이 ORM 을
    # 찾다가 없어서 헤맨다.
    DATABASE_URI = os.environ.get("DATABASE_URL") or build_db_uri(
        DB_DRIVER, DB_USER, DB_PASSWORD, DB_HOST, DB_PORT, DB_NAME
    )

    # ------------------------------------------------------------------
    # AI 에이전트 (Anthropic Claude)
    # ------------------------------------------------------------------
    # API 키는 절대 코드에 적지 않는다. 기본값도 두지 않아서, 값이 없으면 빈 문자열이 되고
    # 화면에 "키가 없습니다" 안내가 뜬다(앱이 죽지는 않는다).
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # 사용할 모델 ID.
    AGENT_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5")

    # 한 번의 응답에서 만들어낼 수 있는 최대 토큰 수.
    AGENT_MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "16000"))

    # 답변에 얼마나 공을 들일지: low / medium / high / xhigh / max.
    # 웹 화면에서 사용자가 기다리므로 기본값(high)보다 낮은 medium 을 기본으로 둔다.
    AGENT_EFFORT = os.environ.get("AGENT_EFFORT", "medium")

    # 어느 경로로 Claude 를 호출할지 고르는 스위치: "claude_api" 또는 "bedrock".
    # 코드를 고치지 않고 환경변수만 바꿔서 전환할 수 있다.
    #   claude_api -> Anthropic 에 직접 호출. ANTHROPIC_API_KEY 사용.
    #   bedrock    -> AWS Bedrock 경유. AWS 자격증명(SigV4 서명) 사용.
    AGENT_PROVIDER = os.environ.get("AGENT_PROVIDER", "claude_api")

    # 안전 분류기가 요청을 거절했을 때 대신 시도할 모델.
    # claude_api 에서는 서버가, bedrock 에서는 SDK 미들웨어가 이 모델로 넘긴다.
    AGENT_FALLBACK_MODEL = os.environ.get("AGENT_FALLBACK_MODEL", "claude-opus-4-8")

    # --- Bedrock 을 쓸 때만 필요한 값들 ---
    # 리전은 필수다. Bedrock 은 리전별로 모델 액세스를 따로 켜야 한다.
    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

    # 액세스 키를 비워두면 botocore 의 기본 자격증명 체인이 대신 찾아준다.
    # (~/.aws/credentials, EC2/ECS/Lambda 의 IAM 역할 등)
    # EC2 나 ECS 위에서 돌린다면 키를 넣지 말고 IAM 역할을 쓰는 편이 안전하다.
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    AWS_SESSION_TOKEN = os.environ.get("AWS_SESSION_TOKEN", "")
    AWS_PROFILE = os.environ.get("AWS_PROFILE", "")

    # ------------------------------------------------------------------
    # 알람 / Lambda
    # ------------------------------------------------------------------
    # 이벤트 정규화를 어디서 실행할지 고르는 스위치.
    #   local -> 같은 프로세스에서 핸들러 함수를 직접 호출 (AWS 불필요, 개발/학습용)
    #   aws   -> 실제 Lambda 함수를 호출
    LAMBDA_MODE = os.environ.get("LAMBDA_MODE", "local")

    # aws 모드에서 호출할 Lambda 함수 이름(또는 ARN).
    LAMBDA_FUNCTION_NAME = os.environ.get("LAMBDA_FUNCTION_NAME", "flask-app-normalize-event")

    # RequestResponse(동기) = 결과를 기다렸다 화면에 보여준다.
    # Event(비동기)        = 던지고 바로 응답한다. 결과는 CloudWatch 에서 확인.
    LAMBDA_INVOCATION_TYPE = os.environ.get("LAMBDA_INVOCATION_TYPE", "RequestResponse")

    # 관리자 계정 목록. 콤마로 구분해서 넣는다.
    # ------------------------------------------------------------------
    # Slack (Incoming Webhook)
    # ------------------------------------------------------------------
    # 비워두면 Slack 전송을 건너뛴다. 설정하지 않은 것은 오류가 아니다.
    # 용도별 웹훅을 채우면 채널을 나눌 수 있고, 없으면 공통 웹훅으로 간다.
    #   당직 인계  -> SLACK_HANDOVER_WEBHOOK
    #   SLA 경고   -> SLACK_SLA_WEBHOOK
    SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
    SLACK_HANDOVER_WEBHOOK = os.environ.get("SLACK_HANDOVER_WEBHOOK", "")
    SLACK_SLA_WEBHOOK = os.environ.get("SLACK_SLA_WEBHOOK", "")

    # SLA 경고를 같은 알람에 대해 다시 보내기까지 기다리는 시간(분).
    # 20건이 같은 이유로 위반이면 20번 찌르는 게 아니라 한 번만 보낸다.
    SLA_NOTICE_WINDOW_MINUTES = int(
        os.environ.get("SLA_NOTICE_WINDOW_MINUTES", "60")
    )

    # ------------------------------------------------------------------
    # Jira (에스컬레이션 티켓)
    # ------------------------------------------------------------------
    # 이 앱은 티켓 시스템을 만들지 않는다. 에스컬레이션이 일정 단계에
    # 이르면 Jira 로 넘기고, 그 뒤의 상태 관리는 Jira 가 한다.
    # 비워두면 넘기기를 건너뛴다.
    JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "")
    JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
    JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
    JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
    JIRA_ISSUE_TYPE = os.environ.get("JIRA_ISSUE_TYPE", "Task")

    # 몇 단계부터 Jira 로 넘길지. 1차 대응자를 부르는 단계에서 매번
    # 티켓을 만들면 Jira 가 노이즈로 찬다.
    JIRA_ESCALATION_LEVEL = int(os.environ.get("JIRA_ESCALATION_LEVEL", "2"))

    # ------------------------------------------------------------------
    # 에스컬레이션 단계
    # ------------------------------------------------------------------
    # SLA 목표를 넘기고 나서 몇 분이 더 지나면 각 단계를 부를지.
    # "0,30,120" = 목표 초과 즉시 1차, +30분 뒤 2차, +120분 뒤 3차.
    ESCALATION_STEPS = [
        int(x.strip())
        for x in os.environ.get("ESCALATION_STEPS", "0,30,120").split(",")
        if x.strip()
    ]

    # 커넥션 풀 옵션(SQLALCHEMY_ENGINE_OPTIONS)이 여기 있었다. 지웠다 -
    # 읽는 쪽이 없었다. 이 앱은 요청마다 psycopg.connect() 로 새 커넥션을
    # 맺고 with 블록에서 닫는다. 풀이 없으므로 pool_pre_ping 도 없다.
    #
    # 풀이 필요해지면 그때 진짜로 도는 것을 넣는다. 설정만 있고 아무도 안
    # 읽는 값은 "이미 처리되어 있다" 는 착각만 만든다 - CSRF 가 정확히
    # 그랬다(WTF_CSRF_ENABLED 는 있는데 Flask-WTF 가 없었다).

    @staticmethod
    def init_app(app):
        """앱이 만들어진 뒤 환경별로 추가 작업이 필요할 때 쓰는 자리.

        기본 설정에서는 할 일이 없어서 비워둔다.
        아래 ProductionConfig 처럼 필요한 환경만 이 메서드를 덮어쓰면 된다.
        """
        pass


class DevelopmentConfig(Config):
    """로컬 개발용. 에러를 자세히 보여주고 SQL 쿼리도 콘솔에 찍는다."""

    DEBUG = True


class TestingConfig(Config):
    """자동 테스트용. 진짜 DB 를 건드리지 않도록 메모리 SQLite 를 쓴다."""

    TESTING = True

    # psycopg 가 쓸 수 없는 값을 일부러 넣는다. DB 를 보는 코드가 전부
    # 실패하게 만들어서, 테스트가 개발용 데이터를 건드리지 않게 한다.
    # (DB 가 필요한 테스트는 conftest 의 db_app 이 development 설정을 쓴다.)
    DATABASE_URI = "sqlite://"

    # 폼 테스트를 편하게 하려고 CSRF 검사를 끈다(Flask-WTF 를 쓸 경우에 해당).
    WTF_CSRF_ENABLED = False


class ProductionConfig(Config):
    """실제 서비스용. 디버그를 끄고, 위험한 기본값이 남아있으면 실행을 막는다."""

    DEBUG = False

    # 운영에서는 쿠키를 HTTPS 로만 전송하고, 자바스크립트가 읽지 못하게 막는다.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    @staticmethod
    def init_app(app):
        """운영 환경에서 설정이 빠졌는지 검사한다.

        개발용 기본 secret_key 를 그대로 운영에 올리면 누구나 세션을 위조할 수 있다.
        그래서 조용히 넘어가지 않고 실행 자체를 실패시킨다.
        (에러는 늦게 터질수록 손해가 크므로, 서버가 뜨는 시점에 미리 막는 편이 낫다.)
        """
        if app.config["SECRET_KEY"] == "dev-secret-key-change-me":
            raise RuntimeError(
                "운영 환경에서는 SECRET_KEY 환경변수를 반드시 설정해야 합니다."
            )
        if not os.environ.get("DATABASE_URL") and not os.environ.get("DB_PASSWORD"):
            raise RuntimeError(
                "운영 환경에서는 DATABASE_URL 또는 DB_PASSWORD 를 설정해야 합니다."
            )


# 이름(문자열) -> 설정 클래스 매핑.
# create_app("development") 처럼 문자열로 골라 쓸 수 있게 해준다.
config = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    # 아무것도 지정하지 않았을 때 쓰는 기본값
    "default": DevelopmentConfig,
}
