# app/readiness.py
# 고객사를 받을 준비가 됐는가.
#
# 컴플라이언스(app/compliance.py)와 구조가 같다. 점검은 코드에 두고,
# 결과는 저장하지 않고 볼 때마다 다시 계산한다. 다른 점은 점검 대상이다.
#   컴플라이언스 : 고객사의 AWS 리소스가 기준에 맞는가
#   준비도       : 우리 쪽 설정이 갖춰져 있는가
#
# ── 왜 필요한가 ─────────────────────────────────────────────────────
# 신규 고객사를 받으면 할 일이 흩어져 있다. 계정 등록은 CLI 에서,
# SLA 목표는 리포트 화면에서, 당직 담당자는 또 다른 CLI 에서, 런북은
# 런북 화면에서. 어느 하나만 빠져도 조용히 망가진다.
#
#   당직 담당자가 없으면  -> 에스컬레이션이 아무도 부르지 않는다
#   SLA 목표가 없으면     -> 초과를 판정할 기준이 없다
#   스냅샷이 없으면       -> 컴플라이언스도 RCA 도 작업 증적도 못 쓴다
#
# 셋 다 "에러" 가 아니라 "조용한 없음" 이라서, 사고가 나고 나서야 안다.
# 이 화면은 그걸 미리 한 번에 보여준다.

from flask import current_app

# 필수  : 없으면 서비스를 시작하면 안 된다
# 권장  : 없어도 돌아가지만 사고가 났을 때 곤란해진다
# 선택  : 있으면 좋다
LEVELS = {"required": "필수", "recommended": "권장", "optional": "선택"}
LEVEL_ORDER = {"required": 0, "recommended": 1, "optional": 2}

# 알람이 이 기간 안에 한 건도 없으면 "안 들어오는 것" 으로 본다.
# 조용한 고객사와 연결이 끊긴 고객사를 구분할 방법이 이것뿐이다.
ALARM_WINDOW_DAYS = 7

# 스냅샷이 이만큼 오래됐으면 낡은 것으로 본다.
# 이보다 오래된 자료로 컴플라이언스를 판정하면 "지금 상태" 가 아니다.
SNAPSHOT_STALE_DAYS = 7


