# app/msr.py
# 월간 서비스 리뷰(MSR) 자료.
#
# ── 새로 만드는 게 거의 없다 ────────────────────────────────────────
# 여기 들어가는 숫자는 전부 이미 다른 화면에 있다. SLA 는 리포트에,
# 장애는 사후 보고서에, 작업은 작업 기록에, 위반은 컴플라이언스에.
#
# 없던 것은 "고객사 하나를 놓고 한 달치를 한 권으로 묶은 것" 이다.
# 지금은 고객사와 미팅하려면 화면 여섯 개를 돌면서 옮겨 적어야 한다.
#
# ── 왜 달(month) 단위인가 ───────────────────────────────────────────
# 리포트(app/report.py)는 '지난 7일' 처럼 지금부터 거슬러 센다. 운영자가
# 보기에는 그게 맞다. 고객사 리뷰는 다르다 - "8월 보고" 는 8월 1일부터
# 8월 31일까지지, 오늘부터 30일 전까지가 아니다. 지난달 것을 다시 뽑으면
# 값이 달라지는 보고서는 보고서가 아니다.

import calendar
from datetime import datetime, timezone

from flask import current_app

from app import sla

SEVERITIES = ("critical", "error", "warning", "info")


class MsrError(Exception):
    """자료를 모으지 못했을 때."""


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
        raise MsrError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise MsrError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _has(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


def month_bounds(year, month):
    """그 달의 시작과 끝(다음 달 1일). 둘 다 UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def previous_month(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def available_months(limit=12):
    """이벤트가 있는 달 목록. 자료가 없는 달을 고르게 두면 빈 보고서가 나온다."""
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "events"):
            raise MsrError("events 테이블이 없습니다.")
        cur.execute(
            """
            SELECT DISTINCT date_part('year', occurred_at)::int  AS year,
                            date_part('month', occurred_at)::int AS month
              FROM events
             ORDER BY year DESC, month DESC
             LIMIT %s
            """,
            (limit,),
        )
        return _rows(cur)


def _pct_change(current, previous):
    """전월 대비 변화율. 지난달이 0이면 비율을 만들지 않는다.

    0 에서 5 로 늘어난 것을 '500% 증가' 라고 쓰면 숫자가 커 보이기만 하고
    아무 뜻이 없다.
    """
    if not previous:
        return None
    return round((current - previous) / previous * 100)


def collect(customer, year, month):
    """고객사 하나의 한 달치를 모은다."""
    start, end = month_bounds(year, month)
    prev_year, prev_month = previous_month(year, month)
    prev_start, prev_end = month_bounds(prev_year, prev_month)

    data = collect_window(
        customer, start, end, prev_start, prev_end,
        label=f"{year}년 {month}월",
        days=calendar.monthrange(year, month)[1],
    )
    # 달력 기준 화면(월간 리뷰)이 쓰는 값. 기간 일반 수집에는 뜻이 없다.
    data["year"] = year
    data["month"] = month
    return data


def collect_week(customer, end=None, days=7):
    """고객사 하나의 최근 한 주치.

    월간 리뷰와 같은 수집기를 쓴다. 주간과 월간의 숫자가 다른 함수에서
    나오면 언젠가 갈라지고, 그때 고객사는 두 보고서를 나란히 놓고 본다.
    """
    from datetime import datetime, timedelta, timezone

    end = end or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return collect_window(
        customer, start, end,
        # 직전 같은 길이의 기간. 절대 건수보다 변화가 신뢰할 만하다
        # (app/report.py 가 같은 이유로 같은 방식을 쓴다).
        start - timedelta(days=days), start,
        label=f"{start:%m-%d} ~ {end:%m-%d}",
        days=days,
    )


def collect_window(customer, start, end, prev_start, prev_end, label, days):
    """고객사 하나의 한 기간치.

    원래 collect() 안에 달 계산과 함께 붙어 있었다. 주간 리포트를 붙이면서
    기간만 다른 같은 집계가 필요해졌는데, 복사해 두면 주간과 월간의 숫자가
    언젠가 갈라진다. 창(window)만 받는 함수로 내렸다.
    """
    data = {
        "customer": customer,
        "start": start,
        "end": end,
        "label": label,
        "days": days,
        "accounts": [],
        "alarms": {"total": 0, "by_severity": {s: 0 for s in SEVERITIES},
                   "prev_total": 0, "change": None},
        "top_alarms": [],
        "incidents": [],
        "works": [],
        "compliance": None,
        "notes": [],
    }

    with _connect() as conn, conn.cursor() as cur:
        # 테이블 존재 확인을 먼저 전부 끝낸다. 같은 커서로 중간에 끼우면
        # 앞선 질의 결과가 덮어써진다.
        present = {
            name: _has(cur, name)
            for name in ("aws_accounts", "events", "incidents",
                         "work_orders", "runbooks")
        }
        if not present["aws_accounts"]:
            raise MsrError("aws_accounts 테이블이 없습니다.")

        cur.execute(
            "SELECT account_id, alias, regions FROM aws_accounts "
            "WHERE customer = %s ORDER BY account_id",
            (customer,),
        )
        data["accounts"] = _rows(cur)
        account_ids = [a["account_id"] for a in data["accounts"]]

        if not account_ids:
            data["notes"].append(
                "등록된 계정이 없어 알람·SLA 를 집계하지 못했습니다."
            )

        if present["events"] and account_ids:
            cur.execute(
                "SELECT severity, count(*) AS n FROM events "
                " WHERE account_id = ANY(%s) "
                "   AND occurred_at >= %s AND occurred_at < %s "
                " GROUP BY severity",
                (account_ids, start, end),
            )
            for row in _rows(cur):
                data["alarms"]["by_severity"][row["severity"]] = row["n"]
            data["alarms"]["total"] = sum(data["alarms"]["by_severity"].values())

            cur.execute(
                "SELECT count(*) FROM events WHERE account_id = ANY(%s) "
                "  AND occurred_at >= %s AND occurred_at < %s",
                (account_ids, prev_start, prev_end),
            )
            data["alarms"]["prev_total"] = cur.fetchone()[0]
            data["alarms"]["change"] = _pct_change(
                data["alarms"]["total"], data["alarms"]["prev_total"]
            )

            # 가장 잦은 알람. 런북이 붙어 있는지도 같이 본다 - 잦은데
            # 절차가 없으면 다음 달에 할 일이 바로 그것이다.
            if present["runbooks"]:
                cur.execute(
                    """
                    SELECT e.fingerprint,
                           count(*) AS times,
                           (array_agg(e.message ORDER BY e.occurred_at DESC))[1] AS sample,
                           (array_agg(e.severity ORDER BY CASE e.severity
                                WHEN 'critical' THEN 1 WHEN 'error' THEN 2
                                WHEN 'warning'  THEN 3 ELSE 4 END))[1] AS severity,
                           EXISTS (SELECT 1 FROM runbooks r
                                    WHERE r.fingerprint = e.fingerprint
                                      AND r.customer IN (%s, '')) AS has_runbook
                      FROM events e
                     WHERE e.account_id = ANY(%s)
                       AND e.fingerprint <> ''
                       AND e.occurred_at >= %s AND e.occurred_at < %s
                     GROUP BY e.fingerprint
                     ORDER BY times DESC
                     LIMIT 8
                    """,
                    (customer, account_ids, start, end),
                )
                data["top_alarms"] = _rows(cur)

        if present["incidents"]:
            cur.execute(
                """
                SELECT id, title, severity, started_at, ended_at,
                       status, customer_status
                  FROM incidents
                 WHERE customer = %s AND started_at >= %s AND started_at < %s
                 ORDER BY started_at
                """,
                (customer, start, end),
            )
            data["incidents"] = _rows(cur)

        if present["work_orders"]:
            cur.execute(
                """
                SELECT id, ticket, title, account_id, region, operator,
                       status, created_at, closed_at
                  FROM work_orders
                 WHERE customer = %s AND created_at >= %s AND created_at < %s
                 ORDER BY created_at
                """,
                (customer, start, end),
            )
            data["works"] = _rows(cur)

    # SLA 는 이미 있는 함수를 쓴다. 같은 계산을 두 벌 두면 리포트 화면과
    # 고객사 보고서의 숫자가 언젠가 갈라진다.
    try:
        data["sla"] = sla.measure(customer, start=start, end=end)
    except Exception as e:
        data["sla"] = None
        data["notes"].append(f"SLA 를 집계하지 못했습니다: {e}")

    data["compliance"] = _compliance_now(customer, account_ids, data["notes"])
    data["actions"] = suggest(data)
    return data


def _compliance_now(customer, account_ids, notes):
    """컴플라이언스는 '지금' 상태를 붙인다.

    지난달 말 시점으로 다시 계산할 수도 있지만(스냅샷이 있으니 가능하다),
    고객사가 보고서를 받아 드는 시점에 알고 싶은 것은 "지금 남아 있는
    위반" 이다. 지난달 말에 몇 개였는지는 고쳐야 할 일을 알려주지 않는다.
    """
    from app import compliance
    from app.compliance import ComplianceError

    if not account_ids:
        return None
    try:
        targets = [
            t for t in compliance.latest_snapshots()
            if t["account_id"] in account_ids
        ]
    except ComplianceError as e:
        notes.append(f"컴플라이언스를 집계하지 못했습니다: {e}")
        return None

    if not targets:
        notes.append("리소스 스냅샷이 없어 컴플라이언스를 집계하지 못했습니다.")
        return None

    totals = {s: 0 for s in compliance.SEVERITIES}
    worst_items, excused = [], 0
    skipped = {}
    for t in targets:
        try:
            violations, snap = compliance.evaluate(t["snapshot_id"], t["account_id"])
        except ComplianceError:
            continue
        summary = compliance.summarize(violations, snap)
        for sev in compliance.SEVERITIES:
            totals[sev] += summary["by_severity"][sev]
        excused += summary["excused"]
        # 못 돌린 점검은 계정별로 다를 수 있다. 항목 이름으로 모은다.
        for c in compliance.coverage(snap)["skipped"]:
            skipped[c["title"]] = skipped.get(c["title"], 0) + 1
        worst_items += [
            v for v in violations
            if not v["excused"] and v["severity"] in ("critical", "high")
        ]

    worst_items.sort(key=lambda v: compliance.SEVERITY_ORDER[v["severity"]])
    if skipped:
        notes.append(
            "수집하지 않아 돌리지 못한 점검이 있습니다: "
            + ", ".join(sorted(skipped))
            + ". 위반이 없는 것이 아니라 보지 않은 것입니다."
        )

    return {
        "by_severity": totals,
        "total": sum(totals.values()),
        "excused": excused,
        "worst": worst_items[:8],
        "skipped": sorted(skipped),
        "checked_at": max(t["collected_at"] for t in targets),
    }


def suggest(data):
    """다음 달 제안.

    자동으로 만든 초안이다. 고객사에 그대로 내보내는 문장이 아니라,
    담당자가 미팅 전에 손볼 재료다. 빈 칸을 앞에 두면 결국 아무도 안 쓴다.
    """
    out = []

    comp = data.get("compliance")
    if comp and comp["by_severity"].get("critical"):
        out.append({
            "kind": "보안",
            "text": f"컴플라이언스 critical 위반 {comp['by_severity']['critical']}건 조치",
            "why": "인터넷에 열린 관리 포트·퍼블릭 버킷 등 즉시 악용 가능한 항목입니다.",
        })

    gaps = [a for a in data.get("top_alarms", []) if not a["has_runbook"]]
    if gaps:
        worst = gaps[0]
        out.append({
            "kind": "운영",
            "text": f"자주 나는 알람 {len(gaps)}종에 대응 절차 작성",
            "why": f"가장 잦은 것은 이번 달 {worst['times']}회 발생했는데 절차가 없습니다.",
        })

    slad = data.get("sla")
    if slad:
        missed = [r for r in slad["by_severity"] if r["tracked"] and not r["met"]]
        if missed:
            names = ", ".join(r["severity"] for r in missed)
            out.append({
                "kind": "SLA",
                "text": f"최초 대응 목표 미달 등급 개선: {names}",
                "why": "중앙값이 목표를 넘었습니다. 알람 인입 경로나 당직 배치를 봐야 합니다.",
            })
        unanswered = sum(r["unanswered"] for r in slad["by_severity"])
        if unanswered:
            out.append({
                "kind": "SLA",
                "text": f"대응 기록이 없는 알람 {unanswered}건 확인",
                "why": "실제로 대응했는데 기록이 없으면 지표가 실제보다 나쁘게 나옵니다.",
            })

    unsent = [i for i in data.get("incidents", []) if i["customer_status"] != "sent"]
    if unsent:
        out.append({
            "kind": "보고",
            "text": f"고객 제출본이 아직 안 나간 장애 {len(unsent)}건",
            "why": "제출이 밀리면 리뷰 자리에서 먼저 질문을 받게 됩니다.",
        })

    open_works = [w for w in data.get("works", []) if w["status"] != "closed"]
    if open_works:
        out.append({
            "kind": "작업",
            "text": f"증적이 확정되지 않은 작업 {len(open_works)}건 마무리",
            "why": "작업 전/후 스냅샷이 확정되지 않으면 나중에 근거로 쓸 수 없습니다.",
        })

    if not out:
        out.append({
            "kind": "-",
            "text": "이번 달에 자동으로 잡힌 후속 조치가 없습니다.",
            "why": "담당자가 직접 채워 넣어야 하는 항목이 있는지 확인하세요.",
        })
    return out


# ----------------------------------------------------------------------
# 주간 리포트
# ----------------------------------------------------------------------

def week_key(end):
    """이 주를 가리키는 이름. 발송 기록의 ref 로 쓴다.

    ISO 주 번호를 쓴다(2026-W34). 같은 주에 두 번 보내는 것을 막으려면
    '이 주' 를 흔들리지 않게 부를 이름이 필요하고, 날짜 범위 문자열은
    한 시간만 달라져도 다른 값이 된다.
    """
    year, week, _ = end.isocalendar()
    return f"{year}-W{week:02d}"


def to_slack_week(data, base_url=""):
    """주간 리포트를 Slack mrkdwn 으로 만든다.

    표를 쓰지 않는다. Slack mrkdwn 은 표를 렌더링하지 못해서 파이프가
    잔뜩 찍힌 글덩어리가 된다(handover.to_slack 에서 겪은 것과 같다).
    """
    from app.slack import escape

    L = []
    a = L.append
    alarms = data["alarms"]

    a(f"*{escape(data['customer'])} 주간 리포트 — {data['label']}*")
    a(f"_{data['start']:%Y-%m-%d} ~ {data['end']:%Y-%m-%d} UTC_")
    a("")

    change = alarms.get("change")
    trend = ""
    if change is not None:
        trend = f" (직전 주 {alarms['prev_total']}건 대비 {change:+d}%)"
    by = alarms["by_severity"]
    a(f"알람 {alarms['total']}건{trend}")
    a(f"  critical {by.get('critical', 0)} / error {by.get('error', 0)} / "
      f"warning {by.get('warning', 0)} / info {by.get('info', 0)}")

    if data.get("sla"):
        s = data["sla"]
        # 달성 여부는 심각도마다 따로 판정된다(sla.measure 의 by_severity).
        # 위쪽에 met 같은 한 개짜리 값은 없다 - 목표가 심각도별로 다르니
        # 하나로 합치면 그 숫자가 무엇을 뜻하는지 아무도 모른다.
        tracked = [row for row in (s.get("by_severity") or []) if row["tracked"]]
        if tracked:
            a("")
            a("*SLA (최초 대응)*")
            for row in tracked:
                # 대상이 0건이면 '미달' 이 아니라 '해당 없음' 이다.
                # 알람이 안 난 주를 미달로 적어 보내면 그건 거짓말이고,
                # SLA 화면은 이미 둘을 구분하고 있다(report_sla.html).
                if not row["total"]:
                    a(f"  • {row['severity']} 목표 {row['target_minutes']}분 — "
                      f"해당 없음 (이 기간에 대상 알람 없음)")
                    continue
                mark = "달성" if row["met"] else "미달"
                a(f"  • {row['severity']} 목표 {row['target_minutes']}분 — "
                  f"{mark} (대상 {row['total']}건, 응답 {row['answered']}건, "
                  f"중앙값 {row['median_minutes']}분)")
            # 이 숫자가 무엇에서 나왔는지 함께 적는다. 확인(ack) 기록과
            # 감사 로그 추론이 섞여 있는데, 밝히지 않으면 계약 이행
            # 증거처럼 읽힌다.
            if s.get("inferred"):
                a(f"  _{s['inferred']}건은 알람 확인 기록이 아니라 "
                  f"계정 조회 기록으로 추정한 값입니다_")

    if data.get("incidents"):
        a("")
        a(f"*장애 {len(data['incidents'])}건*")
        for item in data["incidents"][:5]:
            a(f"  • [{item['severity']}] {escape(item['title'])}")

    if data.get("works"):
        a("")
        a(f"*작업 {len(data['works'])}건*")
        for item in data["works"][:5]:
            a(f"  • {escape(item['title'])} ({item['status']})")

    # 절차 없는 잦은 알람. 다음 주에 할 일이 대개 이것이다.
    gaps = [t for t in (data.get("top_alarms") or []) if not t.get("has_runbook")]
    if gaps:
        a("")
        a("*절차가 없는 잦은 알람*")
        for item in gaps[:3]:
            a(f"  • {item['times']}회 — {escape((item['sample'] or '')[:80])}")

    if data.get("notes"):
        a("")
        for note in data["notes"]:
            a(f"_{escape(note)}_")

    if base_url:
        a("")
        a(f"<{base_url.rstrip('/')}/customer/?name={data['customer']}|고객사 현황 보기>")

    return "\n".join(L)
