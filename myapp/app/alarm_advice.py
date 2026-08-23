# app/alarm_advice.py
# 이 리소스에는 어떤 알람을 걸어야 하는가.
#
# ── 무엇을 하지 않는가 ─────────────────────────────────────────────
# 실제로 알람이 걸려 있는지는 보지 않는다. 볼 수가 없다.
#   * CloudWatch 알람 설정을 수집하지 않는다(수집기는 리소스만 훑는다)
#   * 들어온 이벤트에 리소스 id 가 없다(정규화기가 dimension 을 안 읽는다)
#   * 무엇보다 실제 알람은 모니터링 서버 쪽에 있다
#
# 그래서 "이 인스턴스에 CPU 알람이 없다" 는 말을 하지 않는다. 최근에
# 알람이 안 왔다는 것으로 유추할 수도 있지만, 그건 SLA 에서 '계정을
# 들여다본 것' 과 '알람에 대응한 것' 을 구분하지 못했던 실수와 같다.
#
# 이 표가 하는 말은 하나다. "이런 알람을 걸어야 합니다."
# 걸려 있는지는 모니터링 담당이 이 표를 들고 확인할 일이다.
#
# ── 일반론이 아니라 이 리소스에 대한 것 ────────────────────────────
# "EC2 에는 CPU 알람을 거세요" 는 검색하면 나온다. 여기가 값어치가
# 있으려면 스냅샷에 있는 속성으로 갈라야 한다. t3 인지, 퍼블릭 IP 가
# 있는지, IAM 역할이 붙었는지에 따라 걸 것이 달라진다.

# 권고 등급.
LEVELS = {
    "essential": "필수",
    "recommended": "권장",
    "optional": "선택",
}
LEVEL_ORDER = ("essential", "recommended", "optional")

# 크레딧으로 도는 인스턴스 계열. 이 목록이 이 파일에서 가장 실무적인 값이다.
BURSTABLE = ("t2", "t3", "t3a", "t4g")


def _family(item):
    return str(item["attributes"].get("instance_type") or "").split(".")[0].lower()


def _running(item):
    # state 를 아예 안 담은 스냅샷이 있을 수 있다. 그때는 돌고 있다고 본다 -
    # 꺼져 있다고 단정해 권고에서 빼면 진짜 필요한 알람이 사라진다.
    state = item["attributes"].get("state")
    return state is None or state == "running"


# ----------------------------------------------------------------------
# 권고 규칙
# ----------------------------------------------------------------------
# needs: 이 알람을 걸려면 추가로 필요한 것. 비용이나 설정이 드는 것을
#        숨기면 "왜 이건 안 걸려 있지" 가 나중에 나온다.

