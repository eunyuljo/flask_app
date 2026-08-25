# app/standards.py
# 이 고객사와 합의한 구성은 무엇인가.
#
# ── 컴플라이언스와 무엇이 다른가 ───────────────────────────────────
# app/compliance.py 는 '누구에게나 통하는 모범사례' 다(퍼블릭 RDS, 열린
# 22번 포트 …). 여기는 '이 고객사와 합의한 것' 이다. 필수 태그도, 쓸 수
# 있는 인스턴스 타입도 고객사마다 다르다.
#
# 실제로 compliance.REQUIRED_TAGS 가 ("Name", "Env") 로 코드에 박혀
# 있었다. 고객사가 하나일 때는 문제가 없지만 둘째부터 맞지 않는다.
# 그래서 고객사 표준이 있으면 그쪽이 이긴다 - 런북·SLA·알람 규칙에서
# 반복해 온 '고객사 전용이 기본값을 이긴다' 와 같다.
#
# ── 평가할 수 있는 것만 넣는다 ─────────────────────────────────────
# 규칙 종류를 자유 문자열로 열어두면 "인스턴스는 전부 백업 대상이어야
# 한다" 같은 규칙을 적을 수 있는데, 수집기가 백업 정보를 담지 않으므로
# 영원히 위반 0건으로 보인다. 그건 '지키고 있다' 가 아니라 '볼 수가
# 없다' 다. 화면에서는 똑같이 0 으로 보이므로 아예 못 적게 막는다.

import re

from flask import current_app

from app import db


class StandardError(Exception):
    """구성 표준을 읽거나 쓰는 데 실패했을 때."""


# ----------------------------------------------------------------------
# 규칙
# ----------------------------------------------------------------------
# 각 규칙은
#   parse(value)  -> 판정에 쓸 형태로 바꾼다. 잘못된 값이면 예외를 던진다.
#   check(parsed, snap) -> (리소스 id, 이유) 를 내놓는다.
#   requires      -> 이 규칙을 보려면 수집돼 있어야 하는 리소스 종류.
#
# requires 가 핵심이다. 수집하지 않은 종류를 '위반 없음' 으로 세면
# 컴플라이언스에서 겪은 것과 똑같은 거짓말이 된다.

def _tags(item):
    return (item["attributes"].get("tags") or {})


def _has_tags(item):
    """이 리소스가 태그를 담고 있는가.

    수집기가 EC2 인스턴스에만 tags 를 담는다. 태그 항목 자체가 없는
    리소스를 '태그 누락' 으로 세면, 수집하지 않은 것을 위반으로 만든다.
    """
    return "tags" in item["attributes"]


def _parse_list(value):
    items = [x.strip() for x in (value or "").replace("\n", ",").split(",")]
    items = [x for x in items if x]
    if not items:
        raise StandardError("값을 하나 이상 적으세요(쉼표로 구분).")
    return items


def _check_required_tags(parsed, snap):
    for item in snap.of_type("ec2:instance"):
        if not _has_tags(item):
            continue
        missing = [t for t in parsed if not _tags(item).get(t)]
        if missing:
            yield item["resource_id"], f"필수 태그가 없습니다: {', '.join(missing)}"


def _check_instance_types(parsed, snap):
    allowed = set(parsed)
    for item in snap.of_type("ec2:instance"):
        got = item["attributes"].get("instance_type")
        if got and got not in allowed:
            yield item["resource_id"], f"허용되지 않은 인스턴스 타입입니다: {got}"


def _parse_tag_values(value):
    """'Env=prod|stg|dev' 형식."""
    text = (value or "").strip()
    if "=" not in text:
        raise StandardError("'태그=값1|값2' 형식으로 적으세요. 예) Env=prod|stg")
    key, raw = text.split("=", 1)
    key = key.strip()
    values = [v.strip() for v in raw.split("|") if v.strip()]
    if not key or not values:
        raise StandardError("'태그=값1|값2' 형식으로 적으세요. 예) Env=prod|stg")
    return {"key": key, "values": values}


def _check_tag_values(parsed, snap):
    key, allowed = parsed["key"], set(parsed["values"])
    for item in snap.of_type("ec2:instance"):
        if not _has_tags(item):
            continue
        got = _tags(item).get(key)
        # 태그가 아예 없는 것은 required_tags 가 볼 일이다. 여기서 또
        # 잡으면 같은 리소스가 두 규칙에서 두 번 나온다.
        if got and got not in allowed:
            yield item["resource_id"], (
                f"{key} 값이 합의한 것과 다릅니다: {got} "
                f"(허용: {', '.join(sorted(allowed))})"
            )


