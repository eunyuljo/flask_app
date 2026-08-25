# app/db.py
# DB 접속 한 곳.
#
# ── 왜 모았나 ──────────────────────────────────────────────────────
# 아래 세 함수가 서비스 모듈 21개에 그대로 복사되어 있었다.
#
#   psycopg_uri()  설정에서 접속 문자열 꺼내기
#   _rows(cur)     커서 결과를 dict 목록으로
#   _connect()     psycopg.connect() + 에러를 도메인 예외로 바꾸기
#
# 복사본이 21개라는 것은 고칠 일이 생기면 21곳을 고쳐야 한다는 뜻이다.
# 실제로 그런 일이 있었다 - 설정 키 이름을 SQLALCHEMY_DATABASE_URI 에서
# DATABASE_URI 로 바꾸면서 33개 파일을 건드려야 했다. 커넥션 타임아웃을
# 넣거나, 접속 실패 메시지를 고치거나, 풀을 붙이는 일도 전부 그렇다.
#
# ── 도메인 에러는 그대로 둔다 ──────────────────────────────────────
# connect() 가 예외 클래스를 인자로 받는다. 부르는 쪽은 그대로
# NoiseError, WorkError 를 던진다.
#
# 이게 중요한 이유: 화면은 자기가 부른 모듈의 에러만 잡는다. 여기서
# 공통 DbError 하나로 바꿔버리면, 노이즈 화면이 잡던 NoiseError 가 안
# 잡히면서 500 이 뜬다. 21개 화면이 전부 그렇게 된다.
#
# ── 여기서 앱 모듈을 import 하지 않는다 ────────────────────────────
# 21개 모듈이 이 파일을 부르므로, 이 파일이 그 중 하나라도 부르면
# 순환 import 가 된다. flask 와 표준 라이브러리만 쓴다.

from flask import current_app


def uri():
    """접속 문자열.

    .replace 는 남겨 둔다. 설정 기본값에서는 이제 +psycopg 가 붙지 않지만,
    운영에서 DATABASE_URL 을 예전 표기(postgresql+psycopg://)로 주는 곳이
    있을 수 있다. 정규화는 한 곳에서만 하면 되므로 여기가 그 자리다.
    """
    return current_app.config["DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def rows(cur):
    """커서에 남은 결과를 dict 목록으로.

    psycopg 는 기본이 튜플이라 r[3] 같은 코드가 된다. 열 순서를 바꾸는
    순간 조용히 틀린 값을 읽는다.
    """
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def connect(error):
    """psycopg 커넥션을 연다. 실패는 도메인 예외로 바꾼다.

    error: 이 모듈이 쓰는 예외 클래스(NoiseError, WorkError ...).

    psycopg 예외만 감싸는 이유: 설정 키가 없어서 나는 KeyError 처럼
    우리 잘못인 것까지 "DB 에 접속하지 못했습니다" 로 덮으면, 진짜 원인이
    메시지 뒤에 숨는다. 그런 것은 그대로 올려보내 스택을 남긴다.
    """
    try:
        import psycopg
    except ImportError as e:
        raise error("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(uri())
    except Exception as e:                               # noqa: BLE001
        if type(e).__module__.split(".")[0] == "psycopg":
            raise error(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def table_exists(cur, name):
    """이 표가 있는가.

    "없는 표" 와 "빈 표" 는 다르다. 앞은 init-db 를 안 돌린 것이고 뒤는
    아직 아무도 안 쓴 것이다. 화면이 그 둘을 다르게 말해야 해서, 모듈들이
    질의 전에 이걸 먼저 본다.
    """
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None
