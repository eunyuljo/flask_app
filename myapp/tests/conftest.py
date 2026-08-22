# tests/conftest.py
# 모든 테스트가 공유하는 준비물.
# 프로젝트 루트를 import 경로에 넣어서 `from app import ...` 가 되게 한다.

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def app():
    """테스트용 앱. testing 설정으로 만든다."""
    from app import create_app

    application = create_app("testing")
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture
def client(app):
    """로그인하지 않은 클라이언트."""
    return app.test_client()


@pytest.fixture
def logged_in(app):
    """로그인한 클라이언트."""
    c = app.test_client()
    c.post("/auth/login", data={"username": "admin", "password": "1234"})
    return c


@pytest.fixture
def db_app():
    """진짜 PostgreSQL 을 보는 앱.

    testing 설정은 SQLALCHEMY_DATABASE_URI 가 "sqlite://" 라서 DB 질의가
    전부 실패한다(그게 의도다 - 개발용 데이터를 건드리지 않는다).
    DB 가 필요한 테스트는 development 설정을 쓴다.
    """
    from app import create_app

    return create_app("development")


@pytest.fixture
def db_uri(db_app):
    """DB 가 실제로 붙는지 확인하고, 안 되면 그 테스트를 건너뛴다.

    DB 가 없다고 실패로 처리하면, DB 없이 개발하는 사람이 매번 빨간 화면을
    보게 된다. 이 프로젝트는 DB 없이도 앱이 뜨는 것을 전제로 한다.
    """
    psycopg = pytest.importorskip("psycopg")
    uri = db_app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )
    try:
        with psycopg.connect(uri, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.events')")
            if cur.fetchone()[0] is None:
                pytest.skip("events 테이블이 없습니다 (init-db 필요)")
    except Exception as e:
        pytest.skip(f"PostgreSQL 에 붙지 못했습니다: {e}")
    return uri


@pytest.fixture
def db_client(db_app, db_uri):
    """진짜 DB 를 보는 로그인된 클라이언트.

    로그인 폼을 거치지 않고 세션을 직접 채운다. admin/1234 로 로그인하면
    부트스트랩 계정에 기대게 되는데, 그건 users 테이블이 비어 있을 때만
    통한다. 개발 DB 에 계정을 하나라도 만드는 순간 이 테스트들이 전부
    깨졌다 - 앱은 정상인데 테스트만 깨지는, 가장 나쁜 종류다.
    """
    c = db_app.test_client()
    with c.session_transaction() as s:
        s["username"] = "테스트관리자"
        s["role"] = "admin"
    return c
