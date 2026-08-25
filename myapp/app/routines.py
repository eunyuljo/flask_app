# app/routines.py
# 고객사에 약속한 주기 업무를 지키고 있는가.
#
# ── app/jobs.py 와 무엇이 다른가 ───────────────────────────────────
# jobs.py 는 '우리 cron 이 살아 있나' 다. 기계가 하는 일이고, 안 돌면
# 그건 우리 도구의 고장이다.
# 여기는 '고객사에 약속한 일을 했나' 다. 사람이 하는 일이고, 안 하면
# 그건 계약 불이행이다. 화면도 보는 사람도 다르다.
#
# ── 시간 계산을 일부러 피했다 ──────────────────────────────────────
# 주기를 달력이 아니라 경과일로 잡는다. 이유는 db/schema.sql 의
# customer_routines 주석에 있다. 요약하면, 달력으로 하는 순간
# 영업시간·공휴일·시간대가 따라 들어오고 그게 SLA 를 어렵게 만든 것이다.

from flask import current_app

# 흔한 주기. 화면의 선택지로 쓴다. 값은 그냥 일수라, 목록에 없는 주기도
# 숫자로 넣으면 그대로 동작한다.
PRESETS = [
    (7, "주간"),
    (30, "월간"),
    (90, "분기"),
    (180, "반기"),
    (365, "연간"),
]

STATE_LABEL = {
    "never": "한 번도 안 함",
    "overdue": "기한 지남",
    "soon": "곧 도래",
    "ok": "정상",
    "paused": "멈춤",
}

# 기한까지 이만큼 남으면 '곧 도래' 로 본다.
# 주기에 비례시킨다 - 연간 점검을 7일 전에 알려주면 늦다.
SOON_RATIO = 0.2

# 손을 대야 하는 상태. 화면 위쪽 숫자와 정렬에 쓴다.
ALERT_STATES = ("overdue", "never")


class RoutineError(Exception):
    """정기 점검을 읽거나 쓰는 데 실패했을 때."""


def psycopg_uri():
    return current_app.config["DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _connect():
    try:
        import psycopg
    except ImportError as e:
        raise RoutineError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise RoutineError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _ensure(cur, table):
    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    if cur.fetchone()[0] is None:
        raise RoutineError(
            f"{table} 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


# ----------------------------------------------------------------------
# 판정
# ----------------------------------------------------------------------
# 순수 함수로 둔다. DB 없이 테스트할 수 있고, 화면과 CLI 가 같은 판정을
# 쓴다. jobs._judge() 와 같은 구조다.

def judge(routine, last_done_at, now=None):
    """지금 이 약속이 어떤 상태인가. (상태, 설명) 을 돌려준다."""
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    every = routine["interval_days"]

    if not routine.get("active", True):
        return "paused", "멈춰 둔 항목입니다."

    if last_done_at is None:
        return "never", f"{every}일 주기인데 수행 기록이 없습니다."

    due = last_done_at + timedelta(days=every)
    if now >= due:
        over = (now - due).days
        return "overdue", (f"{over}일 지났습니다." if over
                           else "오늘이 기한입니다.")

    left = (due - now).days
    if (due - now) <= timedelta(days=every * SOON_RATIO):
        return "soon", f"{left}일 남았습니다."
    return "ok", f"{left}일 남았습니다."


# ----------------------------------------------------------------------
# 약속
# ----------------------------------------------------------------------

def add(customer, name, interval_days, why=""):
    """주기 업무를 등록한다. 같은 고객사에 같은 이름이면 갱신한다."""
    customer, name = (customer or "").strip(), (name or "").strip()
    if not customer:
        raise RoutineError("고객사를 고르세요.")
    if not name:
        raise RoutineError("점검 이름을 입력하세요.")
    try:
        interval_days = int(interval_days)
    except (TypeError, ValueError):
        raise RoutineError("주기는 숫자(일)로 적으세요.")
    if interval_days < 1:
        raise RoutineError("주기는 1일 이상이어야 합니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "customer_routines")
        cur.execute(
            """
            INSERT INTO customer_routines (customer, name, interval_days, why)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (customer, name) DO UPDATE SET
                interval_days = EXCLUDED.interval_days,
                why           = EXCLUDED.why,
                active        = true
            RETURNING id
            """,
            (customer, name, interval_days, (why or "").strip()),
        )
        return cur.fetchone()[0]


def set_active(routine_id, active):
    """멈추거나 다시 시작한다. 지우지 않는 이유는 수행 이력 때문이다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "customer_routines")
        cur.execute(
            "UPDATE customer_routines SET active = %s WHERE id = %s RETURNING id",
            (bool(active), routine_id),
        )
        if cur.fetchone() is None:
            raise RoutineError("점검 항목을 찾지 못했습니다.")


def mark_done(routine_id, done_by, note=""):
    """이번 주기 것을 했다고 남긴다.

    이름과 고객사를 복사해 둔다. 약속을 지운 뒤에도 '그때 했다' 가
    무엇에 대한 것인지 알 수 있어야 한다(runbook_runs 와 같은 이유).
    """
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "customer_routines")
        _ensure(cur, "routine_runs")
        cur.execute(
            "SELECT customer, name FROM customer_routines WHERE id = %s",
            (routine_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise RoutineError("점검 항목을 찾지 못했습니다.")

        cur.execute(
            "INSERT INTO routine_runs (routine_id, customer, name, done_by, note) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (routine_id, row[0], row[1], done_by or "", (note or "").strip()),
        )
        return cur.fetchone()[0]


def listing(customer=""):
    """약속과 그 상태. 손대야 하는 것이 위로 온다."""
    where, params = [], []
    if customer:
        where.append("r.customer = %s")
        params.append(customer)

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "customer_routines")
        _ensure(cur, "routine_runs")
        # 마지막 수행을 LATERAL 로 한 번에 붙인다. 항목마다 질의하면
        # 고객사가 늘수록 화면이 느려진다.
        cur.execute(
            """
            SELECT r.*, last.done_at AS last_done_at, last.done_by AS last_done_by,
                   last.note AS last_note
              FROM customer_routines r
              LEFT JOIN LATERAL (
                    SELECT done_at, done_by, note
                      FROM routine_runs
                     WHERE routine_id = r.id
                     ORDER BY done_at DESC
                     LIMIT 1
              ) last ON true
            """
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY r.customer, r.name",
            params,
        )
        items = _rows(cur)

    order = ("overdue", "never", "soon", "ok", "paused")
    for item in items:
        item["state"], item["detail"] = judge(item, item["last_done_at"])
    items.sort(key=lambda i: (order.index(i["state"]), i["customer"], i["name"]))
    return items


def runs(routine_id, limit=20):
    """이 약속의 수행 이력."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "routine_runs")
        cur.execute(
            "SELECT * FROM routine_runs WHERE routine_id = %s "
            " ORDER BY done_at DESC LIMIT %s",
            (routine_id, limit),
        )
        return _rows(cur)


def summary(items):
    """화면 위쪽 숫자. 목록을 이미 읽었으니 다시 질의하지 않는다."""
    counts = {state: 0 for state in STATE_LABEL}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    counts["total"] = len(items)
    counts["alert"] = sum(counts[s] for s in ALERT_STATES)
    return counts
