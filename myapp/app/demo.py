# app/demo.py
# 사람이 채우는 표에 샘플을 넣는다. 로컬에서 화면을 볼 수 있게.
#
# ── 왜 필요한가 ────────────────────────────────────────────────────
# 표 25개 중 18개가 비어 있었다. 그런데 비어 있는 쪽은 전부 '사람이
# 채우는 것' 이다 - 런북, 장애, 작업, 연락처, 점검 주기, 억제 규칙.
#
# 기계가 채우는 쪽(events, resources, audit_log, job_runs)에는 명령이
# 있었는데 사람이 채우는 쪽에는 하나도 없었다. 그래서 로컬에서 화면을
# 열면 절반이 "아직 없습니다" 였고, 그 화면들이 제대로 도는지 볼 수 없었다.
#
# ── 지어내지 않는다 ─────────────────────────────────────────────────
# 런북은 events 에 실제로 있는 지문에 붙인다. 장애는 실제 지문과 잇는다.
# 작업은 등록된 계정에 만든다. 지어낸 값으로 채우면 화면은 차지만 연결이
# 전부 끊겨 있어서, 정작 보려던 '이어지는가' 를 못 본다.
#
# ── 지울 수 있어야 한다 ─────────────────────────────────────────────
# 샘플과 진짜가 섞이면 나중에 못 가른다. 모든 행에 MARK 를 남기고,
# --clear 는 MARK 가 붙은 것만 지운다. 사람이 손으로 쓴 것은 건드리지 않는다.

from datetime import datetime, timedelta, timezone


# 이 표시가 붙은 행만 샘플이다. 지울 때 이걸로 고른다.
# 눈에 띄어야 한다 - 화면에서 보고 "이건 샘플이구나" 를 알 수 있게.
MARK = "(샘플)"


class DemoError(Exception):
    """샘플을 넣지 못했을 때."""


def _now():
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------
# 재료 — DB 에 이미 있는 것에서 가져온다
# ----------------------------------------------------------------------

def materials(limit_fingerprints=6):
    """샘플을 붙일 자리. 없으면 그만큼 덜 만든다.

    지문은 실제로 이벤트가 있는 것에서 고른다. 자주 나는 것부터 -
    런북을 써야 할 순서와 같다.
    """
    from app import accounts, alarm_link, noise

    out = {"accounts": [], "fingerprints": [], "resources": {}}

    try:
        out["accounts"] = accounts.list_accounts(enabled_only=False)
    except Exception:                                    # noqa: BLE001
        pass

    try:
        rows = noise.ranking(hours=24 * 60, limit=40)
        # 정규화 못 한 것에는 절차를 쓰지 않는다. 어댑터로 고칠 일이다.
        rows = [r for r in rows
                if r.get("event_type") not in noise.NOT_A_RUNBOOK_TARGET]
        out["fingerprints"] = rows[:limit_fingerprints]
    except Exception:                                    # noqa: BLE001
        pass

    try:
        out["resources"] = alarm_link.seen()
    except Exception:                                    # noqa: BLE001
        pass

    return out


# ----------------------------------------------------------------------
# 내용
# ----------------------------------------------------------------------
# 실무에서 실제로 쓰는 모양에 가깝게 둔다. "1. 확인 2. 조치" 같은 껍데기를
# 넣으면 런북 화면이 도는 것만 보이고 런북이 무엇인지는 안 보인다.

RUNBOOK_BODY = """## 무엇을 먼저 보나
1. 대시보드에서 같은 지문의 최근 발생 추이를 본다. 처음인가 반복인가.
2. 같은 시각에 다른 알람이 함께 왔는지 본다. 혼자 왔으면 그 리소스 문제,
   같이 왔으면 위쪽(네트워크·의존 서비스)을 먼저 의심한다.

## 확인
- `aws cloudwatch get-metric-statistics` 로 최근 1시간 지표를 본다.
- 콘솔 화면에서 해당 인스턴스의 상태 검사와 최근 이벤트를 본다.

## 조치
- 임계를 잠깐 넘은 것이면 기록만 남기고 닫는다.
- 10분 이상 지속되면 담당자에게 알리고 작업 요청을 만든다.
- **재기동은 승인 없이 하지 않는다.** 작업 기록에서 승인을 받는다.

## 끝내기
- 이 절차대로 해결됐으면 아래 '실행 기록' 에 결과를 남긴다.
- 절차가 안 맞았으면 그것도 남긴다. 안 맞았다는 기록이 다음 사람을 살린다.
"""