RULES = [
    {
        "id": "ec2-status-check",
        "type": "ec2:instance",
        "level": "essential",
        "label": "인스턴스 상태 확인 실패",
        "metric": "StatusCheckFailed",
        "hint": "1분 주기, 2회 연속 실패",
        "why": "하드웨어 고장이나 OS 부팅 실패입니다. CPU 사용률로는 "
               "잡히지 않고, 대개 이 알람이 가장 먼저 울립니다.",
        "needs": "",
        "applies": _running,
    },
    {
        "id": "ec2-cpu-credit",
        "type": "ec2:instance",
        "level": "essential",
        "label": "CPU 크레딧 소진",
        "metric": "CPUCreditBalance",
        "hint": "잔량 20 미만이 10분 지속",
        "why": "버스터블 인스턴스는 크레딧이 바닥나면 성능이 기준선까지 "
               "떨어집니다. 그런데 CPU 사용률은 오히려 낮게 보여서, "
               "사용률 알람으로는 절대 잡히지 않습니다. "
               "실무에서 가장 자주 빠지는 항목입니다.",
        "needs": "",
        "applies": lambda i: _running(i) and _family(i) in BURSTABLE,
    },
    {
        "id": "ec2-cpu",
        "type": "ec2:instance",
        "level": "recommended",
        "label": "CPU 사용률 과다",
        "metric": "CPUUtilization",
        "hint": "80% 이상 10분 지속",
        "why": "가장 기본이 되는 부하 신호입니다. 다만 이것만 걸어두면 "
               "위의 크레딧 소진과 아래 디스크·메모리를 놓칩니다.",
        "needs": "",
        "applies": _running,
    },
    {
        "id": "ec2-disk-memory",
        "type": "ec2:instance",
        "level": "recommended",
        "label": "디스크·메모리 사용률",
        "metric": "disk_used_percent / mem_used_percent",
        "hint": "디스크 85%, 메모리 90%",
        "why": "장애의 상당수가 디스크가 차서 생깁니다.",
        # 이걸 안 적으면 "권고했는데 왜 안 걸려 있냐" 가 나중에 나온다.
        "needs": "CloudWatch 에이전트 설치 (기본 지표에는 없습니다)",
        "applies": _running,
    },
    {
        "id": "ec2-network-spike",
        "type": "ec2:instance",
        "level": "recommended",
        "label": "인바운드 트래픽 급증",
        "metric": "NetworkIn",
        "hint": "평소 대비 급증 (이상 탐지 권장)",
        "why": "퍼블릭 IP 가 붙어 인터넷에서 직접 닿는 인스턴스입니다. "
               "공격이나 오작동이 트래픽으로 먼저 드러납니다.",
        "needs": "",
        "applies": lambda i: _running(i) and bool(i["attributes"].get("public_ip")),
    },
    {
        "id": "ec2-role-misuse",
        "type": "ec2:instance",
        "level": "recommended",
        "label": "인스턴스 역할 자격증명 외부 사용",
        "metric": "CloudTrail: AssumeRole / 비정상 SourceIP",
        "hint": "인스턴스 대역 밖에서 이 역할이 쓰이면",
        "why": "IAM 역할이 붙어 있어, 자격증명이 새면 그 역할 권한이 "
               "그대로 나갑니다. IMDSv1 이 열려 있으면 특히 그렇습니다.",
        "needs": "CloudTrail 이벤트 기반 (지표 알람이 아닙니다)",
        "applies": lambda i: _running(i) and bool(i["attributes"].get("iam_profile")),
    },
    {
        "id": "s3-delete-burst",
        "type": "s3:bucket",
        "level": "recommended",
        "label": "객체 대량 삭제",
        "metric": "CloudTrail: DeleteObject",
        "hint": "짧은 시간에 다량 삭제",
        "why": "버저닝이 꺼져 있어 지운 것을 되돌릴 수 없습니다. "
               "되돌릴 수 없는 동작일수록 알람이 먼저 있어야 합니다.",
        "needs": "CloudTrail 데이터 이벤트 (요금이 따로 붙습니다)",
        "applies": lambda i: i["attributes"].get("versioning") != "Enabled",
    },
    {
        "id": "s3-errors",
        "type": "s3:bucket",
        "level": "optional",
        "label": "요청 오류율",
        "metric": "4xxErrors / 5xxErrors",
        "hint": "5분간 오류 비율 급증",
        "why": "애플리케이션이 이 버킷에 기대고 있다면 권한 변경이나 "
               "키 오류가 여기서 드러납니다.",
        "needs": "S3 요청 지표 활성화 (요금이 따로 붙습니다)",
        "applies": lambda i: True,
    },
]

RULES_BY_ID = {r["id"]: r for r in RULES}

# 알람을 걸 대상이 아닌 종류. 화면에서 "권고 0건" 으로 보이면 빠뜨린
# 것처럼 읽히므로, 왜 비어 있는지 적어 둔다.
NOT_ALARMED = {
    "ec2:security_group": "보안그룹은 지표가 없습니다. 변경 추적 대상이며 "
                          "리소스 변경 화면과 컴플라이언스가 봅니다.",
}


def _name(item):
    attrs = item["attributes"]
    return (attrs.get("tags") or {}).get("Name") or attrs.get("name") or ""


