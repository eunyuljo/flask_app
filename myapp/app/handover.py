# app/handover.py
# 당직 인계에 들어갈 내용을 모은다.
#
# 리포트(app/report.py)와 목적이 다르다.
#   리포트  : 고객사에 내는 것. 월 단위. 기간 대비 변화가 핵심.
#   인계    : 다음 당직자에게 넘기는 것. 시간 단위. "지금 뭐가 열려 있나" 가 핵심.
# 그래서 집계 방식도 다르다 - 인계는 '처음 발생' 과 '미해결' 을 앞세운다.

from datetime import datetime, timezone

from flask import current_app

# 심각도를 '나쁜 순' 으로 세우는 SQL 조각.
#
# max(severity) 를 쓰면 안 된다. 문자열 비교라 알파벳 순으로 가장 큰 값,
# 즉 warning 이 이긴다(critical < error < info < warning).
# critical 과 warning 이 섞인 묶음이 'warning' 으로 보고되어
# 가장 심각한 것을 놓치게 된다.
SEVERITY_RANK = "CASE severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 WHEN 'warning' THEN 3 ELSE 4 END"
SEVERITY_RANK_E = "CASE e.severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 WHEN 'warning' THEN 3 ELSE 4 END"


class HandoverError(Exception):
    """인계 자료를 모으지 못했을 때."""


