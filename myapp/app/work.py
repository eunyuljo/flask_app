# app/work.py
# 작업 기록(work order)을 읽고 쓴다.
# 작업 전/후로 스냅샷을 한 벌씩 찍어두고, 그 차이를 증적으로 남기기 위한 도메인이다.
# SQL 은 전부 여기에만 있고, 뷰는 여기서 나온 dict 만 받는다.

from flask import current_app

# 상태 전이. 이 순서를 벗어나는 요청은 거부한다.
#
#   requested -> open(승인됨) -> before_taken -> after_taken -> closed
#             -> rejected
#
# open 의 뜻은 바꾸지 않았다. 이미 쌓인 기록이 전부 open 이라, 이름을
# 바꾸면 지난 작업이 미승인으로 보인다.
FLOW = ("requested", "open", "before_taken", "after_taken", "closed")

STATUS_LABEL = {
    "requested": "승인 대기",
    "rejected": "반려됨",
    "open": "승인됨 · 작업 전 스냅샷 대기",
    "before_taken": "작업 진행 가능",
    "after_taken": "차이 확인 / 증적 확정 대기",
    "closed": "증적 확정됨",
}

# 아직 손댈 수 있는 상태. 반려와 확정은 끝난 것이다.
OPEN_STATES = ("requested", "open", "before_taken", "after_taken")


class WorkError(Exception):
    """작업 기록을 읽거나 쓰는 데 실패했을 때."""


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
        raise WorkError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        conn = psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise WorkError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise
    return conn