INCIDENTS = [
    {
        "title": f"{MARK} 결제 API 응답 지연 30분",
        "severity": "critical",
        "hours_ago": 74,
        "minutes": 31,
        "impact": "결제 요청의 약 12%가 30초 이상 지연됐고 그중 340건이 실패로 "
                  "떨어졌습니다. 09:12~09:43 사이입니다.",
        "cause": "RDS 커넥션 풀이 가득 찼습니다. 전날 배포에서 커넥션을 닫지 않는 "
                 "경로가 하나 들어갔고, 트래픽이 오르면서 누수가 한계에 닿았습니다.",
        "action": "문제 경로를 이전 버전으로 되돌리고 커넥션 풀을 재기동했습니다. "
                  "09:43 부터 지연이 정상으로 돌아왔습니다.",
        "prevention": "커넥션 수 알람을 임계 180 으로 걸었습니다(전에는 없었습니다). "
                      "배포 후 30분 동안 커넥션 추이를 보는 절차를 런북에 넣었습니다.",
        "publish": True,
    },
    {
        "title": f"{MARK} 웹 서버 1대 상태 검사 실패",
        "severity": "error",
        "hours_ago": 30,
        "minutes": 18,
        "impact": "ALB 뒤 3대 중 1대가 빠졌습니다. 나머지 2대가 받아서 "
                  "사용자 영향은 없었습니다.",
        "cause": "하드웨어 문제로 인스턴스 상태 검사가 실패했습니다. "
                 "AWS Health 에도 같은 시각에 성능 저하 이벤트가 올라왔습니다.",
        "action": "인스턴스를 정지 후 시작해 다른 하드웨어로 옮겼습니다.",
        "prevention": "",          # 일부러 비워 둔다 - 초안 상태를 화면에서 보려고
        "publish": False,
    },
]

WORK_ORDERS = [
    {
        "title": f"{MARK} 웹 서버 보안 패치 적용",
        "request": "web-01, web-02 에 커널 보안 패치를 적용합니다.",
        "expected": "패키지 목록에 변화가 있고 재기동 후 인스턴스가 정상 상태여야 합니다.",
        "rollback": "패치 전 AMI 로 되돌립니다. 스냅샷을 먼저 찍습니다.",
        "stage": "closed",
    },
    {
        "title": f"{MARK} RDS 파라미터 그룹 변경",
        "request": "max_connections 를 200 에서 400 으로 올립니다.",
        "expected": "커넥션 상한이 올라가고 재기동 후 접속이 유지되어야 합니다.",
        "rollback": "이전 파라미터 그룹으로 되돌리고 재기동합니다.",
        "stage": "open",
    },
    {
        "title": f"{MARK} 보안그룹 인바운드 정리",
        "request": "0.0.0.0/0 으로 열린 22번 포트를 사무실 대역으로 좁힙니다.",
        "expected": "해당 규칙이 사라지고 사무실 대역 규칙만 남아야 합니다.",
        "rollback": "삭제한 규칙을 그대로 다시 넣습니다.",
        "stage": "requested",
    },
]

CONTACTS = [
    ("김운영", "primary", "ops@example.com", "010-0000-0001",
     "평일 09~18시 1차 연락"),
    ("박야간", "emergency", "oncall@example.com", "010-0000-0002",
     "야간·주말 긴급"),
    ("이보고", "report", "report@example.com", "", "월간 리포트 수신"),
    ("최결재", "approver", "approve@example.com", "", "작업 승인 권한"),
]

ROUTINES = [
    ("백업 복원 시험", 90, "백업이 있다는 것과 복원된다는 것은 다릅니다."),
    ("IAM 키 회전 점검", 30, "90일 넘은 액세스 키가 남아 있는지 봅니다."),
    ("보안그룹 검토", 30, "임시로 열어둔 규칙이 그대로 남아 있는 일이 잦습니다."),
]

# note 에 MARK 를 남긴다. 이 표는 값(rule/value)이 진짜 설정이라 이름에
# 표시를 넣을 수 없다 - 넣으면 컴플라이언스가 그 값으로 점검해 버린다.
STANDARDS = [
    ("required_tags", "Owner,Service,Env",
     f"{MARK} 이 세 개는 청구 분류와 담당자 추적에 씁니다."),
    ("instance_types", "t3,t3a,m6i,c6i",
     f"{MARK} 합의한 계열 밖은 검토 대상입니다."),
]

ONCALL = [
    (f"김1차 {MARK}", 1, ""),
    (f"박2차 {MARK}", 2, ""),
    (f"최관리자 {MARK}", 3, ""),
]


# ----------------------------------------------------------------------
# 넣기
# ----------------------------------------------------------------------

