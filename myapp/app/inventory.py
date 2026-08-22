# app/inventory.py
# 지금 무엇이 떠 있는가.
#
# ── 왜 따로 필요한가 ────────────────────────────────────────────────
# 리소스 화면(app/resources.py)은 두 시점의 '차이' 를 보여준다. 그건
# "무엇이 바뀌었나" 에는 답하지만 "무엇이 있나" 에는 답하지 않는다.
#
# 인수인계, 감사, 비용 대화에서 매번 요구되는 것은 뒤쪽이다.
#   "이 계정에 인스턴스가 몇 대죠?"
#   "Env 태그가 없는 리소스 목록 주세요"
#   "prod 로 표시된 것만 뽑아 주세요"
#
# 새로 수집할 것은 없다. resources 테이블에 이미 다 들어 있다.
#
# ── 계정+리전마다 최신 스냅샷 하나 ──────────────────────────────────
# '지금' 은 각 계정+리전의 가장 최근 완료 스냅샷이다. 여러 스냅샷을
# 합치면 이미 지워진 리소스가 살아 있는 것처럼 보인다.

from flask import current_app

# 태그를 붙일 수 있는 리소스인지 판단할 때 쓴다.
# attributes 에 tags 키가 아예 없으면 태그를 못 다는 종류로 본다.
TAG_KEY = "tags"


class InventoryError(Exception):
    """목록을 읽지 못했을 때."""


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
        raise InventoryError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise InventoryError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _has(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


def current(account_id=None, region=None, resource_type=None, q="",
            missing_tag="", limit=500):
    """지금 떠 있는 리소스.

    필터는 전부 선택이다. 값은 모두 %s 로 나가므로 SQL 본문에
    사용자 입력이 들어가지 않는다.

    q: 리소스 ID 와 속성 전체에서 찾는다. 태그 값으로도 걸리게
       하려고 attributes 를 통째로 문자열로 보고 찾는다. 정교하지는
       않지만 "prod 라고 적힌 것 다 줘" 같은 요청에 바로 답한다.
    missing_tag: 이 태그가 없는 것만. 비용 배분과 담당자 확인에서
       가장 자주 나오는 질문이다.
    """
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "resources"):
            raise InventoryError("resources 테이블이 없습니다.")

        # 계정+리전마다 최신 완료 스냅샷 하나씩.
        where, params = ["s.complete"], []
        if account_id:
            where.append("s.account_id = %s")
            params.append(account_id)
        if region:
            where.append("s.region = %s")
            params.append(region)

        cur.execute(
            f"""
            SELECT DISTINCT ON (s.account_id, s.region)
                   s.snapshot_id, s.account_id, s.region, s.collected_at
              FROM resource_snapshots s
             WHERE {' AND '.join(where)}
             ORDER BY s.account_id, s.region, s.collected_at DESC
            """,
            params,
        )
        snapshots = _rows(cur)
        if not snapshots:
            return {"items": [], "snapshots": [], "total": 0, "truncated": False}

        ids = [s["snapshot_id"] for s in snapshots]
        where2, params2 = ["r.snapshot_id = ANY(%s)"], [ids]
        if resource_type:
            where2.append("r.resource_type = %s")
            params2.append(resource_type)
        if q:
            # attributes::text 로 태그 값까지 함께 훑는다.
            where2.append("(r.resource_id ILIKE %s OR r.attributes::text ILIKE %s)")
            params2 += [f"%{q}%", f"%{q}%"]
        if missing_tag:
            # 태그를 가질 수 있는 리소스 중에서, 그 태그가 없거나 빈 것.
            where2.append(
                "(r.attributes ? 'tags' "
                " AND COALESCE(r.attributes -> 'tags' ->> %s, '') = '')"
            )
            params2.append(missing_tag)

        cur.execute(
            f"""
            SELECT r.resource_id, r.resource_type, r.attributes,
                   s.account_id, s.region, s.collected_at
              FROM resources r
              JOIN resource_snapshots s USING (snapshot_id)
             WHERE {' AND '.join(where2)}
             ORDER BY s.account_id, r.resource_type, r.resource_id
             LIMIT %s
            """,
            params2 + [limit + 1],
        )
        items = _rows(cur)

        # 전체 개수는 따로 센다. 잘라낸 목록의 길이를 총계로 보여주면
        # "이게 전부" 로 읽힌다.
        cur.execute(
            f"""
            SELECT count(*) FROM resources r
              JOIN resource_snapshots s USING (snapshot_id)
             WHERE {' AND '.join(where2)}
            """,
            params2,
        )
        total = cur.fetchone()[0]

    truncated = len(items) > limit
    return {
        "items": items[:limit],
        "snapshots": snapshots,
        "total": total,
        "truncated": truncated,
    }


def facets(account_id=None, region=None):
    """필터에 쓸 값 목록. 종류와 태그 키.

    태그 키를 코드에 적어두면 고객사마다 다른 태그 규칙을 못 따라간다.
    실제로 들어 있는 것을 뽑아서 보여준다.
    """
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "resources"):
            raise InventoryError("resources 테이블이 없습니다.")

        where, params = ["s.complete"], []
        if account_id:
            where.append("s.account_id = %s")
            params.append(account_id)
        if region:
            where.append("s.region = %s")
            params.append(region)

        cur.execute(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (s.account_id, s.region) s.snapshot_id
                  FROM resource_snapshots s
                 WHERE {' AND '.join(where)}
                 ORDER BY s.account_id, s.region, s.collected_at DESC
            )
            SELECT r.resource_type, count(*) AS n
              FROM resources r
             WHERE r.snapshot_id IN (SELECT snapshot_id FROM latest)
             GROUP BY r.resource_type
             ORDER BY n DESC
            """,
            params,
        )
        types = _rows(cur)

        cur.execute(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (s.account_id, s.region) s.snapshot_id
                  FROM resource_snapshots s
                 WHERE {' AND '.join(where)}
                 ORDER BY s.account_id, s.region, s.collected_at DESC
            )
            SELECT DISTINCT jsonb_object_keys(r.attributes -> 'tags') AS key
              FROM resources r
             WHERE r.snapshot_id IN (SELECT snapshot_id FROM latest)
               AND r.attributes ? 'tags'
             ORDER BY key
            """,
            params,
        )
        tag_keys = [r["key"] for r in _rows(cur)]

    return {"types": types, "tag_keys": tag_keys}


def scopes():
    """고를 수 있는 계정+리전 목록."""
    with _connect() as conn, conn.cursor() as cur:
        if not _has(cur, "resource_snapshots"):
            raise InventoryError("resource_snapshots 테이블이 없습니다.")
        cur.execute(
            """
            SELECT DISTINCT ON (account_id, region)
                   account_id, region, collected_at
              FROM resource_snapshots
             WHERE complete
             ORDER BY account_id, region, collected_at DESC
            """
        )
        return _rows(cur)
