# app/jobs.py
# 배치(CLI 명령)가 언제 돌았는지.
#
# ── 왜 필요한가 ─────────────────────────────────────────────────────
# 이 앱의 자동화는 전부 CLI 다. compliance-check, sla-check, handover,
# prune-events. cron 에 걸어두고 죽으면 아무도 모른다.
#
# 무서운 건 실패가 아니라 침묵이다. 컴플라이언스 점검이 3주째 안 돌아도
# 화면은 3주 전 스냅샷 기준 결과를 아무 표시 없이 보여준다. 읽는 사람은
# 그게 오늘 상태라고 믿는다.
#
# ── 기록을 쌓는다 ───────────────────────────────────────────────────
# 마지막 것만 덮어쓰지 않는다. "어제도 실패했나" 를 알아야 하기 때문이다.
# 한 번 실패는 흔하지만 연속 실패는 다른 문제다.

from datetime import datetime, timedelta, timezone

from flask import current_app

# 정기적으로 돌아야 하는 명령과, 이만큼 안 돌면 이상한 것으로 볼 시간(시간).
# 여기 없는 명령도 기록은 남지만 '안 돌았다' 경고는 하지 않는다 -
# add-account 처럼 사람이 필요할 때만 부르는 것들이다.
EXPECTED = {
    "compliance-check": {"hours": 26, "why": "매일 한 번"},
    "sla-check":        {"hours": 2,  "why": "한 시간마다"},
    "handover":         {"hours": 26, "why": "근무 교대마다"},
    "collect-resources": {"hours": 26, "why": "매일 한 번"},
    # 주 단위라 넉넉하게 잡는다. 하루쯤 늦게 도는 것보다, 한 주를
    # 통째로 안 보낸 것을 놓치는 쪽이 문제다.
    "weekly-report": {"hours": 24 * 8, "why": "매주 한 번"},
    "prune-events":     {"hours": 24 * 8, "why": "주 한 번"},
}

# 시작만 하고 이만큼 지나도 안 끝났으면 죽은 것으로 본다.
# 프로세스가 중간에 사라지면 outcome 이 running 인 채로 남는다.
STUCK_HOURS = 6


class JobError(Exception):
    """실행 기록을 읽거나 쓰는 데 실패했을 때."""


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
        raise JobError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise JobError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _ready(cur):
    cur.execute("SELECT to_regclass('public.job_runs')")
    return cur.fetchone()[0] is not None


def start(job):
    """실행을 시작했다고 남기고 id 를 돌려준다.

    실패해도 예외를 밖으로 던지지 않는다. 기록을 못 남겼다고 배치 자체가
    멎으면, 정작 해야 할 일(점검·정리)이 안 된다. 감사 로그와 같은 판단이다.
    """
    try:
        with _connect() as conn, conn.cursor() as cur:
            if not _ready(cur):
                return None
            cur.execute(
                "INSERT INTO job_runs (job) VALUES (%s) RETURNING id", (job,)
            )
            return cur.fetchone()[0]
    except Exception as e:
        current_app.logger.warning("배치 기록을 시작하지 못했습니다: %s", e)
        return None


def finish(run_id, outcome="ok", summary="", detail="", meta=None):
    """실행이 끝났다고 남긴다. run_id 가 None 이면 아무것도 안 한다."""
    import json

    if run_id is None:
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            if not _ready(cur):
                return
            cur.execute(
                "UPDATE job_runs SET ended_at = now(), outcome = %s, "
                "       summary = %s, detail = %s, meta = %s "
                " WHERE id = %s",
                (outcome, summary[:500], detail[:2000],
                 json.dumps(meta or {}, ensure_ascii=False, default=str), run_id),
            )
    except Exception as e:
        current_app.logger.warning("배치 기록을 마치지 못했습니다: %s", e)


