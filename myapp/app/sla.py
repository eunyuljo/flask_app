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

def measure(customer, days=30, start=None, end=None):
    """이 고객사의 최초 대응 시간을 잰다.

    알람 하나마다: 발생 시각 이후 그 계정에 대한 첫 감사 기록까지의 분.
    감사 기록이 없으면 '미대응' 으로 센다.

    기본은 '지금부터 거슬러 days 일'. start/end 를 주면 그 구간만 잰다.
    월간 서비스 리뷰처럼 '8월' 을 집계할 때는 '지난 30일' 이 8월과
    맞지 않아서 구간을 직접 받아야 한다.
    """
    # 구간을 받았으면 그쪽을 쓴다. 두 갈래를 만들지 않으려고 아래 질의는
    # 항상 start/end 로 쓰고, 안 받았을 때만 여기서 만들어 넣는다.
    if start is None or end is None:
        from datetime import datetime, timedelta, timezone

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "audit_log")
        _ensure(cur, "sla_targets")

        cur.execute(
            "SELECT account_id FROM aws_accounts WHERE customer = %s", (customer,)
        )
        account_ids = [r[0] for r in cur.fetchall()]
        if not account_ids:
            return {
                "customer": customer, "days": days, "start": start, "end": end,
                "accounts": [], "by_severity": [], "total": 0,
                "measured": 0, "unattributed": 0,
            }

        # 알람마다 '그 계정을 그 시각 이후 처음 들여다본 때' 를 붙인다.
        #
        # LATERAL 을 쓰는 이유: 알람 행마다 audit_log 를 따로 뒤져야 한다.
        # 그냥 조인하면 알람 × 감사기록 만큼 행이 부풀고, 그 뒤에 min() 을
        # 걸어도 이벤트가 많아지면 감당하지 못한다.
        # 대응 시각을 두 곳에서 찾는다.
        #
        #   1) 확인 버튼 (events.acknowledged_at)  - 사실
        #   2) 감사 로그의 첫 기록                  - 추론
        #
        # 1번이 있으면 그걸 쓰고, 없을 때만 2번으로 떨어진다. 확인 버튼이
        # 생기기 전 기록을 전부 '미대응' 으로 만들지 않기 위해서다.
        #
        # 대신 어느 쪽에서 나온 값인지를 함께 센다(acked / inferred).
        # 추론 비중이 크면 이 지표를 그만큼 덜 믿어야 하고, 확인 버튼이
        # 자리를 잡을수록 그 수가 줄어드는 것이 보여야 한다.
        cur.execute(
            """
            SELECT e.severity,
                   count(*)                                   AS total,
                   count(COALESCE(e.acknowledged_at, a.at))   AS answered,
                   count(e.acknowledged_at)                   AS acked,
                   count(a.at) FILTER (WHERE e.acknowledged_at IS NULL)
                                                              AS inferred,
                   COALESCE(percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM
                           (COALESCE(e.acknowledged_at, a.at) - e.occurred_at)) / 60
                   ), 0)                                      AS median_minutes,
                   COALESCE(max(EXTRACT(EPOCH FROM
                       (COALESCE(e.acknowledged_at, a.at) - e.occurred_at)) / 60), 0)
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
               AND e.occurred_at >= %s AND e.occurred_at < %s
             GROUP BY e.severity
            """,
            (account_ids, start, end),
        )
        measured = {r["severity"]: r for r in _rows(cur)}

        # 계정을 모르는 이벤트. 이 수가 크면 위 숫자가 전체를 대표하지 못한다.
        cur.execute(
            "SELECT count(*) FROM events WHERE account_id = '' "
            "AND occurred_at >= %s AND occurred_at < %s",
            (start, end),
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
            # 이 등급의 대응 시각이 어디서 나왔나.
            "acked": stat["acked"] if stat else 0,
            "inferred": stat["inferred"] if stat else 0,
            # 목표가 0 이면 집계하지 않는다. '목표 없음' 을 '항상 달성' 으로
            # 보여주면 지표가 거짓말을 한다.
            "tracked": bool(minutes),
            "met": bool(minutes) and stat is not None
                   and stat["median_minutes"] <= minutes,
        })

    return {
        "customer": customer,
        "days": days,
        "start": start,
        "end": end,
        "accounts": account_ids,
        "by_severity": rows,
        "total": sum(r["total"] for r in rows),
        "measured": sum(r["answered"] for r in rows),
        # 확인 버튼으로 잰 것과 감사 로그로 추론한 것의 비율.
        # 추론이 많으면 이 지표를 그만큼 덜 믿어야 한다.
        "acked": sum(r["acked"] for r in rows),
        "inferred": sum(r["inferred"] for r in rows),
        "unattributed": unattributed,
    }


# ----------------------------------------------------------------------
# 위반 감지 (알림용)
# ----------------------------------------------------------------------
# measure() 는 지나간 기간을 집계한다. 여기는 "지금 목표를 넘겼는데 아직
# 아무도 안 본 알람" 을 찾는다. 목적이 달라서 질의도 다르다.

