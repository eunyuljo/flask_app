# app/customer.py
# 고객사 하나의 현황을 한 번에 모은다.
#
# 이 앱은 지금까지 '기능 축' 으로만 나뉘어 있었다. 알람은 알람끼리,
# 작업은 작업끼리. 그런데 MSP 는 고객사 단위로 일하므로
# "A커머스 지금 어떤 상태야?" 에 답하려면 화면 여섯 개를 돌아야 했다.
#
# 알람은 events.account_id 로 묶는다. 이 열이 생기기 전에는 고객사별 알람
# 건수를 낼 수 없어서 칸을 비워뒀다. 지금도 계정을 실어 보내지 않은
# 이벤트는 account_id 가 비어 있어 어느 고객사에도 잡히지 않는다.
# 그 건수를 함께 돌려줘서, 화면이 '알람이 없다' 와 '계정을 몰라서 못 센다' 를
# 구분해 보여줄 수 있게 한다.

from flask import current_app


class CustomerError(Exception):
    """현황을 모으지 못했을 때."""


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
        raise CustomerError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise CustomerError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _has(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


# ----------------------------------------------------------------------
# 고객사 자체
# ----------------------------------------------------------------------
# 예전에는 고객사가 실체가 없었다. 목록이
#   SELECT DISTINCT customer FROM aws_accounts
# 였고, 그래서 AWS 계정을 받기 전에는 고객사를 만들 수가 없었다.
# 온보딩에서 제일 먼저 하는 일(연락처 받기, 보고 주기 정하기)이 전부
# 계정보다 앞인데 시작점이 시스템 밖에 있었다.

STATUSES = {
    "onboarding": "온보딩",
    "active": "운영 중",
    "suspended": "일시 중지",
    "ended": "종료",
}

# 목록에서 기본으로 보여줄 상태. 종료된 고객사까지 매번 섞이면
# 고를 때마다 걸러야 한다. 필요하면 include_ended 로 부른다.
LIVE_STATUSES = ("onboarding", "active", "suspended")

# 공통/기본값을 가리키는 예약 이름. 화면에는 고객사로 나오지 않는다.
# (공통 런북, 기본 SLA 목표, 전체 당직자가 customer = '' 로 저장된다)
SHARED = ""


def names(include_ended=False, with_accounts=False):
    """등록된 고객사 이름 목록.

    with_accounts: AWS 계정이 하나라도 붙은 고객사만.

    예전에는 목록이 aws_accounts 에서 파생돼서 '계정 있는 고객사' 와
    '고객사' 가 같은 말이었다. 이제는 다르다 - 온보딩 중이라 계정이 아직
    없는 고객사가 정상으로 존재한다. 리소스·SLA 처럼 계정이 있어야 뜻이
    있는 곳은 with_accounts 로 물어야 한다.
    """
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "customers"):
            raise CustomerError(
                "customers 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )
        wanted = tuple(STATUSES) if include_ended else LIVE_STATUSES
        having = (" AND EXISTS (SELECT 1 FROM aws_accounts a "
                  "              WHERE a.customer = customers.name)"
                  if with_accounts else "")
        cur.execute(
            "SELECT name FROM customers "
            f" WHERE name <> %s AND status = ANY(%s){having} ORDER BY name",
            (SHARED, list(wanted)),
        )
        return [r[0] for r in cur.fetchall()]


def listing(include_ended=True):
    """고객사 전체를 속성까지. 관리 화면이 쓴다.

    계정 수를 함께 센다. 0인 고객사가 정상이 됐다는 것이 이 표의 요점이다 -
    계약은 했는데 아직 계정을 못 받은 상태를 이제 표현할 수 있다.
    """
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "customers"):
            raise CustomerError(
                "customers 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )
        wanted = tuple(STATUSES) if include_ended else LIVE_STATUSES
        cur.execute(
            """
            SELECT c.name, c.status, c.started_at, c.ended_at,
                   c.report_interval_days, c.note, c.created_at,
                   (SELECT count(*) FROM aws_accounts a
                     WHERE a.customer = c.name) AS accounts
              FROM customers c
             WHERE c.name <> %s AND c.status = ANY(%s)
             ORDER BY array_position(ARRAY['onboarding','active','suspended','ended'],
                                     c.status), c.name
            """,
            (SHARED, list(wanted)),
        )
        return _rows(cur)


def get(name):
    """고객사 하나. 없으면 None."""
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "customers"):
            raise CustomerError("customers 테이블이 없습니다.")
        cur.execute(
            "SELECT name, status, started_at, ended_at, report_interval_days, "
            "       note, created_at, updated_at "
            "  FROM customers WHERE name = %s",
            (name,),
        )
        rows = _rows(cur)
    return rows[0] if rows else None