def seed(echo=print):
    """샘플을 넣는다. 무엇을 몇 건 넣었는지 돌려준다.

    한 곳이 실패해도 나머지는 넣는다. 로컬 데이터가 제각각이라
    (계정이 없다, 이벤트가 없다) 하나 때문에 전부 멈추면 못 쓴다.
    """
    stock = materials()
    made = {}

    for name, fn in (
        ("oncall_members", _oncall),
        ("customer_contacts", _contacts),
        ("customer_routines", _routines),
        ("customer_standards", _standards),
        ("runbooks", _runbooks),
        ("alarm_rules", _rules),
        ("incidents", _incidents),
        ("work_orders", _work),
        ("deliveries", _deliveries),
    ):
        try:
            made[name] = fn(stock)
        except Exception as e:                           # noqa: BLE001
            made[name] = 0
            echo(f"  {name:20} 건너뜀 — {e}")
    return made


def _customers(stock):
    seen, out = set(), []
    for account in stock["accounts"]:
        name = account.get("customer") or ""
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _oncall(stock):
    from app import escalation

    for name, level, slack in ONCALL:
        escalation.add_member(name=name, level=level, slack_id=slack)
    return len(ONCALL)


def _contacts(stock):
    from app import contacts

    n = 0
    for customer in _customers(stock):
        for name, kind, email, phone, note in CONTACTS:
            contacts.add(customer=customer, name=f"{name} {MARK}", kind=kind,
                         email=email, phone=phone, note=note)
            n += 1
    return n


def _routines(stock):
    from app import routines

    n = 0
    for customer in _customers(stock):
        for name, days, why in ROUTINES:
            routine_id = routines.add(customer=customer, name=f"{name} {MARK}",
                                      interval_days=days, why=why)
            n += 1
            # 하나는 최근에 한 것으로 둔다. 전부 '한 번도 안 함' 이면
            # 지연 판정만 보이고 정상 판정을 못 본다.
            if name == "보안그룹 검토":
                routines.mark_done(routine_id, done_by=MARK, note="이상 없음")
    return n


def _standards(stock):
    from app import standards

    n = 0
    for customer in _customers(stock):
        for rule, value, note in STANDARDS:
            standards.save(customer=customer, rule=rule, value=value, note=note)
            n += 1
    return n


