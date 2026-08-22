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


def names():
    """등록된 고객사 이름 목록."""
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "aws_accounts"):
            raise CustomerError(
                "aws_accounts 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )
        cur.execute("SELECT DISTINCT customer FROM aws_accounts ORDER BY customer")
        return [r[0] for r in cur.fetchall()]


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