def _clean(name):
    name = (name or "").strip()
    if not name:
        raise CustomerError("고객사 이름을 입력하세요.")
    if name == SHARED:
        raise CustomerError("빈 이름은 공통 항목이 쓰는 예약된 이름입니다.")
    return name


def create(name, status="onboarding", started_at=None,
           report_interval_days=0, note=""):
    """고객사를 만든다. 계정이 없어도 만들 수 있다 - 그게 요점이다."""
    name = _clean(name)
    if status not in STATUSES:
        raise CustomerError(f"알 수 없는 상태입니다: {status}")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, status, started_at, "
            "                       report_interval_days, note) "
            " VALUES (%s, %s, %s, %s, %s) "
            " ON CONFLICT (name) DO NOTHING RETURNING name",
            (name, status, started_at, int(report_interval_days or 0),
             (note or "").strip()),
        )
        if cur.fetchone() is None:
            raise CustomerError(f"이미 있는 고객사입니다: {name}")
    return name


def update(name, fields):
    """상태·시작일·보고 주기·메모를 고친다. 이름은 rename 으로 바꾼다.

    fields 를 **kwargs 가 아니라 dict 로 받는다. kwargs 로 두면
    update(name, name="다른이름") 이 위치 인자와 부딪혀 TypeError 가 나고,
    허용 목록 검사가 아예 돌지 않는다 - 막아야 할 키가 검사보다 먼저
    걸리는 셈이다. app/incident.py 의 update_narrative 도 같은 모양이다.
    """
    allowed = ("status", "started_at", "ended_at", "report_interval_days", "note")
    unknown = set(fields) - set(allowed)
    if unknown:
        raise CustomerError(f"고칠 수 없는 항목입니다: {', '.join(sorted(unknown))}")
    if not fields:
        return
    if fields.get("status") and fields["status"] not in STATUSES:
        raise CustomerError(f"알 수 없는 상태입니다: {fields['status']}")

    assignments = ", ".join(f"{k} = %s" for k in fields)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE customers SET {assignments}, updated_at = now() "
            f" WHERE name = %s AND name <> %s RETURNING name",
            list(fields.values()) + [name, SHARED],
        )
        if cur.fetchone() is None:
            raise CustomerError("그런 고객사가 없습니다.")


def rename(old, new):
    """이름을 바꾼다.

    외래키가 ON UPDATE CASCADE 라 12개 표의 고객사 이름이 함께 따라온다.
    예전 구조였다면 표마다 UPDATE 를 돌려야 했고, 하나라도 빠뜨리면
    그 표의 자료가 조용히 고아가 됐다.
    """
    new = _clean(new)
    if old == SHARED:
        raise CustomerError("예약된 이름은 바꿀 수 없습니다.")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM customers WHERE name = %s", (new,))
        if cur.fetchone() and old != new:
            raise CustomerError(f"이미 있는 이름입니다: {new}")
        cur.execute(
            "UPDATE customers SET name = %s, updated_at = now() "
            " WHERE name = %s RETURNING name",
            (new, old),
        )
        if cur.fetchone() is None:
            raise CustomerError("그런 고객사가 없습니다.")
    return new


def delete(name):
    """고객사를 지운다.

    자료가 하나라도 남아 있으면 DB 가 거부한다(ON DELETE RESTRICT).
    조용히 고아를 만드는 것보다 지우지 못하는 편이 낫다 - 실제로 그렇게
    생긴 고아가 audit_log 에 남아 있었다.
    """
    import psycopg

    if name == SHARED:
        raise CustomerError("예약된 이름은 지울 수 없습니다.")
    with _connect() as conn, conn.cursor() as cur:
        try:
            cur.execute("DELETE FROM customers WHERE name = %s RETURNING name",
                        (name,))
        except psycopg.errors.ForeignKeyViolation as e:
            raise CustomerError(
                "이 고객사에 딸린 자료가 있어 지울 수 없습니다. "
                "종료 상태로 바꾸는 것을 권합니다."
            ) from e
        if cur.fetchone() is None:
            raise CustomerError("그런 고객사가 없습니다.")