def _ensure_table(cur):
    cur.execute("SELECT to_regclass('public.work_orders')")
    if cur.fetchone()[0] is None:
        raise WorkError(
            "work_orders 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


def create(title, customer, account_id, region, operator,
           ticket="", request="", expected="", requested_by="",
           rollback="", window_start=None, window_end=None):
    """작업을 요청한다. 승인 전에는 스냅샷도 못 찍는다(status=requested).

    requested_by 를 따로 받는 이유: operator 는 '작업할 사람' 이고
    요청자는 다를 수 있다. 그리고 자기 요청을 자기가 승인하지 못하게
    하려면 누가 요청했는지를 알아야 한다.
    """
    if not title.strip():
        raise WorkError("작업 제목을 입력하세요.")
    if window_start and window_end and window_end <= window_start:
        raise WorkError("작업창 종료가 시작보다 빠릅니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            INSERT INTO work_orders
                (ticket, title, request, expected, customer, account_id, region,
                 operator, requested_by, rollback, window_start, window_end, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'requested')
            RETURNING id
            """,
            (ticket.strip(), title.strip(), request.strip(), expected.strip(),
             customer, account_id, region, operator,
             requested_by or operator, rollback.strip(), window_start, window_end),
        )
        return cur.fetchone()[0]


def approve(work_id, approver, note=""):
    """작업을 승인한다.

    자기가 낸 요청은 자기가 승인할 수 없다. 승인이 형식만 남으면 없는 것과
    같아서, 이 규칙 하나가 나머지를 지탱한다.

    판정을 SQL 의 WHERE 로 한다. 두 사람이 거의 동시에 눌러도 한 번만
    통과해야 하는데, 파이썬에서 상태를 읽고 판단한 뒤 UPDATE 하면 그
    사이가 열려 있다.
    """
    approver = (approver or "").strip()
    if not approver:
        raise WorkError("승인자를 알 수 없습니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            UPDATE work_orders
               SET status = 'open', approved_by = %s, approved_at = now(),
                   decided_note = %s
             WHERE id = %s AND status = 'requested' AND requested_by <> %s
            RETURNING id
            """,
            (approver, note.strip(), work_id, approver),
        )
        if cur.fetchone() is None:
            _explain_decision_failure(cur, work_id, approver, "승인")


def reject(work_id, approver, note=""):
    """작업을 반려한다. 사유가 없으면 요청자가 무엇을 고쳐야 할지 모른다."""
    approver = (approver or "").strip()
    if not note.strip():
        raise WorkError("반려 사유를 적어야 합니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            UPDATE work_orders
               SET status = 'rejected', approved_by = %s, approved_at = now(),
                   decided_note = %s
             WHERE id = %s AND status = 'requested' AND requested_by <> %s
            RETURNING id
            """,
            (approver, note.strip(), work_id, approver),
        )
        if cur.fetchone() is None:
            _explain_decision_failure(cur, work_id, approver, "반려")


def _explain_decision_failure(cur, work_id, approver, action):
    """왜 승인/반려가 안 됐는지 알려준다.

    "안 됩니다" 만 하면 요청자도 승인자도 무엇을 해야 할지 모른다.
    """
    cur.execute(
        "SELECT status, requested_by FROM work_orders WHERE id = %s", (work_id,)
    )
    row = cur.fetchone()
    if row is None:
        raise WorkError("작업 기록을 찾지 못했습니다.")
    status, requested_by = row
    if status != "requested":
        raise WorkError(
            f"{action}할 수 있는 상태가 아닙니다(지금: {STATUS_LABEL.get(status, status)})."
        )
    raise WorkError(
        "자기가 낸 요청은 자기가 승인할 수 없습니다. "
        f"이 요청은 {requested_by} 님이 냈습니다."
    )


def pending(limit=50):
    """승인 대기 중인 작업. 승인자가 가장 먼저 보는 목록이다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            "SELECT * FROM work_orders WHERE status = 'requested' "
            " ORDER BY created_at LIMIT %s",
            (limit,),
        )
        return _rows(cur)


def window_state(item, now=None):
    """지금이 작업창 안인가.

    벗어나도 막지 않는다. 막으면 급할 때 이 도구를 통째로 우회하고,
    그러면 증적이 아예 안 남는다. 대신 벗어났다는 사실을 증적에 남긴다.
    """
    from datetime import datetime, timezone

    start, end = item.get("window_start"), item.get("window_end")
    if not start or not end:
        return {"has_window": False, "inside": None, "note": "작업창을 정하지 않았습니다."}

    now = now or datetime.now(timezone.utc)
    if now < start:
        return {"has_window": True, "inside": False,
                "note": f"작업창은 {start:%m-%d %H:%M} 부터입니다(아직 이릅니다)."}
    if now > end:
        return {"has_window": True, "inside": False,
                "note": f"작업창이 {end:%m-%d %H:%M} 에 끝났습니다."}
    return {"has_window": True, "inside": True,
            "note": f"작업창 안입니다({start:%m-%d %H:%M} ~ {end:%m-%d %H:%M})."}


def mark_out_of_window(work_id):
    """작업창을 벗어나서 시작했다고 표시한다. 되돌리지 않는다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            "UPDATE work_orders SET out_of_window = true WHERE id = %s", (work_id,)
        )


def get(work_id):
    """작업 기록 하나. 없으면 None."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute("SELECT * FROM work_orders WHERE id = %s", (work_id,))
        rows = _rows(cur)
    return rows[0] if rows else None


def recent(limit=30, customer=None, account_id=None):
    """최근 작업 기록 목록."""
    where, params = [], []
    if customer:
        where.append("customer = %s")
        params.append(customer)
    if account_id:
        where.append("account_id = %s")
        params.append(account_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            f"SELECT * FROM work_orders {where_sql} ORDER BY id DESC LIMIT %s",
            params,
        )
        return _rows(cur)


# phase -> (스냅샷을 담을 열, 이 상태일 때만 허용, 넘어갈 상태)
PHASES = {
    "before": ("before_snapshot_id", "open", "before_taken"),
    "after": ("after_snapshot_id", "before_taken", "after_taken"),
}


def check_transition(item, phase):
    """이 단계로 넘어갈 수 있는 상태인지 미리 본다.

    실제 판정은 attach_snapshot 의 UPDATE ... WHERE 가 다시 하지만,
    수집을 시작하기 전에 여기서 한 번 걸러야 한다. 리소스 수집은 고객사
    계정에 실제 조회를 날리는 일이라, 어차피 거부될 요청에 그 비용을
    치를 이유가 없다.
    """
    if phase not in PHASES:
        raise WorkError(f"알 수 없는 단계: {phase!r}")

    _, require, _ = PHASES[phase]
    if item["status"] != require:
        raise WorkError(
            f"지금 상태({STATUS_LABEL[item['status']]})에서는 "
            f"{'작업 전' if phase == 'before' else '작업 후'} 스냅샷을 찍을 수 없습니다."
        )


def attach_snapshot(work_id, phase, snapshot_id):
    """작업 전/후 스냅샷을 붙이고 상태를 넘긴다.

    phase: "before" 또는 "after"

    상태를 확인하고 넘기는 일을 SQL 한 방에 묶는다. 파이썬에서 읽고-판단하고-쓰면
    두 사람이 동시에 누를 때 둘 다 통과할 수 있다(확인과 쓰기 사이가 벌어진다).
    WHERE 절에 현재 상태를 넣으면 DB 가 한 번만 통과시킨다.
    """
    if phase not in PHASES:
        raise WorkError(f"알 수 없는 단계: {phase!r}")
    column, require, becomes = PHASES[phase]

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            f"""
            UPDATE work_orders
               SET {column} = %s, status = %s
             WHERE id = %s AND status = %s
            RETURNING id
            """,
            (snapshot_id, becomes, work_id, require),
        )
        if cur.fetchone() is None:
            current = get(work_id)
            if current is None:
                raise WorkError("작업 기록을 찾지 못했습니다.")
            raise WorkError(
                f"지금 상태({STATUS_LABEL[current['status']]})에서는 "
                f"{'작업 전' if phase == 'before' else '작업 후'} 스냅샷을 찍을 수 없습니다."
            )


