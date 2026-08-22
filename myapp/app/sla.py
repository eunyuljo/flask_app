# app/sla.py
# 최초 대응 시간(SLA)을 집계한다.
#
# ── 이 모듈이 재는 것과 재지 못하는 것 ────────────────────────────────
# 재는 것 : 알람이 난 뒤, 그 알람의 계정을 우리가 처음 들여다본 시각까지의 시간
# 재지 못하는 것 : "이 알람에 대응했는가"
#
# audit_log 는 계정 단위 기록이라, 어떤 조회가 어떤 알람에 대한 대응인지
# 알 수 없다. 같은 계정에 알람이 여러 개 떠 있으면 첫 조회 하나가 그
# 전부의 대응 시각으로 잡힌다. AI 진단만 meta.fingerprint 로 알람을
# 정확히 가리키므로, 그 경우에는 지문까지 맞춰본다.
#
# 이 한계를 화면과 문서에 적는다. 근사치를 계약 이행 증거로 내밀면
# 나중에 훨씬 곤란해진다.

from flask import current_app

SEVERITIES = ("critical", "error", "warning", "info")

# 기본 목표. 고객사별 설정이 없을 때 화면이 비어 보이지 않게 하는 값이며,
# 계약과 무관하다. 실제 값은 sla_targets 에 넣어야 한다.
SUGGESTED = {"critical": 30, "error": 120, "warning": 0, "info": 0}


class SlaError(Exception):
    """SLA 집계나 목표 저장에 실패했을 때."""


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
        raise SlaError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise SlaError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _ensure(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    if cur.fetchone()[0] is None:
        raise SlaError(
            f"{name} 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


# ----------------------------------------------------------------------
# 목표
# ----------------------------------------------------------------------

def targets(customer=""):
    """이 고객사에 적용되는 목표. {심각도: 분} 을 돌려준다.

    고객사 전용이 있으면 그것을, 없으면 기본값(customer='')을 쓴다.
    """
    result = {}
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "sla_targets")
        cur.execute(
            """
            SELECT DISTINCT ON (severity) severity, first_response_minutes, customer, note
              FROM sla_targets
             WHERE customer IN (%s, '')
             ORDER BY severity, (customer = '') ASC
            """,
            (customer or "",),
        )
        for row in _rows(cur):
            result[row["severity"]] = row
    return result


def save_target(customer, severity, minutes, note=""):
    """목표를 만들거나 갱신한다."""
    if severity not in SEVERITIES:
        raise SlaError(f"알 수 없는 심각도입니다: {severity}")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise SlaError("목표 시간은 숫자(분)여야 합니다.")
    if minutes < 0:
        raise SlaError("목표 시간은 0 이상이어야 합니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "sla_targets")
        cur.execute(
            """
            INSERT INTO sla_targets (customer, severity, first_response_minutes, note)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (customer, severity) DO UPDATE SET
                first_response_minutes = EXCLUDED.first_response_minutes,
                note = EXCLUDED.note,
                updated_at = now()
            """,
            (customer or "", severity, minutes, note.strip()),
        )


def delete_target(customer, severity):
    """목표를 지운다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "sla_targets")
        cur.execute(
            "DELETE FROM sla_targets WHERE customer = %s AND severity = %s "
            "RETURNING severity",
            (customer or "", severity),
        )
        if cur.fetchone() is None:
            raise SlaError("목표를 찾지 못했습니다.")


# ----------------------------------------------------------------------
# 집계
# ----------------------------------------------------------------------

def measure(customer, days=30):
    """이 고객사의 최초 대응 시간을 잰다.

    알람 하나마다: 발생 시각 이후 그 계정에 대한 첫 감사 기록까지의 분.
    감사 기록이 없으면 '미대응' 으로 센다.
    """
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "audit_log")
        _ensure(cur, "sla_targets")

        cur.execute(
            "SELECT account_id FROM aws_accounts WHERE customer = %s", (customer,)
        )
        account_ids = [r[0] for r in cur.fetchall()]
        if not account_ids:
            return {
                "customer": customer, "days": days, "accounts": [],
                "by_severity": [], "total": 0, "measured": 0, "unattributed": 0,
            }

        # 알람마다 '그 계정을 그 시각 이후 처음 들여다본 때' 를 붙인다.
        #
        # LATERAL 을 쓰는 이유: 알람 행마다 audit_log 를 따로 뒤져야 한다.
        # 그냥 조인하면 알람 × 감사기록 만큼 행이 부풀고, 그 뒤에 min() 을
        # 걸어도 이벤트가 많아지면 감당하지 못한다.
        cur.execute(
            """
            SELECT e.severity,
                   count(*)                                   AS total,
                   count(a.at)                                AS answered,
                   COALESCE(percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (a.at - e.occurred_at)) / 60
                   ), 0)                                      AS median_minutes,
                   COALESCE(max(EXTRACT(EPOCH FROM (a.at - e.occurred_at)) / 60), 0)
                                                              AS worst_minutes
              FROM events e
              LEFT JOIN LATERAL (
                    SELECT al.at
                      FROM audit_log al
                     WHERE al.account_id = e.account_id
                       AND al.at >= e.occurred_at
                     ORDER BY al.at
                     LIMIT 1
                   ) a ON true
             WHERE e.account_id = ANY(%s)
               AND e.occurred_at >= now() - make_interval(days => %s)
             GROUP BY e.severity
            """,
            (account_ids, days),
        )
        measured = {r["severity"]: r for r in _rows(cur)}

        # 계정을 모르는 이벤트. 이 수가 크면 위 숫자가 전체를 대표하지 못한다.
        cur.execute(
            "SELECT count(*) FROM events WHERE account_id = '' "
            "AND occurred_at >= now() - make_interval(days => %s)",
            (days,),
        )
        unattributed = cur.fetchone()[0]

    goals = targets(customer)

    rows = []
    for severity in SEVERITIES:
        stat = measured.get(severity)
        goal = goals.get(severity)
        minutes = goal["first_response_minutes"] if goal else 0
        total = stat["total"] if stat else 0
        answered = stat["answered"] if stat else 0

        rows.append({
            "severity": severity,
            "target_minutes": minutes,
            "target_scope": (goal["customer"] or "기본값") if goal else None,
            "total": total,
            "answered": answered,
            "unanswered": total - answered,
            "median_minutes": round(stat["median_minutes"]) if stat else 0,
            "worst_minutes": round(stat["worst_minutes"]) if stat else 0,
            # 목표가 0 이면 집계하지 않는다. '목표 없음' 을 '항상 달성' 으로
            # 보여주면 지표가 거짓말을 한다.
            "tracked": bool(minutes),
            "met": bool(minutes) and stat is not None
                   and stat["median_minutes"] <= minutes,
        })

    return {
        "customer": customer,
        "days": days,
        "accounts": account_ids,
        "by_severity": rows,
        "total": sum(r["total"] for r in rows),
        "measured": sum(r["answered"] for r in rows),
        "unattributed": unattributed,
    }