class ReadinessError(Exception):
    """준비도를 확인하지 못했을 때."""


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
        raise ReadinessError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise ReadinessError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _has(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


# ----------------------------------------------------------------------
# 자료 모으기
# ----------------------------------------------------------------------

def gather(customer):
    """점검에 필요한 것을 한 번에 읽어온다.

    점검 함수마다 DB 를 부르면 항목 수만큼 질의가 나간다. 여기서 다
    모아두고, 점검은 순수 함수로 둔다(컴플라이언스와 같은 방식이라
    테스트에서 손으로 만들어 먹일 수 있다).
    """
    facts = {
        "customer": customer,
        "accounts": [],
        "alarm_count": 0,
        "last_alarm": None,
        "sla_targets": [],
        "oncall": [],
        "last_snapshot": None,
        "runbook_gaps": [],
        "work_orders": 0,
        "missing_tables": [],
    }

    with _connect() as conn, conn.cursor() as cur:
        # 테이블 존재 확인을 먼저 전부 끝낸다. 본 질의 사이에 끼우면
        # 같은 커서라 앞의 결과가 덮어써진다 - 이 프로젝트에서 두 번 겪었다.
        present = {
            name: _has(cur, name)
            for name in ("aws_accounts", "events", "sla_targets", "oncall_members",
                         "resource_snapshots", "runbooks", "work_orders")
        }
        facts["missing_tables"] = sorted(n for n, ok in present.items() if not ok)

        if not present["aws_accounts"]:
            raise ReadinessError(
                "aws_accounts 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )

        cur.execute(
            "SELECT account_id, alias, regions, enabled, role_arn, external_id "
            "FROM aws_accounts WHERE customer = %s ORDER BY account_id",
            (customer,),
        )
        facts["accounts"] = _rows(cur)
        account_ids = [a["account_id"] for a in facts["accounts"]]

        if present["events"] and account_ids:
            cur.execute(
                "SELECT count(*), max(occurred_at) FROM events "
                "WHERE account_id = ANY(%s) "
                "  AND occurred_at > now() - make_interval(days => %s)",
                (account_ids, ALARM_WINDOW_DAYS),
            )
            facts["alarm_count"], facts["last_alarm"] = cur.fetchone()

        if present["sla_targets"]:
            # 0 은 "목표 없음" 이라는 뜻이라 있는 것으로 세지 않는다.
            # 행이 있으니 설정된 것처럼 보이지만 집계에서 빠진다.
            #
            # 기본값(customer = '')도 함께 읽는다. app/sla.py 는 전용 목표가
            # 없으면 기본값으로 판정하므로, 여기서 전용만 보고 "없음" 이라고
            # 하면 두 화면이 서로 다른 말을 하게 된다.
            cur.execute(
                "SELECT severity, first_response_minutes AS minutes, customer "
                "  FROM sla_targets "
                " WHERE customer IN (%s, '') AND first_response_minutes > 0",
                (customer,),
            )
            facts["sla_targets"] = _rows(cur)

        if present["oncall_members"]:
            # 고객사 전담이 없으면 전체 담당이 받는다. 그래서 둘 다 본다.
            cur.execute(
                "SELECT name, level, slack_id, customer FROM oncall_members "
                "WHERE customer IN (%s, '') ORDER BY level",
                (customer,),
            )
            facts["oncall"] = _rows(cur)

        if present["resource_snapshots"] and account_ids:
            cur.execute(
                "SELECT max(collected_at) FROM resource_snapshots "
                "WHERE account_id = ANY(%s) AND complete",
                (account_ids,),
            )
            facts["last_snapshot"] = cur.fetchone()[0]

        # 런북이 없는 지문. 이 고객사에서 자주 나는 것부터 본다.
        # 자주 나는데 절차가 없다는 건 매번 사람이 처음부터 생각한다는 뜻이다.
        if present["events"] and present["runbooks"] and account_ids:
            cur.execute(
                """
                SELECT e.fingerprint, count(*) AS times,
                       (array_agg(e.message ORDER BY e.occurred_at DESC))[1] AS sample
                  FROM events e
                 WHERE e.account_id = ANY(%s)
                   AND e.fingerprint <> ''
                   AND e.severity IN ('critical', 'error')
                   AND e.occurred_at > now() - make_interval(days => 30)
                   AND NOT EXISTS (
                        SELECT 1 FROM runbooks r
                         WHERE r.fingerprint = e.fingerprint
                           AND r.customer IN (%s, '')
                   )
                 GROUP BY e.fingerprint
                 ORDER BY times DESC
                 LIMIT 5
                """,
                (account_ids, customer),
            )
            facts["runbook_gaps"] = _rows(cur)

        if present["work_orders"]:
            cur.execute(
                "SELECT count(*) FROM work_orders WHERE customer = %s", (customer,)
            )
            facts["work_orders"] = cur.fetchone()[0]

    return facts


# ----------------------------------------------------------------------
# 점검 항목
# ----------------------------------------------------------------------
# 각 함수는 facts 를 받아 (상태, 설명) 을 돌려준다.
#   ok      : 갖춰짐
#   missing : 없음
#   warn    : 있지만 온전하지 않음
#   unknown : 판단할 자료가 없음(테이블이 없거나 DB 를 못 봄)

def _check_account(facts):
    if not facts["accounts"]:
        return "missing", "등록된 계정이 없습니다."
    enabled = [a for a in facts["accounts"] if a["enabled"]]
    if not enabled:
        return "warn", f"계정 {len(facts['accounts'])}개가 전부 꺼져 있습니다."
    return "ok", f"계정 {len(enabled)}개가 등록되어 있습니다."


def _check_assume_role(facts):
    if not facts["accounts"]:
        return "missing", "등록된 계정이 없습니다."
    demo = [a for a in facts["accounts"] if not a["role_arn"]]
    if len(demo) == len(facts["accounts"]):
        return "missing", "전부 데모 계정입니다. 실제 AWS 를 부르지 않습니다."
    if demo:
        names = ", ".join(a["account_id"] for a in demo)
        return "warn", f"일부가 아직 데모 계정입니다: {names}"
    return "ok", "모든 계정에 AssumeRole 이 설정되어 있습니다."


def _check_regions(facts):
    empty = [a["account_id"] for a in facts["accounts"] if not a["regions"]]
    if not facts["accounts"]:
        return "missing", "등록된 계정이 없습니다."
    if empty:
        return "missing", f"리전이 비어 있습니다: {', '.join(empty)}"
    return "ok", "모든 계정에 리전이 지정되어 있습니다."


def _check_alarms(facts):
    if "events" in facts["missing_tables"]:
        return "unknown", "events 테이블이 없습니다."
    if not facts["accounts"]:
        return "missing", "등록된 계정이 없습니다."
    if not facts["alarm_count"]:
        return "missing", (
            f"최근 {ALARM_WINDOW_DAYS}일간 들어온 알람이 없습니다. "
            "연동이 끊겼거나 계정 귀속이 안 되고 있을 수 있습니다."
        )
    return "ok", (
        f"최근 {ALARM_WINDOW_DAYS}일간 {facts['alarm_count']}건이 들어왔습니다."
    )


def _check_sla(facts):
    if "sla_targets" in facts["missing_tables"]:
        return "unknown", "sla_targets 테이블이 없습니다."
    if not facts["sla_targets"]:
        return "missing", "이 고객사에도 기본값에도 목표가 없습니다."

    scoped = [t for t in facts["sla_targets"] if t.get("customer")]
    if not scoped:
        parts = ", ".join(f"{t['severity']} {t['minutes']}분" for t in facts["sla_targets"])
        return "warn", (
            f"전용 목표 없이 기본값으로 판정됩니다({parts}). "
            "계약서에 적힌 값과 같은지 확인하세요."
        )
    parts = ", ".join(f"{t['severity']} {t['minutes']}분" for t in scoped)
    return "ok", f"전용 목표가 정해져 있습니다: {parts}"


def _check_oncall(facts):
    if "oncall_members" in facts["missing_tables"]:
        return "unknown", "oncall_members 테이블이 없습니다."
    if not facts["oncall"]:
        return "missing", "이 고객사를 받을 담당자가 한 명도 없습니다."
    scoped = [m for m in facts["oncall"] if m["customer"]]
    no_slack = [m["name"] for m in facts["oncall"] if not m["slack_id"]]
    if no_slack:
        return "warn", (
            f"담당자 {len(facts['oncall'])}명 중 Slack ID 가 없는 사람이 있습니다: "
            f"{', '.join(no_slack)}. 멘션이 걸리지 않습니다."
        )
    if not scoped:
        return "warn", (
            f"전담 없이 전체 담당 {len(facts['oncall'])}명이 받습니다."
        )
    return "ok", f"전담 {len(scoped)}명을 포함해 {len(facts['oncall'])}명이 있습니다."


def _check_snapshot(facts):
    from datetime import datetime, timezone

    if "resource_snapshots" in facts["missing_tables"]:
        return "unknown", "resource_snapshots 테이블이 없습니다."
    if not facts["last_snapshot"]:
        return "missing", "이 고객사 계정으로 찍은 스냅샷이 없습니다."
    age = (datetime.now(timezone.utc) - facts["last_snapshot"]).days
    if age > SNAPSHOT_STALE_DAYS:
        return "warn", (
            f"마지막 수집이 {age}일 전입니다. 이 자료로 판정한 것은 "
            "'지금 상태' 가 아닙니다."
        )
    return "ok", f"마지막 수집이 {age}일 전입니다."


def _check_runbooks(facts):
    if "runbooks" in facts["missing_tables"]:
        return "unknown", "runbooks 테이블이 없습니다."
    if not facts["runbook_gaps"]:
        return "ok", "자주 나는 알람에 대응 절차가 붙어 있습니다."
    worst = facts["runbook_gaps"][0]
    return "warn", (
        f"절차가 없는 알람이 {len(facts['runbook_gaps'])}종 있습니다. "
        f"가장 잦은 것은 30일간 {worst['times']}회입니다."
    )


def _check_work_orders(facts):
    if "work_orders" in facts["missing_tables"]:
        return "unknown", "work_orders 테이블이 없습니다."
    if not facts["work_orders"]:
        return "missing", "작업 기록이 한 건도 없습니다."
    return "ok", f"작업 기록이 {facts['work_orders']}건 있습니다."


# 고치러 갈 화면의 이름. url_for 에는 엔드포인트가 필요하지만,
# 화면에 "report.sla" 라고 찍으면 그게 어디인지 아는 사람만 읽을 수 있다.
ENDPOINT_LABELS = {
    "alarm.index": "이벤트 화면",
    "report.sla": "보고 > SLA",
    "runbook.index": "런북 화면",
    "work.index": "작업 기록 화면",
}


# how: 어디서 고치는지. 화면이 있으면 endpoint 를, 없으면 명령을 준다.
# "안 됐다" 만 알려주고 어디서 고치는지 안 알려주면 화면을 두 번 돌게 된다.
CHECKS = [
    {
        "id": "account-registered",
        "title": "계정이 등록되어 있다",
        "level": "required",
        "why": "계정이 없으면 나머지가 전부 대상 없이 도는 것입니다.",
        "how": "flask --app run add-account --customer <고객사> --account-id <12자리>",
        "fn": _check_account,
    },
    {
        "id": "assume-role",
        "title": "AssumeRole 이 설정되어 있다",
        "level": "required",
        "why": "역할이 없으면 데모 계정으로 취급되어 실제 AWS 를 부르지 않습니다. "
               "화면에는 값이 보이지만 전부 합성 자료입니다.",
        "how": "flask --app run add-account ... --role-arn arn:aws:iam::<계정>:role/<역할>",
        "fn": _check_assume_role,
    },
    {
        "id": "regions",
        "title": "리전이 지정되어 있다",
        "level": "required",
        "why": "허용 리전이 비어 있으면 콘솔도 수집도 리전을 고를 수 없습니다.",
        "how": "flask --app run add-account ... --regions ap-northeast-2",
        "fn": _check_regions,
    },
    {
        "id": "alarm-arriving",
        "title": "알람이 들어오고 있다",
        "level": "required",
        "why": "알람이 안 들어오면 이 앱이 하는 일의 대부분이 멈춥니다. "
               "조용한 것과 끊긴 것은 화면에서 똑같아 보입니다.",
        "how": "endpoint:alarm.index",
        "fn": _check_alarms,
    },
    {
        "id": "oncall",
        "title": "당직 담당자가 있다",
        "level": "required",
        "why": "에스컬레이션이 걸려도 부를 사람이 없으면 아무 일도 일어나지 않습니다. "
               "이건 오류가 아니라 조용한 없음이라 사고가 나고서야 압니다.",
        "how": "flask --app run add-oncall --name <이름> --level 1 --slack-id U01ABCDEF",
        "fn": _check_oncall,
    },
    {
        "id": "sla-target",
        "title": "SLA 목표가 정해져 있다",
        "level": "required",
        "why": "목표가 없으면 초과를 판정할 기준이 없어, 위반 알림도 "
               "에스컬레이션도 걸리지 않습니다. 기본값으로 도는 것도 "
               "계약 값과 다르면 잘못된 판정입니다.",
        "how": "endpoint:report.sla",
        "fn": _check_sla,
    },
    {
        "id": "snapshot",
        "title": "리소스 스냅샷을 찍고 있다",
        "level": "recommended",
        "why": "컴플라이언스 점검·리소스 변경 추적·작업 증적이 전부 이 자료 "
               "위에서 돕니다. 하나가 없으면 셋이 같이 빕니다.",
        "how": "flask --app run collect-resources --account <12자리> --region <리전>",
        "fn": _check_snapshot,
    },
    {
        "id": "runbook",
        "title": "자주 나는 알람에 절차가 있다",
        "level": "recommended",
        "why": "절차가 없으면 같은 알람마다 사람이 처음부터 생각합니다. "
               "당직자가 바뀌면 대응도 바뀝니다.",
        "how": "endpoint:runbook.index",
        "fn": _check_runbooks,
    },
    {
        "id": "work-evidence",
        "title": "작업 기록을 쓰고 있다",
        "level": "optional",
        "why": "고객사 계정을 건드린 근거가 남지 않으면, 나중에 "
               "'그때 무엇을 바꿨나' 에 답할 수 없습니다.",
        "how": "endpoint:work.index",
        "fn": _check_work_orders,
    },
]

CHECKS_BY_ID = {c["id"]: c for c in CHECKS}

# 화면에서 쓰는 표시. ok 가 아닌 것을 전부 빨갛게 만들면 우선순위가 없어진다.
STATUS_LABEL = {
    "ok": "완료", "warn": "확인 필요", "missing": "없음", "unknown": "판단 불가",
}


def evaluate(customer):
    """고객사 하나의 준비도."""
    facts = gather(customer)
    return check(facts), facts


def check(facts):
    """자료를 받아 점검한다. DB 를 보지 않는 순수 함수다."""
    results = []
    for c in CHECKS:
        status, detail = c["fn"](facts)
        results.append({
            "id": c["id"],
            "title": c["title"],
            "level": c["level"],
            "why": c["why"],
            "how": c["how"],
            "status": status,
            "detail": detail,
        })
    results.sort(key=lambda r: (
        # 안 된 것부터, 그중에서도 필수부터.
        0 if r["status"] != "ok" else 1,
        LEVEL_ORDER[r["level"]],
    ))
    return results


def summarize(results):
    """준비도 한 줄 요약.

    '몇 퍼센트' 는 필수 항목이 빠져 있어도 90% 가 나올 수 있어서 쓰지 않는다.
    필수가 하나라도 비면 '받을 수 없음' 이다.
    """
    blocking = [r for r in results if r["level"] == "required" and r["status"] in ("missing", "warn")]
    done = [r for r in results if r["status"] == "ok"]
    return {
        "total": len(results),
        "done": len(done),
        "blocking": len(blocking),
        "ready": not blocking,
        "unknown": len([r for r in results if r["status"] == "unknown"]),
    }