def close(work_id, note=""):
    """증적을 확정한다. 확정 후에는 스냅샷을 바꾸지 않는다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            UPDATE work_orders
               SET status = 'closed', closed_at = now(), note = %s
             WHERE id = %s AND status = 'after_taken'
            RETURNING id
            """,
            (note.strip(), work_id),
        )
        if cur.fetchone() is None:
            current = get(work_id)
            if current is None:
                raise WorkError("작업 기록을 찾지 못했습니다.")
            raise WorkError(
                f"작업 후 스냅샷을 찍어야 확정할 수 있습니다 "
                f"(지금: {STATUS_LABEL[current['status']]})."
            )


# ----------------------------------------------------------------------
# 흐름이 멈춘 작업
# ----------------------------------------------------------------------
# 작업 한 건에는 사람이 여섯 번 손을 댄다. 그중 어디서든 멈출 수 있는데,
# 멈춘 것을 아무도 안 본다. app/jobs.py 가 배치에 대해 하는 일을 작업
# 흐름에 대해서도 한다 - 무서운 건 실패가 아니라 침묵이다.
#
# 가장 위험한 것은 before_taken 이다. 작업 전 스냅샷만 찍고 멈춘 상태에서
# 리소스가 바뀌면, "요청한 것만 바뀌었다" 를 증명할 방법이 없다.
# 반쪽 증적은 없느니만 못하다 - 있다고 착각하게 만들기 때문이다.

# 상태별로 이 시간을 넘기면 멈춘 것으로 본다.
#
# 값이 다른 이유: 승인은 사람을 기다리는 일이라 하루쯤 걸릴 수 있지만,
# 작업 전 스냅샷을 찍어놓고 반나절이 지났다면 그건 작업이 끊긴 것이다.
STALE_HOURS = {
    "requested":    24,   # 승인자가 모르고 있을 수 있다
    "open":         72,   # 승인해 놓고 시작을 안 했다
    "before_taken":  8,   # 증적이 반쪽 - 가장 위험
    "after_taken":  48,   # 차이를 아무도 안 봤다
}

# 왜 문제인지. 알림과 화면이 같은 문장을 쓴다.
STALE_WHY = {
    "requested":   "승인을 기다리는 중입니다. 승인자가 요청을 못 봤을 수 있습니다.",
    "open":        "승인됐는데 시작하지 않았습니다. 작업창이 지났을 수도 있습니다.",
    "before_taken": "작업 전 스냅샷만 있습니다. 지금 리소스가 바뀌면 "
                    "'요청한 것만 바뀌었다' 를 증명할 수 없습니다.",
    "after_taken": "차이를 확인하지 않았습니다. 증적이 확정되지 않은 채 남아 있습니다.",
}

# 손대야 하는 순서. 위험한 것이 위로 온다.
STALE_ORDER = ("before_taken", "after_taken", "open", "requested")


