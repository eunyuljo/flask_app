# app/resources.py
# 리소스 스냅샷을 저장하고, 두 스냅샷을 비교해 무엇이 바뀌었는지 뽑아내는 모듈.
# AWS 호출은 여기 없다. 수집기(app/cli.py)가 만든 자료를 받아 저장/비교만 한다.

import hashlib
import json

from flask import current_app


class ResourceError(Exception):
    """DB 가 없거나 스냅샷이 부족할 때."""


# ----------------------------------------------------------------------
# 정규화
# ----------------------------------------------------------------------
# 해시를 만들기 전에 '매번 달라지지만 의미는 없는' 값을 걷어낸다.
# 이걸 안 하면 아무것도 안 바뀐 날에도 전부 "변경됨"으로 떠서 diff 가 쓸모없어진다.
# 실무에서 스냅샷 비교가 실패하는 가장 흔한 이유가 이 정규화 누락이다.
NOISY_KEYS = {
    "lastmodified", "lastmodifiedtime", "lastupdated", "lastupdatedtime",
    "requestid", "responsemetadata", "clienttoken", "nexttoken",
    "launchtime", "createdate", "creationdate", "creationtime",
}


def normalize(attributes):
    """해시 계산용으로 속성을 다듬는다.

    - 의미 없는 키를 뺀다
    - 리스트는 정렬한다 (AWS 가 순서를 보장하지 않는 경우가 많다.
      순서만 바뀌어도 '변경'으로 잡히면 안 된다)
    - 중첩된 값도 같은 규칙을 재귀적으로 적용한다
    """
    if isinstance(attributes, dict):
        return {
            k: normalize(v)
            for k, v in sorted(attributes.items())
            if k.lower() not in NOISY_KEYS
        }
    if isinstance(attributes, list):
        # 정렬 기준을 문자열로 통일한다. 요소가 dict 여도 정렬이 가능해진다.
        return sorted((normalize(v) for v in attributes),
                      key=lambda x: json.dumps(x, sort_keys=True, ensure_ascii=False))
    return attributes


def digest_of(attributes):
    """정규화한 속성의 해시. 두 스냅샷에서 이 값이 같으면 '안 바뀐 것'이다."""
    payload = json.dumps(normalize(attributes), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def psycopg_uri():
    return current_app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


# ----------------------------------------------------------------------
# 저장
# ----------------------------------------------------------------------
def save_snapshot(uri, items, account_id, region, source="demo", note=None,
                  collected_types=None):
    """수집 결과를 스냅샷 하나로 저장한다.

    items: [{"resource_id":..., "resource_type":..., "attributes": {...}}, ...]
    collected_types: 이번에 훑은 리소스 종류. 안 주면 app/collect.py 의
        기본 목록을 쓴다.

        이걸 남겨두는 이유는 "봤는데 없음" 과 "안 봤음" 을 구분하기
        위해서다. 둘 다 리소스가 0건이라 저장된 결과만 봐서는 똑같다.

    complete 플래그를 마지막에 켠다. 도중에 실패하면 false 로 남아
    비교 대상에서 자동으로 빠진다.
    """
    import psycopg

    if collected_types is None:
        from app.collect import COLLECTED_TYPES

        collected_types = COLLECTED_TYPES

    with psycopg.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO resource_snapshots "
                "  (account_id, region, source, note, collected_types) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING snapshot_id",
                (account_id, region, source, note, list(collected_types)),
            )
            snapshot_id = cur.fetchone()[0]

            cur.executemany(
                "INSERT INTO resources (snapshot_id, resource_id, resource_type, attributes, digest) "
                "VALUES (%s, %s, %s, %s, %s)",
                [
                    (
                        snapshot_id,
                        it["resource_id"],
                        it["resource_type"],
                        json.dumps(it["attributes"], ensure_ascii=False),
                        digest_of(it["attributes"]),
                    )
                    for it in items
                ],
            )

            # 여기까지 왔으면 전부 성공한 것이다.
            cur.execute(
                "UPDATE resource_snapshots SET complete = true WHERE snapshot_id = %s",
                (snapshot_id,),
            )
    return snapshot_id