def _parse_prefix(value):
    prefix = (value or "").strip()
    if not prefix:
        raise StandardError("접두사를 적으세요. 예) wcorp-")
    return prefix


def _check_name_prefix(parsed, snap):
    for item in snap.of_type("ec2:instance"):
        if not _has_tags(item):
            continue
        name = _tags(item).get("Name")
        # 이름 자체가 없는 것은 required_tags 의 몫이다.
        if name and not name.startswith(parsed):
            yield item["resource_id"], f"이름이 '{parsed}' 로 시작하지 않습니다: {name}"


RULES = {
    "required_tags": {
        "label": "필수 태그",
        "hint": "쉼표로 구분. 예) Name, Env, Owner",
        "why": "태그가 없으면 비용도 책임자도 나중에 추적할 수 없습니다. "
               "고객사마다 요구하는 키가 다릅니다.",
        "requires": ("ec2:instance",),
        "parse": _parse_list,
        "check": _check_required_tags,
    },
    "instance_types": {
        "label": "허용 인스턴스 타입",
        "hint": "쉼표로 구분. 예) t3.micro, t3.small, m6i.large",
        "why": "합의하지 않은 타입이 떠 있으면 비용과 성능 기준이 흔들립니다.",
        "requires": ("ec2:instance",),
        "parse": _parse_list,
        "check": _check_instance_types,
    },
    "tag_values": {
        "label": "태그 값 제한",
        "hint": "태그=값1|값2 형식. 예) Env=prod|stg|dev",
        "why": "'prod' 와 'production' 이 섞이면 태그로 거르는 모든 것이 "
               "어긋납니다.",
        "requires": ("ec2:instance",),
        "parse": _parse_tag_values,
        "check": _check_tag_values,
    },
    "name_prefix": {
        "label": "이름 접두사",
        "hint": "예) wcorp-",
        "why": "여러 고객사 리소스를 한 화면에서 볼 때 이름만으로 "
               "누구 것인지 알 수 있어야 합니다.",
        "requires": ("ec2:instance",),
        "parse": _parse_prefix,
        "check": _check_name_prefix,
    },
}


# ----------------------------------------------------------------------
# 저장
# ----------------------------------------------------------------------

# 접속 문자열은 app/db.py 가 만든다. 다른 모듈이 이 이름으로
# 가져다 쓰고 있어서 별칭으로 남긴다.
psycopg_uri = db.uri


_rows = db.rows


def _connect():
    return db.connect(StandardError)


def _ensure(cur):
    cur.execute("SELECT to_regclass('public.customer_standards')")
    if cur.fetchone()[0] is None:
        raise StandardError(
            "customer_standards 테이블이 없습니다. "
            "flask --app run init-db 를 실행하세요."
        )


def save(customer, rule, value, note=""):
    """규칙을 등록하거나 고친다."""
    customer = (customer or "").strip()
    if not customer:
        raise StandardError("고객사를 고르세요.")
    if rule not in RULES:
        raise StandardError(f"알 수 없는 규칙입니다: {rule}")

    # 저장하기 전에 해석해 본다. 못 읽는 값을 넣어두면 화면을 열 때마다
    # 그 고객사 전체가 깨진다.
    RULES[rule]["parse"](value)

    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            """
            INSERT INTO customer_standards (customer, rule, value, note)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (customer, rule) DO UPDATE SET
                value      = EXCLUDED.value,
                note       = EXCLUDED.note,
                active     = true,
                updated_at = now()
            RETURNING id
            """,
            (customer, rule, (value or "").strip(), (note or "").strip()),
        )
        return cur.fetchone()[0]


def remove(standard_id):
    """규칙을 지운다. 판정 결과를 저장하지 않으므로 남길 이유가 없다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            "DELETE FROM customer_standards WHERE id = %s RETURNING id",
            (standard_id,),
        )
        if cur.fetchone() is None:
            raise StandardError("규칙을 찾지 못했습니다.")


def listing(customer):
    """이 고객사의 규칙. RULES 에 정의된 순서로."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure(cur)
        cur.execute(
            "SELECT * FROM customer_standards WHERE customer = %s AND active",
            (customer,),
        )
        items = _rows(cur)

    order = list(RULES)
    items.sort(key=lambda r: order.index(r["rule"])
               if r["rule"] in order else len(order))
    return items