def overview(customer):
    """고객사 하나의 현황."""
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "aws_accounts"):
            raise CustomerError(
                "aws_accounts 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )

        # 테이블 존재 확인을 먼저 다 해둔다. _has 도 같은 커서로 질의하므로,
        # 본 질의 사이에 끼우면 앞의 결과를 덮어쓴다.
        has_snapshots = _has(cur, "resource_snapshots")
        has_work = _has(cur, "work_orders")
        has_incidents = _has(cur, "incidents")
        has_runbooks = _has(cur, "runbooks")

        # ---- 계정 + 마지막 수집 시각 ----
        # 수집이 오래됐으면 리소스 변경도 RCA 도 전부 못 미더운 상태다.
        # 그래서 계정 옆에 바로 붙여둔다.
        if has_snapshots:
            cur.execute(
                """
                SELECT a.account_id, a.alias, a.regions, a.enabled,
                       (a.role_arn = '') AS is_demo,
                       s.last_collected
                  FROM aws_accounts a
                  LEFT JOIN (
                        SELECT account_id, max(collected_at) AS last_collected
                          FROM resource_snapshots WHERE complete GROUP BY account_id
                       ) s ON s.account_id = a.account_id
                 WHERE a.customer = %s
                 ORDER BY a.account_id
                """,
                (customer,),
            )
        else:
            cur.execute(
                """
                SELECT account_id, alias, regions, enabled,
                       (role_arn = '') AS is_demo, NULL AS last_collected
                  FROM aws_accounts WHERE customer = %s ORDER BY account_id
                """,
                (customer,),
            )
        accounts = _rows(cur)

        # ---- 진행 중인 작업 ----
        works = []
        if has_work:
            cur.execute(
                """
                SELECT id, ticket, title, account_id, region, operator, status, created_at
                  FROM work_orders
                 WHERE customer = %s AND status <> 'closed'
                 ORDER BY created_at
                """,
                (customer,),
            )
            works = _rows(cur)

        # ---- 장애 ----
        incidents, unsent = [], 0
        if has_incidents:
            cur.execute(
                """
                SELECT id, title, severity, started_at, ended_at,
                       status, customer_status
                  FROM incidents
                 WHERE customer = %s
                 ORDER BY started_at DESC
                 LIMIT 10
                """,
                (customer,),
            )
            incidents = _rows(cur)
            # 고객 제출본을 아직 안 낸 장애. 이게 밀리면 곧 독촉이 온다.
            cur.execute(
                "SELECT count(*) FROM incidents "
                "WHERE customer = %s AND customer_status <> 'sent'",
                (customer,),
            )
            unsent = cur.fetchone()[0]

        # ---- 알람 ----
        # 이 고객사 계정들에서 난 이벤트만 센다.
        alarms = {"total": 0, "by_severity": {}, "hours": 24}
        account_ids = [a["account_id"] for a in accounts]
        if account_ids:
            cur.execute(
                """
                SELECT severity, count(*)
                  FROM events
                 WHERE account_id = ANY(%s)
                   AND occurred_at >= now() - interval '24 hours'
                 GROUP BY severity
                """,
                (account_ids,),
            )
            alarms["by_severity"] = dict(cur.fetchall())
            alarms["total"] = sum(alarms["by_severity"].values())

        # 계정을 모르는 이벤트. 이 값이 크면 위 숫자를 믿을 수 없다.
        cur.execute(
            "SELECT count(*) FROM events "
            "WHERE account_id = '' AND occurred_at >= now() - interval '24 hours'"
        )
        alarms["unattributed"] = cur.fetchone()[0]

        # ---- 런북 ----
        runbooks = {"scoped": 0, "common": 0}
        if has_runbooks:
            cur.execute(
                "SELECT count(*) FILTER (WHERE customer = %s), "
                "       count(*) FILTER (WHERE customer = '') FROM runbooks",
                (customer,),
            )
            runbooks["scoped"], runbooks["common"] = cur.fetchone()

    return {
        "customer": customer,
        "accounts": accounts,
        "account_count": len(accounts),
        "demo_count": sum(1 for a in accounts if a["is_demo"]),
        "works": works,
        "incidents": incidents,
        "unsent": unsent,
        "runbooks": runbooks,
        "alarms": alarms,
    }