# ----------------------------------------------------------------------
# 조회 / 비교
# ----------------------------------------------------------------------
def list_snapshots(uri, limit=20, account_id=None, region=None):
    import psycopg

    where, params = [], []
    if account_id:
        where.append("s.account_id = %s")
        params.append(account_id)
    if region:
        where.append("s.region = %s")
        params.append(region)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    with psycopg.connect(uri) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.snapshot_id, s.collected_at, s.account_id, s.region,
                   s.source, s.complete, count(r.resource_id) AS resource_count
            FROM resource_snapshots s
            LEFT JOIN resources r USING (snapshot_id)
            {where_sql}
            GROUP BY s.snapshot_id
            ORDER BY s.snapshot_id DESC
            LIMIT %s
            """,
            params,
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _field_diff(old_attrs, new_attrs):
    """바뀐 필드만 골라낸다. 화면에 '이전 값 -> 현재 값' 을 보여주기 위한 것.

    노이즈 키는 여기서도 빼야 한다.
    digest 는 정규화된 dict 로 계산하므로 노이즈 키를 이미 무시하는데,
    이 함수는 키를 하나씩 비교하기 때문에 따로 걸러주지 않으면
    "탐지는 무시했는데 화면에는 보이는" 불일치가 생긴다.
    """
    keys = sorted(
        k for k in (set(old_attrs) | set(new_attrs))
        if k.lower() not in NOISY_KEYS
    )
    changes = []
    for k in keys:
        before = old_attrs.get(k)
        after = new_attrs.get(k)
        # 정규화한 값으로 비교한다. 리스트 순서만 다른 것은 변경이 아니다.
        if normalize(before) != normalize(after):
            changes.append({"field": k, "before": before, "after": after})
    return changes


def diff(uri, base_id=None, target_id=None, account_id=None, region=None):
    """두 스냅샷을 비교한다.

    account_id / region 을 주면 그 범위 안에서만 최근 2개를 고른다.
    이 조건이 없으면 서로 다른 계정의 스냅샷을 비교하게 되어,
    A 계정 리소스는 전부 '삭제', B 계정 리소스는 전부 '생성' 으로 나온다.
    계정이 하나뿐일 때는 티가 안 나지만 계정이 늘면 즉시 깨진다.
    """
    import psycopg

    with psycopg.connect(uri) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.resources')")
        if cur.fetchone()[0] is None:
            raise ResourceError(
                "resources 테이블이 없습니다. flask --app run init-db 를 실행하세요."
            )

        if base_id is None or target_id is None:
            # complete = true 인 것만 고른다.
            # 수집 실패한 스냅샷과 비교하면 멀쩡한 리소스가 '삭제됨' 으로 나온다.
            where = ["complete"]
            params = []
            if account_id:
                where.append("account_id = %s")
                params.append(account_id)
            if region:
                where.append("region = %s")
                params.append(region)

            cur.execute(
                "SELECT snapshot_id FROM resource_snapshots "
                f"WHERE {' AND '.join(where)} ORDER BY snapshot_id DESC LIMIT 2",
                params,
            )
            ids = [r[0] for r in cur.fetchall()]
            if len(ids) < 2:
                scope = ""
                if account_id or region:
                    scope = f" ({account_id or '전체 계정'} / {region or '전체 리전'})"
                raise ResourceError(
                    f"비교하려면 완료된 스냅샷이 2개 이상 필요합니다{scope} "
                    f"(현재 {len(ids)}개).\n"
                    "flask --app run collect-resources --demo 를 두 번 실행해 보세요."
                )
            target_id, base_id = ids[0], ids[1]

        # 직접 지정한 경우에도 계정이 섞이지 않았는지 확인한다.
        cur.execute(
            "SELECT snapshot_id, account_id, region FROM resource_snapshots "
            "WHERE snapshot_id = ANY(%s)",
            ([base_id, target_id],),
        )
        scopes = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        if len(scopes) == 2 and scopes.get(base_id) != scopes.get(target_id):
            b, t = scopes.get(base_id), scopes.get(target_id)
            raise ResourceError(
                "서로 다른 계정/리전의 스냅샷은 비교할 수 없습니다.\n"
                f"  #{base_id}: {b[0]} / {b[1]}\n"
                f"  #{target_id}: {t[0]} / {t[1]}\n"
                "같은 계정과 리전 안에서 비교해야 의미가 있습니다."
            )

        # 생성/삭제/변경을 한 번의 FULL OUTER JOIN 으로 뽑는다.
        # digest 비교라서 JSONB 전체를 맞대보지 않아도 된다.
        cur.execute(
            """
            SELECT
                COALESCE(a.resource_id, b.resource_id)     AS resource_id,
                COALESCE(a.resource_type, b.resource_type) AS resource_type,
                CASE WHEN a.resource_id IS NULL THEN 'added'
                     WHEN b.resource_id IS NULL THEN 'removed'
                     ELSE 'modified' END                   AS change,
                a.attributes AS before_attrs,
                b.attributes AS after_attrs
            FROM      (SELECT * FROM resources WHERE snapshot_id = %s) a
            FULL OUTER JOIN
                      (SELECT * FROM resources WHERE snapshot_id = %s) b
                 USING (resource_id, resource_type)
            WHERE a.resource_id IS NULL
               OR b.resource_id IS NULL
               OR a.digest <> b.digest
            ORDER BY 3, 2, 1
            """,
            (base_id, target_id),
        )
        rows = cur.fetchall()

        cur.execute(
            "SELECT snapshot_id, collected_at, account_id, region, source "
            "FROM resource_snapshots WHERE snapshot_id = ANY(%s)",
            ([base_id, target_id],),
        )
        meta = {r[0]: {"snapshot_id": r[0], "collected_at": r[1], "account_id": r[2],
                       "region": r[3], "source": r[4]} for r in cur.fetchall()}

        cur.execute(
            "SELECT snapshot_id, count(*) FROM resources "
            "WHERE snapshot_id = ANY(%s) GROUP BY 1",
            ([base_id, target_id],),
        )
        counts = dict(cur.fetchall())

    changes = []
    for resource_id, resource_type, change, before, after in rows:
        item = {
            "resource_id": resource_id,
            "resource_type": resource_type,
            "change": change,
            "before": before,
            "after": after,
            "fields": _field_diff(before or {}, after or {}) if change == "modified" else [],
        }
        changes.append(item)

    summary = {
        "added": sum(1 for c in changes if c["change"] == "added"),
        "removed": sum(1 for c in changes if c["change"] == "removed"),
        "modified": sum(1 for c in changes if c["change"] == "modified"),
    }

    return {
        "base": meta.get(base_id, {"snapshot_id": base_id}),
        "target": meta.get(target_id, {"snapshot_id": target_id}),
        "base_count": counts.get(base_id, 0),
        "target_count": counts.get(target_id, 0),
        "changes": changes,
        "summary": summary,
    }