def recent(job=None, limit=50):
    """최근 실행 기록."""
    with _connect() as conn, conn.cursor() as cur:
        if not _ready(cur):
            raise JobError("job_runs 테이블이 없습니다.")
        sql = "SELECT * FROM job_runs"
        params = []
        if job:
            sql += " WHERE job = %s"
            params.append(job)
        sql += " ORDER BY started_at DESC LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        return _rows(cur)


def status():
    """명령마다 마지막 실행이 언제였고 지금 상태가 어떤가.

    EXPECTED 에 적힌 명령은 한 번도 안 돌았어도 목록에 나온다.
    '기록이 없다' 와 '그런 명령이 없다' 는 다르다 - 앞은 문제고 뒤는 아니다.
    """
    now = datetime.now(timezone.utc)

    with _connect() as conn, conn.cursor() as cur:
        if not _ready(cur):
            raise JobError("job_runs 테이블이 없습니다.")
        cur.execute(
            """
            SELECT DISTINCT ON (job)
                   job, started_at, ended_at, outcome, summary, detail
              FROM job_runs
             ORDER BY job, started_at DESC
            """
        )
        last = {r["job"]: r for r in _rows(cur)}

        # 연속 실패는 한 번 실패와 다른 문제다. 최근 5회를 보고 센다.
        cur.execute(
            """
            SELECT job, outcome FROM (
                SELECT job, outcome,
                       row_number() OVER (PARTITION BY job ORDER BY started_at DESC) AS n
                  FROM job_runs
            ) t WHERE n <= 5
            ORDER BY job, n
            """
        )
        streaks = {}
        for row in _rows(cur):
            bucket = streaks.setdefault(row["job"], [])
            bucket.append(row["outcome"])

    out = []
    for job in sorted(set(EXPECTED) | set(last)):
        row = last.get(job)
        expect = EXPECTED.get(job)
        recent_outcomes = streaks.get(job, [])

        # 연속 실패 수 (최근 것부터 세다가 성공을 만나면 멈춘다)
        fails = 0
        for outcome in recent_outcomes:
            if outcome == "failed":
                fails += 1
            else:
                break

        state, note = _judge(row, expect, now, fails)
        out.append({
            "job": job,
            "expected": expect,
            "last": row,
            "state": state,
            "note": note,
            "consecutive_failures": fails,
            "age_hours": (
                round((now - row["started_at"]).total_seconds() / 3600, 1)
                if row else None
            ),
        })
    return out


def _judge(row, expect, now, fails):
    """상태 한 가지와 이유 한 줄."""
    if row is None:
        if expect:
            return "never", f"한 번도 돌지 않았습니다({expect['why']} 돌아야 합니다)."
        return "never", "기록이 없습니다."

    age = now - row["started_at"]

    if row["outcome"] == "running":
        if age > timedelta(hours=STUCK_HOURS):
            return "stuck", (
                f"{round(age.total_seconds() / 3600)}시간째 끝나지 않았습니다. "
                "중간에 죽었을 수 있습니다."
            )
        return "running", "지금 돌고 있습니다."

    if row["outcome"] == "failed":
        if fails >= 2:
            return "failing", f"{fails}회 연속 실패했습니다."
        return "failed", "마지막 실행이 실패했습니다."

    if expect and age > timedelta(hours=expect["hours"]):
        return "stale", (
            f"마지막 실행이 {round(age.total_seconds() / 3600)}시간 전입니다"
            f"({expect['why']} 돌아야 합니다)."
        )

    return "ok", "정상입니다."


# 화면에서 쓸 표시. 색으로만 구분하지 않고 글자를 함께 둔다.
STATE_LABEL = {
    "ok": "정상", "stale": "오래됨", "failed": "실패", "failing": "연속 실패",
    "stuck": "멈춤", "running": "실행 중", "never": "기록 없음",
}

# 눈여겨봐야 할 상태. 'ok' 와 'running' 만 괜찮다.
ALERT_STATES = ("stale", "failed", "failing", "stuck", "never")


def problems():
    """문제 있는 것만. 관리자 화면 배너에 쓴다."""
    return [s for s in status() if s["state"] in ALERT_STATES]
