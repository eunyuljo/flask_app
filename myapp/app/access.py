# app/access.py
# 고객사 계정에 우리가 들어갈 수 있는가, 그리고 들어가는 방식이 안전한가.
#
# ── 왜 따로 보나 ────────────────────────────────────────────────────
# MSP 가 고객사에 대해 가진 것 중 가장 민감한 것은 계정 접점이다.
# 그런데 지금까지 이 접점은 아무도 안 보고 있었다. AssumeRole 이 죽으면
# 장애 때 못 들어가고, 계약이 끝났는데 살아 있으면 그건 사고다.
# 둘 다 평소에는 아무 증상이 없다.
#
# ── 두 가지를 나눠서 본다 ───────────────────────────────────────────
# 1. 설정 점검 : DB 에 적힌 값만 본다. 공짜라 화면을 열 때마다 계산한다.
# 2. 실제 확인 : AssumeRole 을 진짜로 시도한다. 사람이 누를 때만 한다.
#
# 이 구분은 app/health.py 와 같다. "설정됨" 은 "작동함" 이 아니다.

from flask import current_app

# 심각도. 화면 색과 정렬에 쓴다.
LEVELS = ("danger", "warn", "info")

LEVEL_LABEL = {
    "danger": "위험",
    "warn": "확인 필요",
    "info": "참고",
}

# 마지막 확인이 이보다 오래되면 '오래됨' 으로 본다.
# 자격증명이 아니라 신뢰 정책이 바뀌는 주기를 생각한 값이라 넉넉하다.
STALE_DAYS = 30


class AccessError(Exception):
    """계정 접속 정보를 읽거나 쓰는 데 실패했을 때."""


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
        raise AccessError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise AccessError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _ensure_probes(cur):
    cur.execute("SELECT to_regclass('public.account_probes')")
    if cur.fetchone()[0] is None:
        raise AccessError(
            "account_probes 테이블이 없습니다. flask --app run init-db 를 실행하세요."
        )


# ----------------------------------------------------------------------
# 설정 점검 (공짜, 매번 계산)
# ----------------------------------------------------------------------
# 결과를 저장하지 않는다. 저장하면 계정을 고친 뒤에도 옛 판정이 남아
# 진실이 둘이 된다. 이 프로젝트에서 컴플라이언스와 온보딩 준비도가
# 같은 이유로 매번 다시 계산한다.

def findings(account, probe=None):
    """계정 하나의 설정 문제. [{level, id, title, detail}, ...]

    문제가 없으면 빈 목록이다. '문제 없음' 을 항목으로 만들지 않는다 -
    화면에서 진짜 문제를 가린다.
    """
    from datetime import datetime, timedelta, timezone

    out = []
    role_arn = (account.get("role_arn") or "").strip()
    external_id = (account.get("external_id") or "").strip()
    regions = account.get("regions") or []

    if not role_arn:
        out.append({
            "level": "info", "id": "demo-account",
            "title": "데모 계정 (역할 없음)",
            "detail": "role_arn 이 비어 있어 실제 AWS 를 부르지 않습니다. "
                      "운영 고객사라면 역할을 등록해야 합니다.",
        })
    elif not external_id:
        # 이게 이 화면을 만든 이유다.
        out.append({
            "level": "danger", "id": "no-external-id",
            "title": "ExternalId 가 없습니다",
            "detail": "제3자가 우리에게 이 역할을 대신 맡게 만드는 "
                      "혼동된 대리인(confused deputy) 공격을 막을 수 없습니다. "
                      "고객사 신뢰 정책과 함께 ExternalId 를 설정하세요.",
        })

    if not regions:
        out.append({
            "level": "warn", "id": "no-regions",
            "title": "허용 리전이 없습니다",
            "detail": "리소스 수집·AI 진단·콘솔이 전부 리전을 요구합니다. "
                      "이 상태로는 아무것도 조회하지 못합니다.",
        })

    if not account.get("enabled", True):
        out.append({
            "level": "info", "id": "disabled",
            "title": "비활성 계정",
            "detail": "조회 대상에서 빠져 있습니다. 계약이 끝난 계정이라면 "
                      "고객사 쪽 역할도 회수됐는지 확인하세요.",
        })

    # 실제 확인 상태. role_arn 이 없으면 확인할 것이 없다.
    if role_arn:
        if not probe:
            out.append({
                "level": "warn", "id": "never-probed",
                "title": "한 번도 확인하지 않았습니다",
                "detail": "역할이 적혀 있을 뿐, 실제로 들어가지는 확인된 적이 "
                          "없습니다. 장애 때 처음 시도하게 됩니다.",
            })
        elif not probe["ok"]:
            out.append({
                "level": "danger", "id": "probe-failed",
                "title": "마지막 확인이 실패했습니다",
                "detail": probe.get("detail") or "사유가 기록되지 않았습니다.",
            })
        else:
            age = datetime.now(timezone.utc) - probe["at"]
            if age > timedelta(days=STALE_DAYS):
                out.append({
                    "level": "warn", "id": "stale-probe",
                    "title": f"마지막 확인이 {age.days}일 전입니다",
                    "detail": "그동안 고객사 쪽 신뢰 정책이 바뀌었을 수 있습니다.",
                })

    out.sort(key=lambda f: LEVELS.index(f["level"]))
    return out