def psycopg_uri():
    return current_app.config["DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def collect(hours=12):
    """지난 N시간의 인계 자료를 한 번의 접속으로 모은다."""
    try:
        import psycopg
    except ImportError as e:
        raise HandoverError("psycopg 가 설치되어 있지 않습니다.") from e

    try:
        with psycopg.connect(psycopg_uri()) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.events')")
            if cur.fetchone()[0] is None:
                raise HandoverError(
                    "events 테이블이 없습니다. flask --app run init-db 를 실행하세요."
                )

            # ---- 심각도별 건수 ----
            cur.execute(
                """
                SELECT severity, count(*) FROM events
                 WHERE occurred_at >= now() - make_interval(hours => %s)
                 GROUP BY severity
                """,
                (hours,),
            )
            by_severity = dict(cur.fetchall())

            # ---- 많이 난 알람 (지문별) ----
            # 지문으로 묶는다. 메시지로 묶으면 측정값이 달라서 전부 1건씩 나온다.
            cur.execute(
                """
                SELECT fingerprint,
                       count(*) AS c,
                       -- 대표 메시지는 가장 최근 것. min(message) 로 뽑으면
                       -- 알파벳 순으로 아무거나 하나가 걸린다.
                       (array_agg(message ORDER BY occurred_at DESC))[1] AS sample,
                       (array_agg(severity ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 WHEN 'warning' THEN 3 ELSE 4 END))[1] AS severity,
                       (array_agg(source ORDER BY occurred_at DESC))[1]  AS source,
                       max(occurred_at) AS last_seen
                  FROM events
                 WHERE occurred_at >= now() - make_interval(hours => %s)
                 GROUP BY fingerprint
                 ORDER BY c DESC, last_seen DESC
                 LIMIT 10
                """,
                (hours,),
            )
            top = _rows(cur)

            # ---- 이번 근무에 '처음 나타난' 알람 ----
            # 전체 이력의 첫 발생이 이 구간 안에 있는 지문.
            # 반복되던 알람보다 이쪽이 훨씬 중요하다. 새로 생긴 문제이기 때문이다.
            cur.execute(
                """
                SELECT fingerprint, first_seen, c, sample, severity, source
                  FROM (
                    SELECT fingerprint,
                           min(occurred_at) AS first_seen,
                           count(*)         AS c,
                           -- 처음 나타난 알람이므로 대표 메시지는 첫 번째 것.
                           (array_agg(message ORDER BY occurred_at))[1] AS sample,
                           (array_agg(severity ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 WHEN 'warning' THEN 3 ELSE 4 END))[1] AS severity,
                           (array_agg(source ORDER BY occurred_at))[1]  AS source
                      FROM events
                     GROUP BY fingerprint
                  ) t
                 WHERE first_seen >= now() - make_interval(hours => %s)
                 ORDER BY first_seen DESC
                 LIMIT 10
                """,
                (hours,),
            )
            new_kinds = _rows(cur)

            # ---- 미해결 작업 ----
            # 작업 기록 테이블이 없을 수도 있다(구버전 스키마). 그때는 건너뛴다.
            open_work = []
            cur.execute("SELECT to_regclass('public.work_orders')")
            if cur.fetchone()[0] is not None:
                cur.execute(
                    """
                    SELECT id, ticket, title, customer, account_id, region,
                           operator, status, created_at
                      FROM work_orders
                     WHERE status <> 'closed'
                     ORDER BY created_at
                    """
                )
                open_work = _rows(cur)

            # ---- 절차가 없는 알람 ----
            # 이번 구간에 났는데 런북이 안 붙은 지문. 인계받는 사람이
            # "이건 뭘 봐야 하나" 를 물어보게 되는 항목이다.
            no_runbook = []
            cur.execute("SELECT to_regclass('public.runbooks')")
            if cur.fetchone()[0] is not None:
                cur.execute(
                    """
                    SELECT e.fingerprint, count(*) AS c,
                           (array_agg(e.message ORDER BY e.occurred_at DESC))[1] AS sample,
                           (array_agg(e.severity ORDER BY CASE e.severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 WHEN 'warning' THEN 3 ELSE 4 END))[1] AS severity
                      FROM events e
                     WHERE e.occurred_at >= now() - make_interval(hours => %s)
                       AND e.severity IN ('critical', 'error')
                       AND NOT EXISTS (
                             SELECT 1 FROM runbooks r WHERE r.fingerprint = e.fingerprint
                           )
                     GROUP BY e.fingerprint
                     ORDER BY c DESC
                     LIMIT 10
                    """,
                    (hours,),
                )
                no_runbook = _rows(cur)

    except HandoverError:
        raise
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise HandoverError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise

    total = sum(by_severity.values())
    return {
        "hours": hours,
        "generated_at": datetime.now(timezone.utc),
        "total": total,
        "by_severity": by_severity,
        "alarm_count": by_severity.get("critical", 0) + by_severity.get("error", 0),
        "top": top,
        "new_kinds": new_kinds,
        "open_work": open_work,
        "no_runbook": no_runbook,
    }


# 상태 코드를 인계 문서에 쓸 한국어로.
WORK_LABEL = {
    "open": "작업 전 스냅샷 대기",
    "before_taken": "작업 진행 중",
    "after_taken": "증적 확정 대기",
}


def to_markdown(data):
    """인계 내용을 Markdown 으로. 그대로 메신저에 붙여넣을 수 있는 길이로 유지한다."""
    L = []
    a = L.append

    a(f"# 당직 인계 — 지난 {data['hours']}시간")
    a("")
    a(f"작성 {data['generated_at']:%Y-%m-%d %H:%M} UTC")
    a("")

    sev = data["by_severity"]
    a(f"이벤트 {data['total']}건 "
      f"(critical {sev.get('critical', 0)} / error {sev.get('error', 0)} / "
      f"warning {sev.get('warning', 0)} / info {sev.get('info', 0)})")
    a("")

    a("## 이번 근무에 처음 나타난 알람")
    a("")
    if not data["new_kinds"]:
        a("없습니다. 이번 구간의 알람은 모두 이전에도 있던 종류입니다.")
    else:
        a("| 심각도 | 건수 | 출처 | 알람 |")
        a("|---|---|---|---|")
        for k in data["new_kinds"]:
            a(f"| {k['severity']} | {k['c']} | {k['source']} | {k['sample']} |")
    a("")

    a("## 많이 난 알람")
    a("")
    if not data["top"]:
        a("이번 구간에 이벤트가 없습니다.")
    else:
        a("| 건수 | 심각도 | 출처 | 알람 | 마지막 |")
        a("|---|---|---|---|---|")
        for t in data["top"]:
            a(f"| {t['c']} | {t['severity']} | {t['source']} | {t['sample']} "
              f"| {t['last_seen']:%m-%d %H:%M} |")
    a("")

    a("## 진행 중인 작업")
    a("")
    if not data["open_work"]:
        a("확정되지 않은 작업이 없습니다.")
    else:
        a("| # | 티켓 | 작업 | 고객사 | 상태 | 작업자 |")
        a("|---|---|---|---|---|---|")
        for w in data["open_work"]:
            a(f"| {w['id']} | {w['ticket'] or '-'} | {w['title']} | {w['customer']} "
              f"| {WORK_LABEL.get(w['status'], w['status'])} | {w['operator']} |")
    a("")

    if data["no_runbook"]:
        a("## 대응 절차가 없는 알람")
        a("")
        a("아래는 이번 구간에 발생한 critical/error 중 런북이 등록되지 않은 것들입니다.")
        a("인계받는 사람이 판단 근거 없이 마주치게 되므로, 절차를 남겨두면 좋습니다.")
        a("")
        a("| 건수 | 심각도 | 알람 |")
        a("|---|---|---|")
        for n in data["no_runbook"]:
            a(f"| {n['c']} | {n['severity']} | {n['sample']} |")
        a("")

    return "\n".join(L)


def to_slack(data, base_url=""):
    """인계 내용을 Slack mrkdwn 으로 만든다.

    to_markdown 을 재사용하지 않는 이유: Slack mrkdwn 은 표를 렌더링하지
    못한다. Markdown 표를 그대로 보내면 파이프 문자가 잔뜩 찍힌 글덩어리가
    된다. 같은 내용을 목록으로 다시 쓴다.

    Slack 문법도 다르다. **굵게** 가 아니라 *굵게*, [링크](url) 이 아니라
    <url|링크> 다.
    """
    from app.slack import escape

    L = []
    a = L.append
    sev = data["by_severity"]

    a(f"*당직 인계 — 지난 {data['hours']}시간*")
    a(f"_{data['generated_at']:%Y-%m-%d %H:%M} UTC_")
    a("")
    a(f"이벤트 {data['total']}건 "
      f"(critical {sev.get('critical', 0)} / error {sev.get('error', 0)} / "
      f"warning {sev.get('warning', 0)} / info {sev.get('info', 0)})")

    # 처음 나타난 알람을 맨 위에 둔다. 반복되던 것보다 중요하다.
    a("")
    a("*이번 근무에 처음 나타난 알람*")
    if not data["new_kinds"]:
        a("• 없음 — 모두 이전에도 있던 종류입니다")
    else:
        for k in data["new_kinds"][:8]:
            a(f"• `{k['severity']}` {escape(k['sample'])} "
              f"— {escape(k['source'])}, {k['c']}건")
        if len(data["new_kinds"]) > 8:
            a(f"• … 외 {len(data['new_kinds']) - 8}종")

    a("")
    a("*많이 난 알람*")
    if not data["top"]:
        a("• 이 구간에 이벤트가 없습니다")
    else:
        for t in data["top"][:5]:
            a(f"• {t['c']}건 `{t['severity']}` {escape(t['sample'])} "
              f"— {escape(t['source'])}")

    a("")
    a("*진행 중인 작업*")
    if not data["open_work"]:
        a("• 확정되지 않은 작업이 없습니다")
    else:
        for w in data["open_work"]:
            ticket = f"{escape(w['ticket'])} · " if w["ticket"] else ""
            line = (f"• #{w['id']} {escape(w['title'])} — {ticket}"
                    f"{escape(w['customer'])} · {WORK_LABEL.get(w['status'], w['status'])} "
                    f"· {escape(w['operator'])}")
            if base_url:
                line += f"  <{base_url.rstrip('/')}/work/{w['id']}|열기>"
            a(line)

    if data["no_runbook"]:
        a("")
        a("*대응 절차가 없는 알람* — 인계받는 사람이 판단 근거 없이 마주칩니다")
        for n in data["no_runbook"][:5]:
            a(f"• {n['c']}건 `{n['severity']}` {escape(n['sample'])}")
        if len(data["no_runbook"]) > 5:
            a(f"• … 외 {len(data['no_runbook']) - 5}종")

    if base_url:
        a("")
        a(f"<{base_url.rstrip('/')}/handover/?hours={data['hours']}|전체 보기>")

    return "\n".join(L)