def advise(snap):
    """스냅샷 한 장의 리소스마다 걸어야 할 알람을 낸다.

    돌려주는 것: [{"resource_id", "type", "name", "note", "rules": [...]}, ...]

    수집하지 않은 종류는 아예 나오지 않는다. 없는 것이 아니라 못 본
    것이므로, 부르는 쪽이 snap.collected 로 함께 알려야 한다.
    """
    out = []
    for item in snap.items:
        kind = item["resource_type"]
        if kind in NOT_ALARMED:
            out.append({
                "resource_id": item["resource_id"], "type": kind,
                "name": _name(item), "note": NOT_ALARMED[kind], "rules": [],
            })
            continue

        matched = [r for r in RULES
                   if r["type"] == kind and r["applies"](item)]
        matched.sort(key=lambda r: LEVEL_ORDER.index(r["level"]))

        note = ""
        if kind == "ec2:instance" and not _running(item):
            note = (f"상태가 '{item['attributes'].get('state')}' 이라 "
                    "지표 알람을 걸 대상이 아닙니다. 오래 이 상태라면 "
                    "왜 남겨 두는지가 먼저입니다(EBS 요금은 계속 나갑니다).")

        out.append({
            "resource_id": item["resource_id"], "type": kind,
            "name": _name(item), "note": note, "rules": matched,
        })

    out.sort(key=lambda r: (r["type"], r["name"], r["resource_id"]))
    return out


def by_rule(rows):
    """규칙별로 묶는다. 모니터링 담당에게는 이쪽이 일하기 편하다 -
    '크레딧 알람 걸 인스턴스 12대' 로 한 번에 처리한다."""
    groups = []
    for rule in RULES:
        targets = [r for r in rows if any(x["id"] == rule["id"] for x in r["rules"])]
        if targets:
            groups.append({**rule, "targets": targets})
    groups.sort(key=lambda g: (LEVEL_ORDER.index(g["level"]), -len(g["targets"])))
    return groups


def summarize(rows):
    counts = {level: 0 for level in LEVELS}
    for row in rows:
        for rule in row["rules"]:
            counts[rule["level"]] += 1
    return {
        "resources": len([r for r in rows if r["rules"]]),
        "total": sum(counts.values()),
        "by_level": counts,
        "not_alarmed": len([r for r in rows if r["type"] in NOT_ALARMED]),
    }


# ----------------------------------------------------------------------
# 고객사 단위로 모으기
# ----------------------------------------------------------------------
# 위쪽은 스냅샷 한 장만 보는 순수 함수다(테스트에서 손으로 만들어 먹인다).
# 여기서부터 DB 를 본다 - standards.evaluate 와 같은 구조다.

class AdviceError(Exception):
    """권고를 모으지 못했을 때."""


def for_customer(customer):
    """이 고객사의 모든 계정·리전을 합쳐 권고를 낸다.

    돌려주는 것:
      {"rows", "groups", "counts", "snapshots", "missing_types"}

    missing_types 가 중요하다. 수집하지 않은 리소스 종류는 권고 목록에
    아예 나오지 않는데, 그걸 '걸 알람이 없다' 로 읽으면 안 된다.
    """
    from app import compliance
    from app.accounts import list_accounts, AccountError
    from app.collect import COLLECTED_TYPES
    from app.compliance import ComplianceError

    try:
        accounts = [a["account_id"] for a in list_accounts(enabled_only=False)
                    if a["customer"] == customer]
    except AccountError as e:
        raise AdviceError(str(e))

    if not accounts:
        raise AdviceError("이 고객사에 등록된 계정이 없습니다.")

    try:
        snapshots = [s for s in compliance.latest_snapshots(limit=200)
                     if s["account_id"] in accounts]
    except ComplianceError as e:
        raise AdviceError(str(e))

    if not snapshots:
        raise AdviceError(
            "이 고객사의 리소스 스냅샷이 없습니다. "
            "flask --app run collect-resources 를 먼저 실행하세요."
        )

    rows, collected = [], set()
    try:
        with compliance._connect() as conn, conn.cursor() as cur:
            for meta in snapshots:
                snap = compliance.load_snapshot(cur, meta["snapshot_id"])
                collected |= snap.collected
                for row in advise(snap):
                    rows.append({**row, "account_id": meta["account_id"],
                                 "region": meta["region"]})
    except Exception as e:                       # noqa: BLE001
        raise AdviceError(f"스냅샷을 읽지 못했습니다: {e}")

    rows.sort(key=lambda r: (r["type"], r["account_id"], r["region"],
                             r["name"], r["resource_id"]))
    return {
        "customer": customer,
        "rows": rows,
        "groups": by_rule(rows),
        "counts": summarize(rows),
        "snapshots": snapshots,
        # 수집기가 훑기로 한 것 중 이번에 안 담긴 종류.
        "missing_types": sorted(set(COLLECTED_TYPES) - collected),
    }
