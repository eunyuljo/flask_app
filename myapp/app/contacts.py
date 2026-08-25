# app/contacts.py
# 고객사 쪽 사람. 우리가 연락하는 상대다.
#
# ── oncall_members 와 헷갈리면 안 된다 ─────────────────────────────
# 저쪽은 우리 쪽 당직자(호출을 받는 사람)이고, 여기는 고객사 담당자
# (우리가 연락하는 사람)다. 방향이 반대라 표를 합치면 반드시 섞인다.
#
# ── 개인정보라서 ───────────────────────────────────────────────────
# 이름·이메일·전화번호가 들어간다. 리포트(엑셀/PPT)에 싣지 않는다.
# 고객사에 나가는 산출물에 그 고객사 담당자 연락처를 넣을 이유가 없다.

from flask import current_app

from app import db

KINDS = {
    "primary": "기술 담당",
    "report": "보고 수신",
    "emergency": "긴급 연락",
    "approver": "승인 권한",
}

# 화면에 보여줄 순서. 급할 때 먼저 찾는 것이 위로 온다.
KIND_ORDER = ("emergency", "primary", "approver", "report")


class ContactError(Exception):
    """연락처를 읽거나 쓰는 데 실패했을 때."""


# 접속 문자열은 app/db.py 가 만든다. 다른 모듈이 이 이름으로
# 가져다 쓰고 있어서 별칭으로 남긴다.
psycopg_uri = db.uri


_rows = db.rows


def _connect():
    return db.connect(ContactError)


def _ensure(cur):
    cur.execute("SELECT to_regclass('public.customer_contacts')")
    if cur.fetchone()[0] is None:
        raise ContactError(
            "customer_contacts 테이블이 없습니다. "
            "flask --app run init-db 를 실행하세요."
        )


def add(customer, name, kind, email="", phone="", note=""):
    """연락처를 등록한다. 같은 고객사·이름·역할이면 갱신한다."""
    customer, name = (customer or "").strip(), (name or "").strip()
    if not customer:
        raise ContactError("고객사를 고르세요.")
    if not name:
        raise ContactError("이름을 입력하세요.")
    if kind not in KINDS:
        raise ContactError(f"알 수 없는 역할입니다: {kind}")

    email, phone = (email or "").strip(), (phone or "").strip()
    if not email and not phone:
        # 연락 수단이 없는 연락처는 연락처가 아니다.
        raise ContactError("이메일이나 전화번호 중 하나는 있어야 합니다.")

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            """
            INSERT INTO customer_contacts (customer, name, kind, email, phone, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (customer, name, kind) DO UPDATE SET
                email  = EXCLUDED.email,
                phone  = EXCLUDED.phone,
                note   = EXCLUDED.note,
                active = true
            RETURNING id
            """,
            (customer, name, kind, email, phone, (note or "").strip()),
        )
        return cur.fetchone()[0]


def set_active(contact_id, active):
    """퇴사·인사이동. 지우지 않는 이유는 지난 발송 기록에 남은 이름이
    누구였는지 확인할 수 있어야 하기 때문이다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            "UPDATE customer_contacts SET active = %s WHERE id = %s RETURNING id",
            (bool(active), contact_id),
        )
        if cur.fetchone() is None:
            raise ContactError("연락처를 찾지 못했습니다.")


def listing(customer, include_inactive=False):
    """이 고객사의 연락처. 급할 때 먼저 찾는 역할이 위로 온다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        sql = "SELECT * FROM customer_contacts WHERE customer = %s"
        params = [customer]
        if not include_inactive:
            sql += " AND active"
        cur.execute(sql + " ORDER BY name", params)
        items = _rows(cur)

    order = {k: i for i, k in enumerate(KIND_ORDER)}
    items.sort(key=lambda c: (order.get(c["kind"], len(order)), c["name"]))
    return items


def for_kind(customer, kind):
    """이 역할의 연락처들. 발송이 '누구에게' 를 물을 때 쓴다."""
    return [c for c in listing(customer) if c["kind"] == kind]


def counts(customers):
    """{고객사: {역할: 수}}. 여러 고객사를 한 번에 볼 때 쓴다.

    고객사마다 listing() 을 부르면 고객사 수만큼 질의가 나간다.
    """
    if not customers:
        return {}
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            "SELECT customer, kind, count(*) AS n FROM customer_contacts "
            " WHERE customer = ANY(%s) AND active GROUP BY customer, kind",
            (list(customers),),
        )
        out = {}
        for row in _rows(cur):
            out.setdefault(row["customer"], {})[row["kind"]] = row["n"]
        return out
