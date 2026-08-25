# app/delivery.py
# 무엇을, 누구에게, 어떤 경로로 보냈나.
#
# ── 왜 필요했나 ────────────────────────────────────────────────────
# "고객사에 냈다" 는 incidents.customer_status 한 칸이 전부였다. 나중에
# "그거 받으셨나요" 를 물으면 답할 근거가 없었고, 월간 리뷰는 아예
# 발송이라는 개념이 없어서 파일만 만들어졌다.
#
# ── 다운로드는 발송이 아니다 ───────────────────────────────────────
# 파일을 내려받은 것을 발송으로 세면, 확인하려고 열어본 것도 전부
# 발송이 된다. 그러면 "보냈다" 는 기록이 아무 뜻도 없어진다.
# 사람이 눌러야 한 줄이 생긴다(런북 실행 기록과 같은 판단).

from flask import current_app

from app import db

KINDS = {
    "incident": "장애 보고서",
    "msr": "월간 리뷰",
    "report": "기간 리포트",
    "other": "그 밖",
}

CHANNELS = {
    "email": "이메일",
    "slack": "Slack",
    "jira": "Jira",
    "hand": "직접 전달·구두",
}


class DeliveryError(Exception):
    """발송 기록을 읽거나 쓰는 데 실패했을 때."""


# 접속 문자열은 app/db.py 가 만든다. 다른 모듈이 이 이름으로
# 가져다 쓰고 있어서 별칭으로 남긴다.
psycopg_uri = db.uri


_rows = db.rows


def _connect():
    return db.connect(DeliveryError)


def _ensure(cur):
    cur.execute("SELECT to_regclass('public.deliveries')")
    if cur.fetchone()[0] is None:
        raise DeliveryError(
            "deliveries 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


def record(customer, kind, channel, sent_by, ref="", title="",
           recipients="", note=""):
    """보냈다는 기록을 남긴다.

    recipients 는 그때의 값을 그대로 받는다. customer_contacts 를 참조만
    하면 담당자가 바뀐 뒤에 지난 기록이 새 사람 이름으로 보인다.
    """
    customer = (customer or "").strip()
    if not customer:
        raise DeliveryError("고객사를 고르세요.")
    if kind not in KINDS:
        raise DeliveryError(f"알 수 없는 산출물입니다: {kind}")
    if channel not in CHANNELS:
        raise DeliveryError(f"알 수 없는 경로입니다: {channel}")

    recipients = (recipients or "").strip()
    if not recipients:
        # 누구에게 보냈는지 없는 발송 기록은 나중에 아무 답도 못 해준다.
        raise DeliveryError("받는 사람을 적으세요.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            """
            INSERT INTO deliveries
                (customer, kind, ref, title, channel, recipients, note, sent_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (customer, kind, str(ref or "").strip(), (title or "").strip()[:200],
             channel, recipients[:500], (note or "").strip(), sent_by or ""),
        )
        return cur.fetchone()[0]


def recent(customer="", kind="", limit=100):
    """발송 기록. 최근 순."""
    where, params = [], []
    if customer:
        where.append("customer = %s")
        params.append(customer)
    if kind in KINDS:
        where.append("kind = %s")
        params.append(kind)

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            "SELECT * FROM deliveries"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY sent_at DESC LIMIT %s",
            params + [limit],
        )
        return _rows(cur)


def for_ref(kind, ref):
    """이 산출물을 보낸 기록. 상세 화면에서 '보냈나?' 를 묻는다.

    표가 없으면 빈 목록이다. 발송 기록을 못 읽는다고 보고서 상세가
    안 뜨면 곤란하다.
    """
    try:
        with _connect() as conn, conn.cursor() as cur:
            _ensure(cur)
            cur.execute(
                "SELECT * FROM deliveries WHERE kind = %s AND ref = %s "
                " ORDER BY sent_at DESC",
                (kind, str(ref)),
            )
            return _rows(cur)
    except DeliveryError:
        return []


def suggest_recipients(customer):
    """이 고객사의 보고 수신자. 발송 폼의 기본값으로 쓴다.

    연락처를 못 읽어도 발송 기록은 남길 수 있어야 한다. 손으로 적으면
    되는 일이고, 여기서 막으면 기록 자체가 안 남는다.
    """
    from app import contacts
    from app.contacts import ContactError

    try:
        people = contacts.for_kind(customer, "report") or \
                 contacts.for_kind(customer, "primary")
    except ContactError:
        return ""

    parts = []
    for person in people:
        target = person["email"] or person["phone"]
        parts.append(f"{person['name']} <{target}>" if target else person["name"])
    return ", ".join(parts)


def summary(days=90):
    """고객사별로 마지막에 무엇을 언제 보냈나."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            """
            SELECT customer,
                   count(*)                                  AS total,
                   max(sent_at)                              AS last_at,
                   count(*) FILTER (WHERE kind = 'msr')      AS msr,
                   count(*) FILTER (WHERE kind = 'incident') AS incident
              FROM deliveries
             WHERE sent_at >= now() - make_interval(days => %s)
             GROUP BY customer
             ORDER BY last_at DESC
            """,
            (days,),
        )
        return {"days": days, "rows": _rows(cur)}