def stalled(now=None):
    """흐름이 멈춘 작업. 상태마다 다른 기준으로 본다.

    돌려주는 것: [{...작업, "stale_hours": 경과, "why": 설명}, ...]

    기준 시각을 인자로 받는 이유는 테스트에서 시간을 옮겨보기 위해서다
    (jobs._judge, routines.judge 와 같은 이유).
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        # 상태가 바뀐 시각을 따로 저장하는 칼럼은 없다. 대신 그 상태에서
        # 마지막으로 일어난 일의 시각을 쓴다 - 스냅샷을 찍은 시각은
        # resource_snapshots.collected_at 에 이미 있다.
        #
        # 새 칼럼을 더하는 것보다, 이미 있는 사실로 답할 수 있으면 그쪽이 낫다.
        # (칼럼을 더하면 지난 작업들은 그 값이 비어 있어 판정에서 빠진다.)
        cur.execute(
            """
            SELECT w.*,
                   COALESCE(af.collected_at, bf.collected_at,
                            w.approved_at, w.created_at) AS last_move
              FROM work_orders w
              LEFT JOIN resource_snapshots bf
                     ON bf.snapshot_id = w.before_snapshot_id
              LEFT JOIN resource_snapshots af
                     ON af.snapshot_id = w.after_snapshot_id
             WHERE w.status = ANY(%s)
             ORDER BY last_move
            """,
            (list(STALE_HOURS),),
        )
        rows = _rows(cur)

    out = []
    for row in rows:
        hours = (now - row["last_move"]).total_seconds() / 3600
        if hours < STALE_HOURS[row["status"]]:
            continue
        out.append({**row, "stale_hours": int(hours),
                    "limit_hours": STALE_HOURS[row["status"]],
                    "why": STALE_WHY[row["status"]]})

    out.sort(key=lambda r: (STALE_ORDER.index(r["status"]), -r["stale_hours"]))
    return out


def stalled_summary(rows):
    """화면과 알림 위쪽 숫자."""
    counts = {state: 0 for state in STALE_HOURS}
    for row in rows:
        counts[row["status"]] += 1
    return {
        "total": len(rows),
        "by_status": counts,
        # 증적이 반쪽인 것. 다른 것과 무게가 다르다.
        "evidence_at_risk": counts["before_taken"],
    }


def to_slack_stalled(rows, base_url=""):
    """멈춘 작업을 Slack mrkdwn 으로."""
    from app.slack import escape

    if not rows:
        return ""

    counts = stalled_summary(rows)
    L = [f"*멈춘 작업 {counts['total']}건*"]
    if counts["evidence_at_risk"]:
        L.append(f"이 중 *{counts['evidence_at_risk']}건은 증적이 반쪽*입니다 "
                 "(작업 전 스냅샷만 있음).")
    L.append("")

    for row in rows[:10]:
        label = STATUS_LABEL.get(row["status"], row["status"])
        line = (f"• `{label}` {row['stale_hours']}시간 — "
                f"{escape(row['title'])} ({escape(row['customer'])})")
        if base_url:
            line += f" <{base_url.rstrip('/')}/work/{row['id']}|열기>"
        L.append(line)

    if len(rows) > 10:
        L.append(f"_외 {len(rows) - 10}건_")
    return "\n".join(L)


def due_for_auto_snapshot(now=None, grace_minutes=30):
    """작업창이 시작됐는데 작업 전 스냅샷이 아직 없는 작업.

    작업창을 새벽 2시로 잡아놓고 그 시각에 사람이 로그인해서 버튼을
    누르는 것은 기계가 할 일이다.

    ── 무엇을 자동으로 하지 않는가 ──────────────────────────────
    작업 후 스냅샷은 자동으로 찍지 않는다. 작업이 끝났는지 기계가 알 수
    없어서, 창 종료 시각에 찍으면 작업 도중 상태가 '작업 후' 로 남는다.
    그건 증적이 아니라 잘못된 증적이다. 대신 '창이 끝났는데 작업 후가
    없다' 는 stalled() 가 잡는다.

    승인도 자동으로 하지 않는다. 자기 승인 금지가 이 흐름의 핵심이다.

    grace_minutes: 창 시작 직후 이만큼은 기다린다. 사람이 직접 찍으려고
        들어오는 중일 수 있고, 그 경우 사람이 찍은 것이 '작업 직전' 에
        더 가깝다. 자동은 사람이 안 할 때의 보험이다.
    """
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=grace_minutes)

    with _connect() as conn, conn.cursor() as cur:
        _ensure_table(cur)
        cur.execute(
            """
            SELECT * FROM work_orders
             WHERE status = 'open'
               AND before_snapshot_id IS NULL
               AND window_start IS NOT NULL
               AND window_start <= %s
               -- 창이 이미 끝난 것은 자동으로 찍지 않는다. 그때 찍은
               -- 스냅샷은 '작업 전' 이 아니라 '한참 뒤' 이고, 오히려
               -- 증적을 어지럽힌다. 그건 stalled() 가 잡는다.
               AND (window_end IS NULL OR window_end >= %s)
             ORDER BY window_start
            """,
            (cutoff, now),
        )
        return _rows(cur)
