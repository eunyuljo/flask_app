# app/escalation.py
# SLA 목표를 넘겼는데 아무도 안 보면 사람을 부른다.
#
# 지금까지는 위반을 세어놓고 그 다음에 아무 일도 일어나지 않았다.
# 여기서 단계를 올리고, 일정 단계에 이르면 Jira 로 넘긴다.
#
# 넣지 않은 것: 날짜 기반 당번표("이번 주 1차는 누구"). 달력과 교대 규칙이
# 따라오는데 그건 이 앱의 성격을 넘는다. level 은 당번 순번이 아니라
# 단계다 - 1차 대응자, 2차, 관리자.

from flask import current_app

from app import db


class EscalationError(Exception):
    """에스컬레이션 처리에 실패했을 때."""


# 접속 문자열은 app/db.py 가 만든다. 다른 모듈이 이 이름으로
# 가져다 쓰고 있어서 별칭으로 남긴다.
psycopg_uri = db.uri


_rows = db.rows


def _connect():
    return db.connect(EscalationError)


def _ensure(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    if cur.fetchone()[0] is None:
        raise EscalationError(
            f"{name} 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


# ----------------------------------------------------------------------
# 담당자
# ----------------------------------------------------------------------

def members(customer=None):
    """담당자 목록. customer 를 주면 그 고객사 전담 + 전체 담당을 함께 준다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "oncall_members")
        if customer is None:
            cur.execute(
                "SELECT * FROM oncall_members ORDER BY level, customer, name"
            )
        else:
            cur.execute(
                "SELECT * FROM oncall_members WHERE enabled AND customer IN (%s, '') "
                "ORDER BY level, (customer = '') ASC, name",
                (customer,),
            )
        return _rows(cur)


def add_member(name, level, slack_id="", customer=""):
    """담당자를 추가한다."""
    if not name.strip():
        raise EscalationError("이름을 입력하세요.")
    try:
        level = int(level)
    except (TypeError, ValueError):
        raise EscalationError("단계는 숫자여야 합니다.")
    if level < 1:
        raise EscalationError("단계는 1 이상이어야 합니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "oncall_members")
        cur.execute(
            "INSERT INTO oncall_members (name, level, slack_id, customer) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (name.strip(), level, slack_id.strip(), customer.strip()),
        )
        return cur.fetchone()[0]


def remove_member(member_id):
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "oncall_members")
        cur.execute(
            "DELETE FROM oncall_members WHERE id = %s RETURNING id", (member_id,)
        )
        if cur.fetchone() is None:
            raise EscalationError("담당자를 찾지 못했습니다.")


def _for_level(all_members, level):
    """이 단계에서 부를 사람. 고객사 전담이 있으면 전체 담당보다 우선한다."""
    at_level = [m for m in all_members if m["level"] == level]
    scoped = [m for m in at_level if m["customer"]]
    return scoped or at_level


# ----------------------------------------------------------------------
# 단계 판정
# ----------------------------------------------------------------------

def due_level(elapsed_minutes, target_minutes, steps):
    """지금 몇 단계까지 올라가야 하는가. 해당 없으면 0.

    steps = [0, 30, 120] 이면
      목표 초과 즉시        -> 1단계
      목표 + 30분 지나면    -> 2단계
      목표 + 120분 지나면   -> 3단계
    """
    over = elapsed_minutes - target_minutes
    if over < 0:
        return 0
    level = 0
    for index, after in enumerate(steps, start=1):
        if over >= after:
            level = index
    return level


def already_notified(account_id, fingerprint):
    """이 알람에 대해 이미 올린 단계들."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "escalations")
        cur.execute(
            "SELECT level FROM escalations "
            "WHERE account_id = %s AND fingerprint = %s",
            (account_id, fingerprint),
        )
        return {r[0] for r in cur.fetchall()}


def record(account_id, fingerprint, level, jira_key=""):
    """올린 단계를 기록한다. 같은 단계를 두 번 부르지 않기 위함이다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "escalations")
        cur.execute(
            """
            INSERT INTO escalations (account_id, fingerprint, level, jira_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (account_id, fingerprint, level) DO UPDATE
                SET notified_at = now(),
                    jira_key = COALESCE(NULLIF(EXCLUDED.jira_key, ''),
                                        escalations.jira_key)
            """,
            (account_id, fingerprint, level, jira_key),
        )


def clear(account_id, fingerprint):
    """이 알람의 에스컬레이션 기록을 지운다.

    대응이 끝난 뒤 같은 알람이 다시 나면 1단계부터 다시 올라가야 한다.
    기록이 남아 있으면 두 번째 장애 때 아무도 안 불린다.
    """
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur, "escalations")
        cur.execute(
            "DELETE FROM escalations WHERE account_id = %s AND fingerprint = %s",
            (account_id, fingerprint),
        )
        return cur.rowcount


def pending(breaches, steps):
    """위반 목록에서 '지금 올려야 하는 단계' 를 붙여 돌려준다.

    이미 올린 단계는 뺀다. 3단계까지 가야 하는데 1단계만 올렸으면
    2, 3 을 한 번에 올린다 - 중간 단계를 건너뛰면 그 사람은 자기가
    호출된 적 없다는 것도 모른 채 지나간다.
    """
    result = []
    for item in breaches:
        level = due_level(
            float(item["elapsed_minutes"]), float(item["minutes"]), steps
        )
        if level < 1:
            continue
        done = already_notified(item["account_id"], item["fingerprint"])
        todo = [n for n in range(1, level + 1) if n not in done]
        if todo:
            result.append({**item, "levels": todo, "due_level": level})
    return result


# ----------------------------------------------------------------------
# 메시지
# ----------------------------------------------------------------------

def mention(member):
    """Slack 멘션 문자열. ID 가 없으면 이름만 적는다.

    @이름 은 멘션이 걸리지 않는다. Slack 은 <@U01ABCDEF> 형식만 알아본다.
    """
    if member["slack_id"]:
        return f"<@{member['slack_id']}>"
    return member["name"]


# Slack 메시지에 실을 절차의 길이 상한.
# 런북 전문을 그대로 보내면 채널이 글로 막히고, 정작 누가 불려 나왔는지가
# 스크롤 위로 밀려 올라간다. 앞부분만 싣고 전체는 화면에서 본다.
RUNBOOK_EXCERPT = 700


def to_slack(item, level, people, runbook=None, base_url=""):
    """한 알람의 한 단계에 대한 Slack 메시지.

    runbook: app.runbook.find() 결과. 있으면 절차를 함께 싣는다.

    이 메시지를 받는 사람은 새벽에 깬 당직자다. 알람 내용만 보내면
    절차를 보려고 도구에 로그인해야 하는데, 그 순간이 가장 로그인하기
    싫은 순간이다. 런북을 만들어 두고 정작 가장 필요할 때 안 보내는 것은
    앞뒤가 안 맞는다.
    """
    from app.slack import escape

    who = " ".join(mention(m) for m in people) if people else "_(담당자 미등록)_"
    over = int(float(item["elapsed_minutes"]) - float(item["minutes"]))

    lines = [
        f"*에스컬레이션 {level}단계* — {escape(item['customer'])}",
        f"{who}",
        "",
        f"`{item['severity']}` {escape(item['sample'])}",
        f"{item['count']}건 · {escape(item['source'])} · 계정 {item['account_id']}",
        f"목표 {int(item['minutes'])}분 / 경과 *{int(item['elapsed_minutes'])}분* "
        f"(초과 {over}분)",
        "",
        "_아직 이 계정을 들여다본 기록이 없습니다(감사 로그 기준)._",
    ]

    if runbook:
        scope = f"{runbook['customer']} 전용" if runbook["customer"] else "공통"
        body = (runbook["body"] or "").strip()
        clipped = len(body) > RUNBOOK_EXCERPT
        if clipped:
            body = body[:RUNBOOK_EXCERPT].rstrip()

        lines += ["", f"*대응 절차 — {escape(runbook['title'])}* ({scope})",
                  escape(body)]
        if clipped:
            lines.append("_… 이어집니다. 전체는 런북 화면에서._")
        if base_url:
            lines.append(
                f"<{base_url.rstrip('/')}/runbook/{runbook['id']}/edit|절차 전체 보기>")
    else:
        # 절차가 없다는 것도 정보다. 불려 나온 사람이 "어딘가에 있겠지" 하고
        # 찾아 헤매는 것보다, 없다고 알려주는 편이 낫다.
        lines += ["", "_이 알람에는 등록된 대응 절차가 없습니다._"]

    return "\n".join(lines)


def to_jira(item, level, runbook=None):
    """Jira 이슈 제목과 본문.

    Jira 는 길이 제한이 Slack 만큼 빡빡하지 않고, 티켓을 받는 사람이
    나중에 열어볼 수도 있다. 절차는 잘라내지 않고 그대로 싣는다.
    """
    over = int(float(item["elapsed_minutes"]) - float(item["minutes"]))
    summary = (f"[SLA {level}단계] {item['customer']} · "
               f"{item['sample'][:80]}")
    description = "\n".join([
        f"SLA 목표를 초과했고 대응 기록이 없어 {level}단계로 넘어왔습니다.",
        "",
        f"고객사: {item['customer']}",
        f"계정: {item['account_id']}",
        f"출처: {item['source']}",
        f"심각도: {item['severity']}",
        f"알람: {item['sample']}",
        f"건수: {item['count']}",
        f"지문: {item['fingerprint']}",
        "",
        f"목표 {int(item['minutes'])}분 / 경과 {int(item['elapsed_minutes'])}분 "
        f"(초과 {over}분)",
        "",
        "대응 여부는 감사 로그(콘솔 조회·AI 진단) 기준입니다.",
        "다른 경로로 대응했다면 여기 잡히지 않습니다.",
    ])

    if runbook:
        scope = f"{runbook['customer']} 전용" if runbook["customer"] else "공통"
        description += "\n".join([
            "",
            "",
            f"h3. 대응 절차 — {runbook['title']} ({scope})",
            "",
            runbook["body"] or "",
        ])
    else:
        description += "\n\n등록된 대응 절차가 없습니다."

    return summary, description
