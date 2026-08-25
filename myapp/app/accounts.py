# app/accounts.py
# 고객사 AWS 계정 목록을 읽고 쓴다. role_arn 이 비어 있으면 '데모 계정' 으로 취급해
# 실제 AWS 를 호출하지 않는다(자격증명 없이도 화면과 흐름을 확인할 수 있게 하기 위함).

from flask import current_app

from app import db


class AccountError(Exception):
    """계정 조회/저장에 실패했을 때."""


# 접속 문자열은 app/db.py 가 만든다. 다른 모듈이 이 이름으로
# 가져다 쓰고 있어서 별칭으로 남긴다.
psycopg_uri = db.uri


_rows = db.rows


def list_accounts(enabled_only=True):
    """계정 목록. 고객사 이름 순으로 돌려준다."""
    import psycopg

    try:
        with psycopg.connect(psycopg_uri()) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.aws_accounts')")
            if cur.fetchone()[0] is None:
                raise AccountError(
                    "aws_accounts 테이블이 없습니다. flask --app run init-db 를 실행하세요."
                )
            where = "WHERE enabled" if enabled_only else ""
            cur.execute(
                f"""
                SELECT id, customer, account_id, alias, role_arn, external_id,
                       regions, enabled
                FROM aws_accounts {where}
                ORDER BY customer, account_id
                """
            )
            return _rows(cur)
    except AccountError:
        raise
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise AccountError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def get_account(account_id):
    """계정 하나를 찾는다. 없으면 None."""
    for a in list_accounts(enabled_only=False):
        if a["account_id"] == account_id:
            return a
    return None


def upsert_account(customer, account_id, alias=None, role_arn="", external_id="",
                   regions=None, enabled=True):
    """계정을 추가하거나 갱신한다."""
    import psycopg

    regions = regions or []
    with psycopg.connect(psycopg_uri()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO aws_accounts
                (customer, account_id, alias, role_arn, external_id, regions, enabled)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id) DO UPDATE SET
                customer    = EXCLUDED.customer,
                alias       = EXCLUDED.alias,
                role_arn    = EXCLUDED.role_arn,
                external_id = EXCLUDED.external_id,
                regions     = EXCLUDED.regions,
                enabled     = EXCLUDED.enabled
            RETURNING id
            """,
            (customer, account_id, alias, role_arn, external_id, regions, enabled),
        )
        return cur.fetchone()[0]


def by_customer(accounts):
    """고객사별로 묶는다. 화면의 2단 드롭다운을 만들 때 쓴다."""
    grouped = {}
    for a in accounts:
        grouped.setdefault(a["customer"], []).append(a)
    return grouped