def breaches(lookback_hours=24):
    """지금 목표를 넘겼는데 대응 기록이 없는 알람을 찾는다.

    (계정, 지문) 으로 묶는다. 같은 알람이 20건이면 20줄이 아니라 1줄이다.
    """
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "audit_log")
        _ensure(cur, "sla_targets")

        # 목표가 있는 심각도만 본다. 목표 0(=목표 없음)은 위반이 성립하지 않는다.
        #
        # 계정 -> 고객사 -> 목표 순으로 이어붙인다. 고객사 전용 목표가
        # 있으면 그것을, 없으면 기본값(customer='')을 쓴다.
        cur.execute(
            """
            WITH goal AS (
                SELECT DISTINCT ON (a.account_id, t.severity)
                       a.account_id, a.customer, t.severity,
                       t.first_response_minutes AS minutes
                  FROM aws_accounts a
                  JOIN sla_targets t ON t.customer IN (a.customer, '')
                 WHERE t.first_response_minutes > 0
                 ORDER BY a.account_id, t.severity, (t.customer = '') ASC
            )
            SELECT e.account_id, g.customer, e.severity, e.fingerprint,
                   g.minutes,
                   count(*)                       AS count,
                   min(e.occurred_at)             AS first_seen,
                   (array_agg(e.message ORDER BY e.occurred_at DESC))[1] AS sample,
                   (array_agg(e.source  ORDER BY e.occurred_at DESC))[1] AS source,
                   round(EXTRACT(EPOCH FROM (now() - min(e.occurred_at))) / 60)
                                                  AS elapsed_minutes
              FROM events e
              JOIN goal g
                ON g.account_id = e.account_id AND g.severity = e.severity
             WHERE e.occurred_at >= now() - make_interval(hours => %s)
               -- 목표 시간이 이미 지난 것만
               AND e.occurred_at < now() - make_interval(mins => g.minutes)
               -- 그 계정을 그 시각 이후 들여다본 기록이 없는 것만
               AND NOT EXISTS (
                     SELECT 1 FROM audit_log al
                      WHERE al.account_id = e.account_id
                        AND al.at >= e.occurred_at
                   )
             GROUP BY e.account_id, g.customer, e.severity, e.fingerprint, g.minutes
             ORDER BY g.minutes ASC, count DESC
            """,
            (lookback_hours,),
        )
        return _rows(cur)


def unnotified(items, window_minutes):
    """아직 알리지 않은 것만 골라낸다.

    window_minutes 안에 이미 보낸 (계정, 지문) 은 건너뛴다.
    이 걸러내기가 없으면 주기 실행마다 같은 위반을 다시 보낸다.
    """
    if not items:
        return []

    # (a, b) = ANY(%s) 로는 안 된다. psycopg 가 튜플 목록을 익명 복합 타입으로
    # 넘기는데 PostgreSQL 이 그 입력을 지원하지 않는다
    # ("input of anonymous composite types is not implemented").
    # 배열 두 개를 unnest 로 짝지어 넘긴다.
    accounts = [i["account_id"] for i in items]
    fingerprints = [i["fingerprint"] for i in items]

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "sla_notices")
        cur.execute(
            """
            SELECT n.account_id, n.fingerprint
              FROM sla_notices n
              JOIN unnest(%s::text[], %s::text[]) AS k(account_id, fingerprint)
                ON k.account_id = n.account_id AND k.fingerprint = n.fingerprint
             WHERE n.last_notified_at > now() - make_interval(mins => %s)
            """,
            (accounts, fingerprints, window_minutes),
        )
        recent_keys = {(r[0], r[1]) for r in cur.fetchall()}

    return [i for i in items
            if (i["account_id"], i["fingerprint"]) not in recent_keys]


def mark_notified(items):
    """알린 것을 기록한다."""
    if not items:
        return
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "sla_notices")
        cur.executemany(
            """
            INSERT INTO sla_notices (account_id, fingerprint, last_notified_at)
            VALUES (%s, %s, now())
            ON CONFLICT (account_id, fingerprint) DO UPDATE
                SET last_notified_at = now(),
                    notice_count = sla_notices.notice_count + 1
            """,
            [(i["account_id"], i["fingerprint"]) for i in items],
        )


def to_slack(items):
    """위반 목록을 Slack mrkdwn 으로 만든다."""
    from app.slack import escape

    L = []
    a = L.append
    a(f"*SLA 목표 초과 — 아직 대응 기록이 없는 알람 {len(items)}종*")
    a("")
    for i in items:
        a(f"• `{i['severity']}` *{escape(i['customer'])}* "
          f"{escape(i['sample'])}")
        a(f"    {i['count']}건 · {escape(i['source'])} · 계정 {i['account_id']} · "
          f"목표 {i['minutes']}분 / 경과 *{int(i['elapsed_minutes'])}분*")
    a("")
    a("_대응 여부는 감사 로그(콘솔 조회·AI 진단) 기준입니다. "
      "다른 경로로 대응했다면 여기 잡히지 않습니다._")
    return "\n".join(L)
