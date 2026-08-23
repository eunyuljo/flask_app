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


# ----------------------------------------------------------------------
# 실행 기록
# ----------------------------------------------------------------------
# 위쪽(save/find/...)이 '절차' 를 다루고, 여기서부터는 '그 절차를 따른 일'
# 을 다룬다. 표를 나눈 이유는 db/schema.sql 의 runbook_runs 주석에 있다.

OUTCOMES = {
    "resolved": "해결됨",
    "partial": "부분 해결 · 추가 조치 필요",
    "failed": "절차대로 했으나 해결 안 됨",
    "stale": "절차가 현행과 맞지 않음",
}

# 런북을 고치라는 신호. 이 결과는 이유를 적지 않으면 쓸모가 없다.
NEEDS_NOTE = ("failed", "stale")


def _ensure_runs(cur):
    cur.execute("SELECT to_regclass('public.runbook_runs')")
    if cur.fetchone()[0] is None:
        raise RunbookError(
            "runbook_runs 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


def record_run(runbook_id, outcome, ran_by, note="", minutes=0,
               account_id="", event_id="", customer=""):
    """이 런북을 따랐다는 기록을 남긴다.

    지문과 제목은 런북에서 읽어 복사한다. 호출하는 쪽이 넘기게 하면
    화면마다 다른 값이 들어오고, 무엇보다 런북이 지워진 뒤에 남은
    기록이 무엇에 대한 것인지 알 수 없게 된다.
    """
    if outcome not in OUTCOMES:
        raise RunbookError(f"알 수 없는 결과입니다: {outcome}")
    note = (note or "").strip()
    if outcome in NEEDS_NOTE and not note:
        raise RunbookError(
            f"'{OUTCOMES[outcome]}' 은 무엇이 안 맞았는지 적어야 저장됩니다."
        )
    try:
        minutes = max(0, int(minutes or 0))
    except (TypeError, ValueError):
        raise RunbookError("걸린 시간은 숫자로 적으세요.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        _ensure_runs(cur)
        cur.execute(
            "SELECT fingerprint, title, customer FROM runbooks WHERE id = %s",
            (runbook_id,),
        )
        book = cur.fetchone()
        if book is None:
            raise RunbookError("런북을 찾지 못했습니다.")
        fingerprint, title, book_customer = book

        cur.execute(
            """
            INSERT INTO runbook_runs
                (runbook_id, fingerprint, title, customer, account_id,
                 event_id, outcome, note, minutes, ran_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (runbook_id, fingerprint, title,
             # 어느 고객사 일이었는지. 알람에서 왔으면 그 계정의 고객사가,
             # 없으면 런북 자체의 적용 범위가 들어간다.
             (customer or book_customer or "").strip(),
             account_id.strip(), event_id.strip(),
             outcome, note, minutes, ran_by or ""),
        )
        return cur.fetchone()[0]


def runs(runbook_id=None, fingerprint="", limit=100):
    """실행 기록 목록. 최근 순.

    runbook_id 로 좁힐 때도 지문을 함께 본다. 런북을 지웠다 다시 쓰면
    id 가 달라지는데, 사람에게는 '같은 알람의 대응 이력' 이 이어져
    보여야 한다.
    """
    where, params = [], []
    if runbook_id is not None:
        where.append("(runbook_id = %s OR fingerprint = %s)")
        params += [runbook_id, fingerprint or ""]
    elif fingerprint:
        where.append("fingerprint = %s")
        params.append(fingerprint)

    with _connect() as conn, conn.cursor() as cur:
        _ensure_runs(cur)
        cur.execute(
            "SELECT * FROM runbook_runs"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY ran_at DESC LIMIT %s",
            params + [limit],
        )
        return _rows(cur)


def usage(runbook_ids):
    """런북별 사용 현황. {id: {...}} 를 돌려준다.

    목록 화면에서 런북마다 질의하지 않기 위해 한 번에 모은다
    (find_many 와 같은 이유).

    '한 번도 안 쓰인 런북' 은 여기에 키가 없는 것으로 나타난다.
    0 건짜리 행을 만들어 돌려주지 않는 이유: 없는 것과 0 인 것을
    호출하는 쪽에서 구분할 수 있어야 한다.
    """
    if not runbook_ids:
        return {}
    with _connect() as conn, conn.cursor() as cur:
        _ensure_runs(cur)
        cur.execute(
            """
            SELECT runbook_id,
                   count(*)                                        AS total,
                   count(*) FILTER (WHERE outcome = 'resolved')    AS resolved,
                   count(*) FILTER (WHERE outcome IN ('failed', 'stale'))
                                                                   AS trouble,
                   max(ran_at)                                     AS last_at
              FROM runbook_runs
             WHERE runbook_id = ANY(%s)
             GROUP BY runbook_id
            """,
            (list(set(runbook_ids)),),
        )
        return {r["runbook_id"]: r for r in _rows(cur)}


def summary(days=30):
    """최근 실행 기록 요약. 화면 위쪽 숫자에 쓴다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_runs(cur)
        cur.execute(
            """
            SELECT count(*)                                     AS total,
                   count(*) FILTER (WHERE outcome = 'resolved') AS resolved,
                   count(*) FILTER (WHERE outcome IN ('failed', 'stale'))
                                                                AS trouble,
                   count(DISTINCT runbook_id)                   AS books
              FROM runbook_runs
             WHERE ran_at >= now() - make_interval(days => %s)
            """,
            (days,),
        )
        row = _rows(cur)[0]

        # 손봐야 할 런북. 최근에 failed/stale 이 난 것들을 그대로 보여준다.
        # 비율로 순위를 매기지 않는다 - 한 번 쓰고 한 번 실패한 절차가
        # 100% 로 맨 위에 오면 정작 자주 쓰는 절차의 문제가 묻힌다.
        cur.execute(
            """
            SELECT fingerprint, title, runbook_id,
                   count(*)      AS trouble,
                   max(ran_at)   AS last_at,
                   (array_agg(note ORDER BY ran_at DESC))[1] AS last_note
              FROM runbook_runs
             WHERE outcome IN ('failed', 'stale')
               AND ran_at >= now() - make_interval(days => %s)
             GROUP BY fingerprint, title, runbook_id
             ORDER BY trouble DESC, last_at DESC
             LIMIT 10
            """,
            (days,),
        )
        row["needs_fix"] = _rows(cur)
        row["days"] = days
        return row
