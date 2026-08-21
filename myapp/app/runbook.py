# app/runbook.py
# 알람 종류(지문)별 대응 절차를 읽고 쓴다.
#
# 이 모듈이 성립하는 전제는 '같은 알람이 같은 지문을 갖는다' 는 것이다.
# 지문이 메시지 전문을 해시하던 시절에는 같은 알람이 매번 다른 지문이 되어
# 런북을 붙일 대상 자체가 없었다.

from flask import current_app


class RunbookError(Exception):
    """런북을 읽거나 쓰는 데 실패했을 때."""


def psycopg_uri():
    return current_app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _connect():
    try:
        import psycopg
    except ImportError as e:
        raise RunbookError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise RunbookError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _ensure_table(cur):
    cur.execute("SELECT to_regclass('public.runbooks')")
    if cur.fetchone()[0] is None:
        raise RunbookError(
            "runbooks 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


def save(fingerprint, title, body, author, customer="", sample=""):
    """런북을 만들거나 갱신한다.

    (지문, 고객사) 가 같으면 덮어쓴다. 같은 알람에 절차가 둘일 이유가 없다.
    """
    if not fingerprint.strip():
        raise RunbookError("지문이 없습니다.")
    if not title.strip():
        raise RunbookError("제목을 입력하세요.")
    if not body.strip():
        raise RunbookError("대응 절차를 입력하세요.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            INSERT INTO runbooks (fingerprint, customer, title, body, author, sample)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint, customer) DO UPDATE SET
                title      = EXCLUDED.title,
                body       = EXCLUDED.body,
                author     = EXCLUDED.author,
                sample     = EXCLUDED.sample,
                updated_at = now()
            RETURNING id
            """,
            (fingerprint.strip(), customer.strip(), title.strip(),
             body.strip(), author, sample.strip()[:200]),
        )
        return cur.fetchone()[0]


def find(fingerprint, customer=""):
    """이 알람에 맞는 런북 하나를 찾는다. 없으면 None.

    고객사 전용 절차가 있으면 그것을, 없으면 공통 절차를 돌려준다.
    ORDER BY 로 고르는 이유: 두 건을 다 가져와 파이썬에서 고르면
    '전용이 이긴다' 는 규칙이 호출하는 쪽마다 흩어진다.
    """
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            SELECT * FROM runbooks
             WHERE fingerprint = %s AND customer IN (%s, '')
             ORDER BY (customer = '') ASC
             LIMIT 1
            """,
            (fingerprint, customer or ""),
        )
        rows = _rows(cur)
    return rows[0] if rows else None


def find_many(fingerprints):
    """여러 지문에 절차가 있는지 한 번에 본다. {지문: 런북} 을 돌려준다.

    find() 와 고르는 기준이 다르다. find() 는 '이 고객사 계정을 진단할 때 무엇을
    따를까' 라서 전용 절차가 이긴다. 이쪽은 알람 목록에서 '이 알람에 절차가
    있기는 한가' 를 보는 것이라 고객사를 가리지 않는다.

    고객사를 걸러버리면, B물류 전용 절차만 써둔 알람이 목록에서는
    '절차 없음' 으로 보인다. 그러면 이미 쓴 절차를 또 쓰게 된다.
    여러 개면 공통을 먼저 보여주고, 화면이 어느 범위인지 함께 표시한다.

    목록 화면에서 이벤트마다 find() 를 부르면 20건에 질의가 20번 나간다.
    """
    if not fingerprints:
        return {}

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            SELECT DISTINCT ON (fingerprint) *
              FROM runbooks
             WHERE fingerprint = ANY(%s)
             ORDER BY fingerprint, (customer = '') DESC, customer
            """,
            (list(set(fingerprints)),),
        )
        return {r["fingerprint"]: r for r in _rows(cur)}


def recent(limit=50):
    """런북 목록. 최근 수정 순."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            "SELECT * FROM runbooks ORDER BY updated_at DESC LIMIT %s", (limit,)
        )
        return _rows(cur)


def get(runbook_id):
    """런북 하나. 없으면 None."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute("SELECT * FROM runbooks WHERE id = %s", (runbook_id,))
        rows = _rows(cur)
    return rows[0] if rows else None


def delete(runbook_id):
    """런북을 지운다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute("DELETE FROM runbooks WHERE id = %s RETURNING id", (runbook_id,))
        if cur.fetchone() is None:
            raise RunbookError("런북을 찾지 못했습니다.")
