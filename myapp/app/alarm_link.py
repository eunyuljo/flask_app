# app/alarm_link.py
# 알람과 리소스를 잇는다.
#
# ── 오래 못 했던 이유 ───────────────────────────────────────────────
# alarm_advice.py 머리말에 이렇게 적혀 있었다.
#
#   "들어온 이벤트에 리소스 id 가 없다(정규화기가 dimension 을 안 읽는다).
#    그래서 '이 인스턴스에 CPU 알람이 없다' 는 말을 하지 않는다."
#
# 어댑터가 CloudWatch 차원을 읽기 시작하면서 그 제약이 사라졌다.
# 이제 events.meta->>'resource_id' 와 resources.resource_id 가 이어진다.
#
# ── 그런데 절반만 사라졌다 ──────────────────────────────────────────
# 알람이 왔다는 것은 '그 알람이 걸려 있다' 는 사실이다. 확실하다.
# 알람이 안 왔다는 것은 아무 사실도 아니다. 설정이 없어서일 수도 있고,
# 임계를 한 번도 안 넘어서일 수도 있다.
#
# 그래서 여기서 만드는 판정은 셋이다. 둘이 아니다.
#
#   confirmed  이 지표로 알람이 실제로 왔다        <- 사실
#   unsure     안 왔다. 왜 안 왔는지는 모른다      <- 사실 아님
#   not_metric 지표 알람이 아니라 이 방법으로 못 본다
#
# '없음' 이라고 쓰지 않는다. 그건 SLA 에서 '계정을 들여다본 것' 과
# '알람에 대응한 것' 을 구분하지 못했던 실수와 같은 종류다.

from flask import current_app


class LinkError(Exception):
    """알람-리소스 연결을 읽지 못했을 때."""


# 판정. 화면이 이 순서로 읽는다.
STATES = {
    "confirmed": "확인됨",
    "unsure": "모름",
    "not_metric": "지표 알람 아님",
}

SEVERITY_RANK = ("critical", "error", "warning", "info")


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
        raise LinkError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise LinkError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def seen(hours=720, account_ids=None):
    """리소스별로 실제로 온 알람. {리소스 id: {...}}

    돌려주는 것(리소스 하나):
      count       기간 안 알람 건수
      metrics     {지표 이름: 건수}   <- 권고 확인의 근거
      alarms      {알람 이름: 건수}
      worst       가장 나쁜 심각도
      last_at     마지막 발생
      account_id  어느 계정에서

    기본 30일을 본다. 7일로 잡으면 월 1회 도는 배치 알람이 통째로
    '모름' 이 된다 - 그건 이 판정에서 제일 피해야 할 오답이다.
    """
    where = ["e.meta->>'resource_id' IS NOT NULL",
             "e.occurred_at >= now() - make_interval(hours => %s)"]
    params = [hours]
    if account_ids:
        where.append("e.account_id = ANY(%s)")
        params.append(list(account_ids))

    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.events')")
        if cur.fetchone()[0] is None:
            raise LinkError("events 테이블이 없습니다.")

        cur.execute(
            f"""
            SELECT e.meta->>'resource_id'          AS resource_id,
                   e.meta->>'metric'               AS metric,
                   e.meta->>'alarmname'            AS alarm_name,
                   e.severity,
                   e.account_id,
                   count(*)                        AS n,
                   max(e.occurred_at)              AS last_at
              FROM events e
             WHERE {' AND '.join(where)}
             GROUP BY 1, 2, 3, 4, 5
            """,
            params,
        )
        raw = _rows(cur)

    out = {}
    for row in raw:
        entry = out.setdefault(row["resource_id"], {
            "resource_id": row["resource_id"], "count": 0,
            "metrics": {}, "alarms": {}, "severities": {},
            "worst": None, "last_at": None,
            "account_id": row["account_id"],
        })
        entry["count"] += row["n"]
        if row["metric"]:
            entry["metrics"][row["metric"]] = \
                entry["metrics"].get(row["metric"], 0) + row["n"]
        if row["alarm_name"]:
            entry["alarms"][row["alarm_name"]] = \
                entry["alarms"].get(row["alarm_name"], 0) + row["n"]
        entry["severities"][row["severity"]] = \
            entry["severities"].get(row["severity"], 0) + row["n"]
        if entry["last_at"] is None or row["last_at"] > entry["last_at"]:
            entry["last_at"] = row["last_at"]

    for entry in out.values():
        entry["worst"] = next(
            (s for s in SEVERITY_RANK if entry["severities"].get(s)), None)
    return out


# ----------------------------------------------------------------------
# 권고에 '실제로 왔는가' 를 붙인다
# ----------------------------------------------------------------------

def judge(rule, evidence):
    """규칙 하나에 대한 판정. (상태, 근거 지표들)

    부수효과가 없는 순수 함수다. rule 은 alarm_advice.RULES 한 항목,
    evidence 는 seen() 의 그 리소스 항목(없으면 None).
    """
    metrics = rule.get("metrics") or ()
    if not metrics:
        # CloudTrail 이벤트 기반이거나 지표가 아닌 규칙. 이 방법으로는
        # 확인할 수 없다. '모름' 과 섞으면 안 된다 - 저쪽은 확인했는데
        # 근거가 없는 것이고 이쪽은 확인 자체를 못 한 것이다.
        return "not_metric", []

    hit = [m for m in metrics if (evidence or {}).get("metrics", {}).get(m)]
    if hit:
        return "confirmed", hit
    return "unsure", []


def confirm(rows, evidence):
    """권고 목록(alarm_advice.advise 결과)에 판정을 붙인다.

    규칙마다 state/state_metrics 가 생기고, 리소스 줄에는 alarms(실제로
    온 알람 요약)가 붙는다. 원본을 고치지 않고 새 dict 로 돌려준다.
    """
    out = []
    for row in rows:
        found = evidence.get(row["resource_id"])
        judged = []
        for rule in row["rules"]:
            state, hit = judge(rule, found)
            judged.append({**rule, "state": state, "state_metrics": hit})
        out.append({**row, "rules": judged, "alarms": found})
    return out


def summarize(rows):
    """화면 위쪽 숫자.

    confirmed 를 '잘 되고 있는 것' 으로만 읽으면 안 된다. 알람이 왔다는
    것은 그 알람이 걸려 있다는 뜻이지 문제가 없다는 뜻이 아니다.
    """
    counts = {"confirmed": 0, "unsure": 0, "not_metric": 0}
    resources_with_alarms = 0
    for row in rows:
        if row.get("alarms"):
            resources_with_alarms += 1
        for rule in row["rules"]:
            state = rule.get("state")
            if state in counts:
                counts[state] += 1

    checkable = counts["confirmed"] + counts["unsure"]
    return {
        **counts,
        "checkable": checkable,
        # 확인할 수 있는 것 중 몇 %가 확인됐나. 확인 못 하는 규칙까지
        # 분모에 넣으면 영원히 100% 가 안 나온다.
        "confirmed_pct": round(counts["confirmed"] * 100 / checkable)
                         if checkable else 0,
        "resources_with_alarms": resources_with_alarms,
    }