def _runbooks(stock):
    """실제로 이벤트가 있는 지문에만 붙인다.

    전부에 붙이지 않는다. 커버리지 화면이 '아직 비어 있는 것' 을 보여주는
    화면이라, 100%로 채우면 그 화면이 무슨 일을 하는지 안 보인다.
    """
    from app import runbook

    rows = stock["fingerprints"]
    if not rows:
        raise DemoError("이벤트가 없습니다. seed-events 를 먼저 실행하세요.")

    n = 0
    for row in rows[: max(1, len(rows) // 2)]:
        name = row.get("alarm_name") or (row.get("sample") or "")[:40]
        runbook_id = runbook.save(
            fingerprint=row["fingerprint"],
            title=f"{MARK} {name} 대응",
            body=RUNBOOK_BODY,
            author=MARK,
            sample=(row.get("sample") or "")[:200],
        )
        n += 1
        # 절차만 있고 실행 기록이 없으면 '이 절차가 통하는가' 화면이 빈다.
        if n == 1:
            runbook.record_run(
                runbook_id=runbook_id, outcome="resolved", ran_by=MARK,
                note="절차대로 해결됐습니다.", minutes=12)
            runbook.record_run(
                runbook_id=runbook_id, outcome="partial", ran_by=MARK,
                note="절차에 없는 단계가 하나 필요했습니다. 3번 항목을 고쳐야 합니다.",
                minutes=35)
    return n


def _rules(stock):
    """억제 규칙. 가장 시끄러운 것 하나에만 건다."""
    from app import noise

    rows = stock["fingerprints"]
    if not rows:
        raise DemoError("이벤트가 없습니다.")
    row = rows[0]
    noise.save_rule(
        fingerprint=row["fingerprint"], window_minutes=30, muted=False,
        note=f"{MARK} 같은 알람은 30분에 한 번만. 배치 시간대 정상 동작.",
        author=MARK, sample=(row.get("sample") or "")[:120])
    return 1


def _incidents(stock):
    from app import incident

    accounts = stock["accounts"]
    fingerprints = stock["fingerprints"]
    n = 0
    for i, spec in enumerate(INCIDENTS):
        account = accounts[i % len(accounts)] if accounts else {}
        started = _now() - timedelta(hours=spec["hours_ago"])
        incident_id = incident.create(
            title=spec["title"],
            started_at=started,
            ended_at=started + timedelta(minutes=spec["minutes"]),
            author=MARK,
            severity=spec["severity"],
            customer=account.get("customer", ""),
            account_id=account.get("account_id", ""),
            region=(account.get("regions") or ["ap-northeast-2"])[0],
        )
        incident.update_narrative(incident_id, {
            k: spec[k] for k in ("impact", "cause", "action", "prevention")
            if spec[k]
        })

        # 사람이 지목한 지문 하나를 잇는다. 시간이 겹쳐 딸려온 것과
        # 근거의 무게가 다르므로 origin 으로 남는다.
        if fingerprints and i < len(fingerprints):
            row = fingerprints[i]
            incident.link_origin(incident_id, row["fingerprint"],
                                 sample=(row.get("sample") or "")[:200])

        if spec["publish"]:
            incident.publish(incident_id)
        n += 1
    return n


def _work(stock):
    """작업을 여러 단계로 흩어 둔다.

    전부 '승인 대기' 로 두면 승인 뒤 화면(스냅샷, 차이, 확정)을 못 본다.
    """
    from app import work

    accounts = stock["accounts"]
    if not accounts:
        raise DemoError("등록된 계정이 없습니다. add-account 를 먼저 실행하세요.")

    n = 0
    for i, spec in enumerate(WORK_ORDERS):
        account = accounts[i % len(accounts)]
        start = _now() + timedelta(hours=2)
        work_id = work.create(
            title=spec["title"], customer=account.get("customer", ""),
            account_id=account["account_id"],
            region=(account.get("regions") or ["ap-northeast-2"])[0],
            operator=f"작업자 {MARK}", requested_by=f"요청자 {MARK}",
            ticket=f"OPS-{100 + i}",
            request=spec["request"], expected=spec["expected"],
            rollback=spec["rollback"],
            window_start=start, window_end=start + timedelta(hours=3),
        )
        # requested 는 만든 그대로 둔다.
        if spec["stage"] in ("open", "closed"):
            work.approve(work_id, approver=f"승인자 {MARK}",
                         note=f"{MARK} 내용 확인했습니다.")
        n += 1
    return n


def _deliveries(stock):
    from app import delivery

    n = 0
    for customer in _customers(stock):
        # 산출물 종류를 섞는다. 한 종류만 넣으면 발송 기록 화면의
        # 종류별 묶음이 도는지 알 수 없다.
        for kind, title in (("msr", "월간 리뷰"), ("report", "주간 운영 리포트")):
            delivery.record(customer=customer, kind=kind, channel="email",
                            sent_by=MARK, title=f"{MARK} {title}",
                            # 그때의 값을 그대로 남기는 칸이다. 연락처를
                            # 참조만 하면 담당자가 바뀐 뒤 지난 기록이
                            # 새 사람 이름으로 보인다.
                            recipients=f"이보고 {MARK} <report@example.com>",
                            note="정기 발송")
            n += 1
    return n


# ----------------------------------------------------------------------
# 지우기
# ----------------------------------------------------------------------
# MARK 가 붙은 것만 지운다. 사람이 손으로 쓴 것은 건드리지 않는다.
# 지우는 순서가 중요하다 - 자식 먼저, 부모 나중.

CLEAN = (
    ("runbook_runs", "ran_by LIKE %s"),
    ("runbooks", "author LIKE %s"),
    ("alarm_rules", "note LIKE %s"),
    ("routine_runs", "done_by LIKE %s"),
    ("customer_routines", "name LIKE %s"),
    ("customer_contacts", "name LIKE %s"),
    ("customer_standards", "note LIKE %s"),
    ("incident_fingerprints",
     "incident_id IN (SELECT id FROM incidents WHERE title LIKE %s)"),
    ("incidents", "title LIKE %s"),
    ("work_orders", "title LIKE %s"),
    ("deliveries", "sent_by LIKE %s"),
    ("oncall_members", "name LIKE %s"),
)


def clear(echo=print):
    """샘플만 지운다. 몇 건씩 지웠는지 돌려준다."""
    import psycopg

    from app.event_store import psycopg_uri

    pattern = f"%{MARK}%"
    removed = {}
    with psycopg.connect(psycopg_uri()) as conn, conn.cursor() as cur:
        for table, where in CLEAN:
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if cur.fetchone()[0] is None:
                continue
            try:
                cur.execute(f"DELETE FROM {table} WHERE {where}", (pattern,))
                if cur.rowcount:
                    removed[table] = cur.rowcount
            except psycopg.Error as e:
                conn.rollback()
                echo(f"  {table:20} 건너뜀 — {e}")
    return removed