def worst(items):
    """가장 심각한 등급. 문제가 없으면 None."""
    for level in LEVELS:
        if any(f["level"] == level for f in items):
            return level
    return None


# ----------------------------------------------------------------------
# 실제 확인 기록
# ----------------------------------------------------------------------

def last_probes():
    """{(account_id, region): 기록}. 화면에서 계정마다 질의하지 않기 위해."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_probes(cur)
        cur.execute("SELECT * FROM account_probes")
        return {(r["account_id"], r["region"]): r for r in _rows(cur)}


def record_probe(account_id, region, ok, detail="", probed_by=""):
    """확인 결과를 남긴다. 같은 (계정, 리전) 은 최신 것만 남는다."""
    with _connect() as conn, conn.cursor() as cur:
        _ensure_probes(cur)
        cur.execute(
            """
            INSERT INTO account_probes (account_id, region, ok, detail, probed_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (account_id, region) DO UPDATE SET
                at        = now(),
                ok        = EXCLUDED.ok,
                detail    = EXCLUDED.detail,
                probed_by = EXCLUDED.probed_by
            """,
            (account_id, region, bool(ok), detail[:500], probed_by or ""),
        )


def probe(account, region, probed_by=""):
    """이 계정에 실제로 들어가 본다. (성공 여부, 설명) 을 돌려주고 기록한다.

    화면을 열 때 자동으로 부르지 않는다. AssumeRole 은 고객사 CloudTrail 에
    남는 행위다. 새로고침 한 번에 그게 나가면 안 된다(health.py 와 같은 판단).
    """
    from app.aws_session import get_env, is_demo, SessionError

    account_id = account["account_id"]

    if is_demo(account):
        # 확인할 것이 없다. 실패로 적으면 데모 계정이 영원히 빨갛게 남는다.
        return False, "데모 계정입니다(role_arn 없음). 확인할 역할이 없습니다."

    try:
        get_env(account, region)
    except SessionError as e:
        detail = str(e).splitlines()[0]
        record_probe(account_id, region, False, detail, probed_by)
        return False, detail
    except Exception as e:                       # noqa: BLE001
        # 점검 하나가 터졌다고 화면이 500 이 되면 안 된다.
        detail = f"{type(e).__name__}: {e}"
        record_probe(account_id, region, False, detail, probed_by)
        return False, detail

    detail = f"{region} 에 AssumeRole 성공"
    record_probe(account_id, region, True, detail, probed_by)
    return True, detail


# ----------------------------------------------------------------------
# 화면용 모으기
# ----------------------------------------------------------------------

def overview(accounts):
    """계정마다 설정 문제와 마지막 확인을 붙여 돌려준다.

    확인 기록을 못 읽어도 설정 점검은 돌려준다. DB 표 하나가 없다고
    ExternalId 가 빠진 것까지 안 보이면 곤란하다.
    """
    try:
        probes = last_probes()
    except AccessError:
        probes = {}

    rows = []
    for account in accounts:
        regions = account.get("regions") or []
        # 리전이 여럿이면 확인도 리전마다다. 대표로 하나를 고르지 않는다 -
        # 어떤 리전만 막히는 경우가 실제로 있다.
        per_region = [
            {"region": r, "probe": probes.get((account["account_id"], r))}
            for r in regions
        ]
        # 설정 점검에는 가장 나쁜 확인 결과를 넘긴다.
        got = [p["probe"] for p in per_region if p["probe"]]
        chosen = None
        if got:
            failed = [p for p in got if not p["ok"]]
            chosen = failed[0] if failed else min(got, key=lambda p: p["at"])

        items = findings(account, chosen)
        rows.append({
            "account": account,
            "regions": per_region,
            "findings": items,
            "worst": worst(items),
        })

    rows.sort(key=lambda r: (LEVELS.index(r["worst"]) if r["worst"] else len(LEVELS),
                             r["account"].get("customer") or ""))
    return rows
