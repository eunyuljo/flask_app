# app/users.py
# 사용자 계정. 비밀번호는 해시로만 저장한다.
#
# 해시는 werkzeug.security 를 쓴다. Flask 가 이미 의존하는 패키지라
# 새로 설치할 것이 없고, 솔트와 반복 횟수를 알아서 처리한다.
#
# ── 부트스트랩에 대해 ────────────────────────────────────────────────
# users 테이블이 없거나 비어 있으면 예전 하드코딩 계정으로 로그인된다.
# 이 앱은 DB 없이도 뜨는 것을 전제로 하고, 처음 켠 사람이 로그인조차
# 못 하면 아무것도 못 해보기 때문이다. 대신 그 상태를 화면에 크게 알린다.
# 사용자를 하나라도 만들면 자동으로 그쪽만 쓴다.

from flask import current_app

from app import db

# 부트스트랩 계정. 사용자가 하나도 없을 때만 통한다.
BOOTSTRAP_USERNAME = "admin"
BOOTSTRAP_PASSWORD = "1234"

ROLES = {
    "admin": "관리자",
    "operator": "운영자",
    "viewer": "조회 전용",
}

# 역할이 가진 권한. 상위 역할은 하위를 포함한다.
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


class UserError(Exception):
    """사용자 관리에 실패했을 때."""


# 접속 문자열은 app/db.py 가 만든다. 다른 모듈이 이 이름으로
# 가져다 쓰고 있어서 별칭으로 남긴다.
psycopg_uri = db.uri


_rows = db.rows


def _connect():
    return db.connect(UserError)


def _table_ready(cur):
    cur.execute("SELECT to_regclass('public.users')")
    return cur.fetchone()[0] is not None


def bootstrap_mode():
    """지금 하드코딩 계정으로 도는 중인가.

    화면에 경고를 띄우기 위해 쓴다. DB 가 아예 없어도 True 다 -
    그 상태에서도 부트스트랩 계정으로는 들어올 수 있기 때문이다.
    """
    try:
        with _connect() as conn, conn.cursor() as cur:
            if not _table_ready(cur):
                return True
            cur.execute("SELECT count(*) FROM users WHERE enabled")
            return cur.fetchone()[0] == 0
    except UserError:
        return True


def authenticate(username, password):
    """로그인. 성공하면 사용자 dict, 실패하면 None."""
    from werkzeug.security import check_password_hash

    username = (username or "").strip()
    if not username or not password:
        return None

    try:
        with _connect() as conn, conn.cursor() as cur:
            if not _table_ready(cur):
                return _bootstrap_login(username, password)

            cur.execute(
                "SELECT * FROM users WHERE username = %s AND enabled", (username,)
            )
            rows = _rows(cur)
            if not rows:
                # 사용자가 하나도 없으면 부트스트랩을 허용한다.
                cur.execute("SELECT count(*) FROM users WHERE enabled")
                if cur.fetchone()[0] == 0:
                    return _bootstrap_login(username, password)
                return None

            user = rows[0]
            if not check_password_hash(user["password_hash"], password):
                return None

            cur.execute(
                "UPDATE users SET last_login_at = now() WHERE id = %s", (user["id"],)
            )
    except UserError:
        # DB 가 없어도 들어올 수는 있어야 한다.
        return _bootstrap_login(username, password)

    user.pop("password_hash", None)
    return user


def _bootstrap_login(username, password):
    """하드코딩 계정. 사용자가 하나도 없을 때만 통한다."""
    import hmac

    # compare_digest 는 str 을 받으면 ASCII 만 허용한다(한글 비밀번호에서 TypeError).
    # bytes 로 바꿔서 넘기면 어떤 문자든 그대로 비교된다.
    def same(a, b):
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

    if not (same(username, BOOTSTRAP_USERNAME) and same(password, BOOTSTRAP_PASSWORD)):
        return None
    return {"id": 0, "username": BOOTSTRAP_USERNAME, "role": "admin",
            "enabled": True, "bootstrap": True}


def can(role, required):
    """이 역할이 required 이상인가."""
    return ROLE_RANK.get(role or "", 0) >= ROLE_RANK.get(required, 99)


# ----------------------------------------------------------------------
# 관리
# ----------------------------------------------------------------------

def create(username, password, role="operator"):
    """사용자를 만든다."""
    from werkzeug.security import generate_password_hash

    username = (username or "").strip()
    if not username:
        raise UserError("사용자 이름을 입력하세요.")
    if not password or len(password) < 8:
        raise UserError("비밀번호는 8자 이상이어야 합니다.")
    if role not in ROLES:
        raise UserError(f"알 수 없는 역할입니다: {role}")

    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur):
            raise UserError(
                "users 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )
        cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
        if cur.fetchone():
            raise UserError(f"이미 있는 사용자입니다: {username}")
        cur.execute(
            "INSERT INTO users (username, password_hash, role) "
            "VALUES (%s, %s, %s) RETURNING id",
            (username, generate_password_hash(password), role),
        )
        return cur.fetchone()[0]


def set_password(username, password):
    """비밀번호를 바꾼다."""
    from werkzeug.security import generate_password_hash

    if not password or len(password) < 8:
        raise UserError("비밀번호는 8자 이상이어야 합니다.")

    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur):
            raise UserError("users 테이블이 없습니다.")
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE username = %s RETURNING id",
            (generate_password_hash(password), username.strip()),
        )
        if cur.fetchone() is None:
            raise UserError(f"사용자를 찾지 못했습니다: {username}")


def set_enabled(username, enabled):
    """계정을 켜거나 끈다. 지우지 않는 이유: 감사 로그에 남은 이름이
    누구였는지 나중에 확인할 수 있어야 한다."""
    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur):
            raise UserError("users 테이블이 없습니다.")
        if not enabled:
            # 마지막 관리자를 끄면 아무도 관리자 화면에 못 들어간다.
            # 끄려는 대상이 관리자일 때만 따진다. 운영자를 끄는데 이 검사가
            # 걸리면(관리자가 한 명뿐인 상황) 엉뚱한 이유로 막힌다.
            cur.execute(
                "SELECT count(*) FILTER (WHERE username = %s AND role = 'admin'), "
                "       count(*) FILTER (WHERE username <> %s AND role = 'admin') "
                "FROM users WHERE enabled",
                (username.strip(), username.strip()),
            )
            target_is_admin, other_admins = cur.fetchone()
            if target_is_admin and other_admins == 0:
                raise UserError("마지막 관리자 계정은 끌 수 없습니다.")
        cur.execute(
            "UPDATE users SET enabled = %s WHERE username = %s RETURNING id",
            (bool(enabled), username.strip()),
        )
        if cur.fetchone() is None:
            raise UserError(f"사용자를 찾지 못했습니다: {username}")


def listing():
    """사용자 목록. 해시는 돌려주지 않는다."""
    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur):
            raise UserError("users 테이블이 없습니다.")
        cur.execute(
            "SELECT id, username, role, enabled, created_at, last_login_at "
            "FROM users ORDER BY role, username"
        )
        return _rows(cur)
