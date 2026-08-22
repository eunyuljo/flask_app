# app/event_store.py
# 이벤트를 읽는 곳. 원본은 Lambda 가 DB(events 테이블)에 넣는다.
#
# 예전에는 이 모듈이 메모리 deque(maxlen=100) 였다. 그게 만든 문제:
#   - 서버를 재시작하면 목록이 비었다
#   - 101건째부터 앞이 밀려났다
#   - 대시보드는 DB 를 보고 이 목록은 메모리를 봐서 숫자가 서로 달랐다
#   - AI 진단 버튼이 이 목록에만 붙어서, DB 에 쌓인 알람은 진단할 수 없었다
#
# 같은 데이터가 두 곳에 있으면 화면마다 어느 쪽을 볼지 매번 정해야 하고,
# 그 결정이 화면마다 달라진다. 저장소를 하나로 모은다.
#
# 모듈 이름과 함수 이름은 그대로 둔다. 부르는 쪽이 바뀔 이유가 없다 -
# '이벤트를 읽는 곳' 이라는 역할은 그대로이고 구현만 바뀌었다.

from flask import current_app


class EventStoreError(Exception):
    """이벤트를 읽지 못했을 때."""


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
        raise EventStoreError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise EventStoreError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _ensure(cur):
    cur.execute("SELECT to_regclass('public.events')")
    if cur.fetchone()[0] is None:
        raise EventStoreError(
            "events 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


# 화면이 기대하는 모양은 예전과 같다: {"record": {...}, "delivery": {...}}
#
# delivery(적재/알람 결과)는 Lambda 응답에만 있고 DB 에는 없다. 목록에서는
# 그걸 보여주지 않는다 - 목록에 있다는 것 자체가 '적재됐다' 는 뜻이고,
# 알람 발송 여부는 알람 노이즈 화면(alarm_state)에서 본다.
def _wrap(row):
    return {"record": row, "delivery": {}}


def recent(limit=20, account_id=None):
    """최근 이벤트를 새 것부터. account_id 를 주면 그 계정 것만."""
    where, params = [], []
    if account_id:
        where.append("account_id = %s")
        params.append(account_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            f"""
            SELECT event_id, event_type, source, severity, message,
                   occurred_at, received_at, fingerprint, account_id, meta
              FROM events {where_sql}
             ORDER BY occurred_at DESC
             LIMIT %s
            """,
            params,
        )
        return [_wrap(r) for r in _rows(cur)]


def get(event_id):
    """event_id 로 이벤트 하나를 찾는다. 없으면 None.

    이제 목록에서 밀려나도 찾을 수 있다. 예전에는 100건을 넘기면
    방금 만든 이벤트도 진단할 수 없었다.
    """
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            """
            SELECT event_id, event_type, source, severity, message,
                   occurred_at, received_at, fingerprint, account_id, meta
              FROM events WHERE event_id = %s
            """,
            (event_id,),
        )
        rows = _rows(cur)
    return _wrap(rows[0]) if rows else None


def stats():
    """관리자 화면에 쓸 간단한 집계."""
    counts = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute("SELECT severity, count(*) FROM events GROUP BY severity")
        counts.update(dict(cur.fetchall()))

        # 알람이 실제로 나간 건수. 예전에는 메모리에 있던 delivery 를 셌는데,
        # 그건 이 프로세스가 처리한 것만이었다. 지금은 발송 기록을 본다.
        alarmed = 0
        cur.execute("SELECT to_regclass('public.alarm_state')")
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT COALESCE(sum(sent_count), 0) FROM alarm_state")
            alarmed = int(cur.fetchone()[0])

        cur.execute("SELECT count(*) FROM events")
        total = cur.fetchone()[0]

    return {
        "total": total,
        "by_severity": {k: counts.get(k, 0) for k in
                        ("critical", "error", "warning", "info")},
        "alarmed": alarmed,
    }


def clear():
    """이벤트를 전부 지운다. 관리자 화면에서만 부른다.

    감사 로그(audit_log)는 건드리지 않는다. 이벤트 정리와 수명이 다르다.
    """
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute("DELETE FROM events")
        return cur.rowcount