def required_tags_for(customer):
    """이 고객사의 필수 태그. 없으면 None.

    compliance 의 기본값을 대신할 값이다. None 을 돌려주는 것이 중요하다 -
    빈 목록을 주면 '태그를 하나도 요구하지 않는다' 가 되어, 규칙을 안 정한
    고객사에서 기본 점검까지 꺼진다.
    """
    try:
        for item in listing(customer):
            if item["rule"] == "required_tags":
                return _parse_list(item["value"])
    except StandardError:
        return None
    return None


# ----------------------------------------------------------------------
# 판정
# ----------------------------------------------------------------------
# 결과를 저장하지 않는다. 리소스를 고친 뒤에도 옛 판정이 남으면 진실이
# 둘이 된다(컴플라이언스·온보딩 준비도와 같은 판단).

def evaluate(customer):
    """이 고객사의 규칙을 최신 스냅샷에 대고 판정한다.

    돌려주는 것:
      {"rules": [...], "snapshots": [...], "no_rules": bool}

    각 규칙은 아래 셋 중 하나다.
      ok      : 봤고 위반이 없다
      violated: 위반이 있다
      skipped : 그 리소스를 수집하지 않아 볼 수가 없었다

    마지막 것이 중요하다. 컴플라이언스에서 '위반 0건' 이 "지키고 있다" 와
    "보지 않았다" 두 가지 뜻을 갖던 문제를 여기서도 똑같이 가른다.
    """
    from app import compliance
    from app.accounts import list_accounts, AccountError
    from app.compliance import ComplianceError

    rules = listing(customer)
    if not rules:
        return {"rules": [], "snapshots": [], "no_rules": True}

    try:
        accounts = [a["account_id"] for a in list_accounts(enabled_only=False)
                    if a["customer"] == customer]
    except AccountError as e:
        raise StandardError(str(e))

    if not accounts:
        raise StandardError("이 고객사에 등록된 계정이 없습니다.")

    try:
        snapshots = [s for s in compliance.latest_snapshots(limit=200)
                     if s["account_id"] in accounts]
    except ComplianceError as e:
        raise StandardError(str(e))

    if not snapshots:
        raise StandardError(
            "이 고객사의 리소스 스냅샷이 없습니다. "
            "flask --app run collect-resources 를 먼저 실행하세요."
        )

    loaded = []
    try:
        with compliance._connect() as conn, conn.cursor() as cur:
            for meta in snapshots:
                loaded.append((meta, compliance.load_snapshot(cur, meta["snapshot_id"])))
    except Exception as e:
        raise StandardError(f"스냅샷을 읽지 못했습니다: {e}")

    out = []
    for item in rules:
        spec = RULES.get(item["rule"])
        if spec is None:
            # 코드에서 규칙을 뺐는데 DB 에 남아 있는 경우.
            out.append({**item, "state": "unknown", "label": item["rule"],
                        "findings": [], "missing": []})
            continue

        try:
            parsed = spec["parse"](item["value"])
        except StandardError as e:
            # 저장할 때 검사하지만, 예전 값이 남아 있을 수 있다.
            out.append({**item, "state": "bad_value", "label": spec["label"],
                        "detail": str(e), "findings": [], "missing": []})
            continue

        findings, missing = [], set()
        # 이 규칙을 실제로 볼 수 있었던 스냅샷 수. 0 이면 위반이 없는 게
        # 아니라 볼 수가 없었던 것이다.
        evaluated = 0
        for meta, snap in loaded:
            gap = [t for t in spec["requires"] if t not in snap.collected]
            if gap:
                missing.update(gap)
                continue
            evaluated += 1
            for resource_id, reason in spec["check"](parsed, snap):
                findings.append({
                    "resource_id": resource_id, "reason": reason,
                    "account_id": meta["account_id"], "region": meta["region"],
                })

        if findings:
            state = "violated"
        elif evaluated == 0:
            state = "skipped"
        else:
            state = "ok"

        out.append({**item, "state": state, "label": spec["label"],
                    "why": spec["why"], "findings": findings,
                    "evaluated": evaluated, "missing": sorted(missing)})

    return {"rules": out, "snapshots": snapshots, "no_rules": False}


def summarize(result):
    """화면 위쪽 숫자."""
    rules = result["rules"]
    return {
        "total": len(rules),
        "violated": len([r for r in rules if r["state"] == "violated"]),
        "skipped": len([r for r in rules if r["state"] == "skipped"]),
        "findings": sum(len(r["findings"]) for r in rules),
    }
