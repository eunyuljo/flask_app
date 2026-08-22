# app/audit.py
# 고객사 계정을 건드린 기록을 남기고 읽는다.
#
# 처음에는 event_store(메모리, 100건)에 넣었다. 재시작하면 사라지고
# 101건째부터 앞이 밀려나므로 감사 로그로 쓸 수 없다. DB 로 옮겼다.

from flask import current_app, session

# 결과 값과 화면에 쓸 한국어.
OUTCOMES = {
    "ok": "실행됨",
    "failed": "실행됨(오류)",
    "rejected": "차단됨",
    "exec_failed": "실행 실패",
    "no_credentials": "자격증명 없음",
}

# 감사에서 눈여겨봐야 할 것. 사용자가 금지된 명령을 시도한 기록이다.
ALERT_OUTCOMES = ("rejected",)

ACTIONS = {
    "console_command": "콘솔 명령",
    "ai_diagnose": "AI 진단",
    "user_manage": "계정 관리",
    "compliance_exception": "컴플라이언스 예외",
    "rca_draft": "사후 보고서 초안",
    "incident_jira": "사후 보고서 Jira 등록",
    "health_check": "연동 점검",
}

ACTOR_KINDS = {"human": "사람", "agent": "모델"}


class AuditError(Exception):
    """감사 로그를 읽거나 쓰는 데 실패했을 때."""


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
        raise AuditError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise AuditError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def record(action, outcome, summary="", detail="", account=None,
           region="", actor_kind="human", meta=None):
    """기록 한 줄을 남긴다.

    기록에 실패해도 예외를 밖으로 던지지 않는다. 감사 로그를 못 남겼다고
    사용자의 작업까지 실패시키면, DB 가 잠깐 흔들릴 때 콘솔 전체가 멎는다.
    대신 앱 로그에 남겨서 놓치지 않게 한다.

    (감사 기록이 반드시 남아야 하는 환경이라면 반대로 막아야 한다.
     그건 이 앱의 성격을 넘는 판단이라 여기서 정하지 않는다.)
    """
    import json

    account = account or {}
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.audit_log')")
            if cur.fetchone()[0] is None:
                current_app.logger.warning("audit_log 테이블이 없어 감사 기록을 건너뜁니다")
                return
            cur.execute(
                """
                INSERT INTO audit_log
                    (actor, actor_kind, action, customer, account_id, region,
                     outcome, summary, detail, meta)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session.get("username", ""), actor_kind, action,
                    account.get("customer", ""), account.get("account_id", ""),
                    region, outcome, summary[:500], detail[:2000],
                    json.dumps(meta or {}, ensure_ascii=False, default=str),
                ),
            )
    except Exception as e:
        current_app.logger.warning("감사 기록에 실패했습니다: %s", e)


def recent(limit=100, outcome=None, account_id=None, actor_kind=None):
    """최근 기록. 필터는 전부 선택이다."""
    where, params = [], []
    # 값은 전부 %s 로 나간다. 아래 필터 값이 SQL 본문에 들어가지 않는다.
    if outcome in OUTCOMES:
        where.append("outcome = %s")
        params.append(outcome)
    if account_id:
        where.append("account_id = %s")
        params.append(account_id)
    if actor_kind in ACTOR_KINDS:
        where.append("actor_kind = %s")
        params.append(actor_kind)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.audit_log')")
        if cur.fetchone()[0] is None:
            raise AuditError(
                "audit_log 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )
        cur.execute(
            f"SELECT * FROM audit_log {where_sql} ORDER BY at DESC LIMIT %s",
            params,
        )
        return _rows(cur)


def summary(hours=168):
    """결과별 건수. 차단된 시도가 몇 건인지가 제일 중요하다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.audit_log')")
        if cur.fetchone()[0] is None:
            raise AuditError("audit_log 테이블이 없습니다.")
        cur.execute(
            "SELECT outcome, count(*) FROM audit_log "
            "WHERE at >= now() - make_interval(hours => %s) GROUP BY outcome",
            (hours,),
        )
        by_outcome = dict(cur.fetchall())
        cur.execute(
            "SELECT actor_kind, count(*) FROM audit_log "
            "WHERE at >= now() - make_interval(hours => %s) GROUP BY actor_kind",
            (hours,),
        )
        by_actor = dict(cur.fetchall())

    return {
        "hours": hours,
        "total": sum(by_outcome.values()),
        "by_outcome": by_outcome,
        "by_actor": by_actor,
        "alerts": sum(by_outcome.get(o, 0) for o in ALERT_OUTCOMES),
    }
