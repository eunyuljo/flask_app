# app/compliance.py
# 스냅샷을 기준으로 인프라가 모범사례를 지키고 있는지 점검한다.
#
# ── AWS Config 와 무엇이 다른가 ──────────────────────────────────────
# Config 는 '지금 이 리소스가 규칙에 맞나' 를 본다. 이쪽은 이미 찍어둔
# 스냅샷 위에서 돌기 때문에 시간축이 있다. 그래서 이런 걸 물을 수 있다.
#
#   - 언제부터 위반이었나
#   - 이번에 새로 생긴 위반은 무엇인가
#   - 고쳤다가 다시 열린 것은 무엇인가  <- 이게 진짜 필요한 것이다
#
# 마지막 항목이 중요하다. 재발은 "고쳤다" 는 보고가 사실은 임시 조치였다는
# 뜻이거나, 자동화가 되돌리고 있다는 뜻이다. 지금 상태만 보면 처음 열린
# 것과 열 번째 열린 것이 똑같아 보인다.
#
# 대신 한계가 분명하다. 우리는 '수집한 것' 만 볼 수 있다. Config 는
# 자기가 지원하는 리소스를 전부 본다. 이건 Config 를 대체하는 물건이 아니라,
# 이미 찍고 있는 스냅샷을 한 번 더 쓰는 것이다.
#
# ── 결과를 저장하지 않는 이유 ────────────────────────────────────────
# 위반 목록을 표에 넣지 않는다. 스냅샷이 이미 시점 데이터라서 언제든
# 다시 계산할 수 있고, 저장하면 진실이 둘이 된다. 점검 규칙을 고쳤을 때
# 저장된 옛 결과가 남아 있으면 어느 쪽이 맞는지 알 수 없다.
#
# (event_store 를 메모리에 두었다가 DB 로 옮기면서 배운 것과 같은 이야기다.
#  원본이 있는데 사본을 따로 들고 있을 이유가 없다.)

import re

from flask import current_app

# 인터넷 전체를 뜻하는 CIDR.
ANY_IPV4 = "0.0.0.0/0"
ANY_IPV6 = "::/0"

# 관리 포트. 여기가 인터넷에 열려 있으면 그 자체로 사고 경로다.
ADMIN_PORTS = {22: "SSH", 3389: "RDP", 5985: "WinRM", 5986: "WinRM"}

# 데이터베이스 포트. 인터넷에서 직접 닿을 이유가 없다.
DB_PORTS = {
    3306: "MySQL", 5432: "PostgreSQL", 1433: "MSSQL",
    27017: "MongoDB", 6379: "Redis", 9200: "Elasticsearch",
    5439: "Redshift", 11211: "Memcached",
}

# 모든 리소스에 있어야 하는 태그.
# 없으면 장애가 났을 때 누구를 불러야 할지 모른다.
REQUIRED_TAGS = ("Name", "Env")

SEVERITIES = ("critical", "high", "medium", "low")

# 심각도 정렬용. 문자열로 정렬하면 critical < high < low < medium 이 된다.
SEVERITY_ORDER = {s: i for i, s in enumerate(SEVERITIES)}


class ComplianceError(Exception):
    """점검을 돌리지 못했을 때."""


# ----------------------------------------------------------------------
# 스냅샷 한 장을 다루기 쉬운 형태로
# ----------------------------------------------------------------------

class Snapshot:
    """점검 함수에 넘길 스냅샷 한 장.

    점검 중에는 다른 리소스를 봐야 할 때가 있다. "이 인스턴스가 붙은
    보안그룹이 인터넷에 열려 있나" 같은 것은 리소스 하나만 봐서는
    답할 수 없다. Config 의 규칙 하나로는 다루기 어려운 부분인데,
    스냅샷은 통째로 들고 있으니 그냥 찾아보면 된다.
    """

    def __init__(self, snapshot_id, items, meta=None, collected_types=None):
        self.snapshot_id = snapshot_id
        self.items = items
        self.meta = meta or {}
        self._by_id = {i["resource_id"]: i for i in items}
        self._by_type = {}
        for i in items:
            self._by_type.setdefault(i["resource_type"], []).append(i)

        # 이 스냅샷이 훑은 리소스 종류. 비어 있으면 collected_types 열이
        # 생기기 전에 찍힌 스냅샷이라, 실제로 들어 있는 종류만 본 것으로
        # 여긴다. 가장 보수적인 추측이다 - 없는 종류를 '봤는데 깨끗함'
        # 으로 처리하는 것보다 '못 봤음' 으로 두는 편이 안전하다.
        self.collected = set(collected_types or self._by_type)

    def of_type(self, resource_type):
        return self._by_type.get(resource_type, [])

    def knows(self, *resource_types):
        """이 종류들을 전부 수집했는가."""
        return all(t in self.collected for t in resource_types)

    def get(self, resource_id):
        return self._by_id.get(resource_id)

    def __len__(self):
        return len(self.items)


def _parse_ingress(rule):
    """'22/tcp:0.0.0.0/0' 을 (포트, 프로토콜, 출처) 로 나눈다.

    수집기가 이 형태로 만들어 넣는다. 형식이 다르면 (None, ...) 을
    돌려주고, 점검은 그런 규칙을 건너뛴다. 읽지 못한 규칙을
    '안전함' 으로 처리하는 셈이라 안전한 쪽은 아니지만, 형식을
    모르는 값을 위반으로 올리면 목록이 거짓으로 가득 찬다.
    """
    m = re.match(r"^(\d+|\*)/([a-z0-9-]+):(.+)$", str(rule).strip(), re.I)
    if not m:
        return None, None, None
    port = None if m.group(1) == "*" else int(m.group(1))
    return port, m.group(2).lower(), m.group(3).strip()


def _is_world(source):
    return source in (ANY_IPV4, ANY_IPV6)


# ----------------------------------------------------------------------
# 점검 항목
# ----------------------------------------------------------------------
# 각 함수는 스냅샷을 받아 (resource_id, 설명) 을 내놓는다.
# 규칙을 표가 아니라 코드로 두는 이유는 파일 맨 위에 적었다.

def _check_admin_port_open(snap):
    for sg in snap.of_type("ec2:security_group"):
        for rule in sg["attributes"].get("ingress", []):
            port, proto, source = _parse_ingress(rule)
            if port in ADMIN_PORTS and _is_world(source):
                yield sg["resource_id"], f"{ADMIN_PORTS[port]}({port}/{proto}) 가 {source} 에 열려 있습니다"


def _check_db_port_open(snap):
    for sg in snap.of_type("ec2:security_group"):
        for rule in sg["attributes"].get("ingress", []):
            port, proto, source = _parse_ingress(rule)
            if port in DB_PORTS and _is_world(source):
                yield sg["resource_id"], f"{DB_PORTS[port]}({port}/{proto}) 가 {source} 에 열려 있습니다"


def _check_all_ports_open(snap):
    """포트를 지정하지 않고 전부 연 규칙."""
    for sg in snap.of_type("ec2:security_group"):
        for rule in sg["attributes"].get("ingress", []):
            port, proto, source = _parse_ingress(rule)
            if port is None and source and _is_world(source):
                yield sg["resource_id"], f"모든 포트({proto})가 {source} 에 열려 있습니다"


def _check_instance_behind_open_admin_port(snap):
    """관리 포트가 열린 보안그룹을 쓰고 있는 인스턴스.

    보안그룹만 보면 '어딘가 열려 있다' 로 끝난다. 실제로 그 그룹을
    쓰는 인스턴스가 있어야 노출이다. 붙은 인스턴스가 없는 그룹은
    치우면 되는 문제고, 붙어 있으면 지금 뚫려 있는 문제다.
    """
    risky = {}
    for sg in snap.of_type("ec2:security_group"):
        for rule in sg["attributes"].get("ingress", []):
            port, _proto, source = _parse_ingress(rule)
            if port in ADMIN_PORTS and _is_world(source):
                risky[sg["resource_id"]] = ADMIN_PORTS[port]

    if not risky:
        return
    for inst in snap.of_type("ec2:instance"):
        attrs = inst["attributes"]
        if attrs.get("state") != "running":
            continue
        hit = [g for g in attrs.get("security_groups", []) if g in risky]
        if not hit:
            continue

        names = ", ".join(f"{g}({risky[g]})" for g in hit)

        # 퍼블릭 IP 를 알면 '열려 있을 수 있다' 와 '지금 닿는다' 를 가른다.
        # 사설 IP 만 있는 인스턴스까지 critical 로 올리면 목록이 부풀고,
        # 그러면 진짜 뚫린 것이 묻힌다.
        if "public_ip" not in attrs:
            # 이 값을 수집하기 전에 찍힌 스냅샷. 판단을 미루지 않고
            # 예전처럼 올리되, 확인하지 못했다는 것을 밝힌다.
            yield (inst["resource_id"],
                   f"관리 포트가 열린 보안그룹에 붙어 있습니다: {names} "
                   "(퍼블릭 IP 를 수집하지 않아 실제 도달 여부는 확인하지 못했습니다)")
        elif attrs.get("public_ip"):
            yield (inst["resource_id"],
                   f"퍼블릭 IP({attrs['public_ip']})가 붙어 있고 관리 포트가 "
                   f"열린 보안그룹을 씁니다: {names}. 지금 인터넷에서 닿습니다.",
                   "critical")
        else:
            yield (inst["resource_id"],
                   f"관리 포트가 열린 보안그룹에 붙어 있습니다: {names}. "
                   "퍼블릭 IP 가 없어 인터넷에서 직접 닿지는 않습니다.",
                   "medium")


def _check_imdsv1(snap):
    """인스턴스 메타데이터 서비스 v1 이 열려 있는 인스턴스.

    IMDSv1 은 단순 GET 으로 임시 자격증명을 돌려준다. 그래서 애플리케이션에
    SSRF 하나만 있어도 그 인스턴스 역할의 자격증명이 통째로 나간다.
    v2 는 PUT 으로 토큰을 먼저 받아야 해서 그 경로가 막힌다.

    붙은 역할이 있으면 훔칠 것이 있다는 뜻이라 무게를 올린다.
    """
    for inst in snap.of_type("ec2:instance"):
        attrs = inst["attributes"]
        # 키가 아예 없으면 이 값을 수집하기 전에 찍힌 스냅샷이다.
        # 모르는 것을 위반으로 올리면 목록이 거짓으로 찬다.
        if "imds" not in attrs:
            continue
        if attrs.get("imds_endpoint") == "disabled":
            continue          # 메타데이터 자체를 껐으면 v1 도 안 열린다
        if attrs.get("imds") == "required":
            continue          # v2 강제 - 정상

        profile = attrs.get("iam_profile") or ""
        if profile:
            yield (inst["resource_id"],
                   f"IMDSv1 이 열려 있고 역할이 붙어 있습니다({profile}). "
                   "SSRF 한 번으로 이 역할의 자격증명이 나갈 수 있습니다.",
                   "critical")
        else:
            yield (inst["resource_id"],
                   "IMDSv1 이 열려 있습니다(HttpTokens=optional).",
                   "high")


def _check_s3_public(snap):
    for b in snap.of_type("s3:bucket"):
        if b["attributes"].get("public_access_blocked") is not True:
            yield b["resource_id"], "퍼블릭 액세스 차단이 켜져 있지 않습니다"


def _check_s3_encryption(snap):
    for b in snap.of_type("s3:bucket"):
        enc = b["attributes"].get("encryption")
        if not enc or str(enc).lower() in ("none", "disabled", "unknown"):
            yield b["resource_id"], "기본 암호화가 설정되어 있지 않습니다"


def _check_s3_versioning(snap):
    for b in snap.of_type("s3:bucket"):
        if b["attributes"].get("versioning") != "Enabled":
            yield b["resource_id"], "버저닝이 꺼져 있어 덮어쓰기·삭제를 되돌릴 수 없습니다"


def _check_required_tags(snap):
    """필수 태그. 고객사가 정한 것이 있으면 그쪽을 쓴다.

    REQUIRED_TAGS 는 코드에 박힌 기본값이라 고객사가 둘째부터 맞지 않는다.
    고객사 표준(app/standards.py)에 required_tags 가 있으면 스냅샷을 읽는
    쪽이 snap.meta 에 넣어 준다 - 여기서 DB 를 보면 점검 함수가 순수하지
    않게 되고, 그러면 테스트에서 손으로 만들어 먹일 수 없다.
    """
    required = snap.meta.get("required_tags") or REQUIRED_TAGS
    for item in snap.items:
        # 태그를 붙일 수 없는 리소스가 있으므로, 태그 항목 자체가 없으면 넘어간다.
        attrs = item["attributes"]
        if "tags" not in attrs:
            continue
        missing = [t for t in required if not (attrs.get("tags") or {}).get(t)]
        if missing:
            yield item["resource_id"], f"필수 태그가 없습니다: {', '.join(missing)}"


def _check_rds_public(snap):
    for db in snap.of_type("rds:instance"):
        if db["attributes"].get("public") is True:
            yield db["resource_id"], "퍼블릭 접근이 켜져 있습니다"


def _check_rds_multi_az(snap):
    for db in snap.of_type("rds:instance"):
        if db["attributes"].get("multi_az") is False:
            yield db["resource_id"], "단일 AZ 로 떠 있어 AZ 장애를 견디지 못합니다"


# 광범위 권한으로 취급할 관리형 정책.
BROAD_POLICIES = re.compile(r"(AdministratorAccess|FullAccess|PowerUser)", re.I)


def _check_iam_broad_policy(snap):
    for role in snap.of_type("iam:role"):
        broad = [p for p in role["attributes"].get("policies", []) if BROAD_POLICIES.search(str(p))]
        if broad:
            yield role["resource_id"], f"광범위한 권한이 붙어 있습니다: {', '.join(broad)}"


# 점검 항목 정의.
#   standard : 근거로 삼은 기준. 화면에서 "왜 이게 위반인가" 를 설명한다.
#   severity : 조치 순서를 정하는 값. 전부 critical 이면 우선순위가 없다.
CHECKS = [
    {
        "id": "sg-admin-port-open",
        "requires": ("ec2:security_group",),
        "title": "관리 포트가 인터넷에 열려 있음",
        "severity": "critical",
        "standard": "CIS AWS 5.2 / 5.3",
        "why": "SSH·RDP 가 인터넷에 열려 있으면 자격증명 하나로 내부에 들어옵니다. "
               "무차별 대입은 사람이 시작하지 않아도 자동으로 들어옵니다.",
        "fn": _check_admin_port_open,
    },
    {
        "id": "sg-db-port-open",
        "requires": ("ec2:security_group",),
        "title": "DB 포트가 인터넷에 열려 있음",
        "severity": "critical",
        "standard": "CIS AWS 5.2",
        "why": "데이터베이스가 인터넷에서 직접 닿을 이유는 거의 없습니다. "
               "애플리케이션 계층을 건너뛰고 데이터에 바로 닿는 경로가 됩니다.",
        "fn": _check_db_port_open,
    },
    {
        "id": "sg-all-ports-open",
        "requires": ("ec2:security_group",),
        "title": "모든 포트가 인터넷에 열려 있음",
        "severity": "critical",
        "standard": "CIS AWS 5.2",
        "why": "포트를 지정하지 않은 허용 규칙입니다. 지금 무엇이 떠 있는지와 "
               "무관하게, 앞으로 열릴 모든 포트까지 함께 여는 것입니다.",
        "fn": _check_all_ports_open,
    },
    {
        "id": "ec2-exposed-admin-port",
        "requires": ("ec2:instance", "ec2:security_group"),
        "title": "관리 포트가 열린 보안그룹을 쓰는 인스턴스",
        "severity": "critical",
        "standard": "CIS AWS 5.2",
        "why": "보안그룹만 보면 '어딘가 열려 있다' 로 끝납니다. 그 그룹을 실제로 "
               "쓰는 인스턴스가 있으면 지금 뚫려 있는 것입니다.",
        "fn": _check_instance_behind_open_admin_port,
    },
    {
        "id": "ec2-imdsv1",
        "requires": ("ec2:instance",),
        "title": "인스턴스 메타데이터 v1 이 열려 있음",
        "severity": "critical",
        "standard": "CIS AWS 5.6",
        "why": "IMDSv1 은 단순 GET 으로 임시 자격증명을 돌려줍니다. "
               "애플리케이션에 SSRF 하나만 있어도 그 인스턴스 역할의 "
               "자격증명이 통째로 나갑니다. v2 는 토큰을 먼저 받게 해서 막습니다.",
        "fn": _check_imdsv1,
    },
    {
        "id": "s3-public-access",
        "requires": ("s3:bucket",),
        "title": "S3 퍼블릭 액세스 차단이 꺼져 있음",
        "severity": "critical",
        "standard": "CIS AWS 2.1.5",
        "why": "차단을 켜두지 않으면, 나중에 누군가 버킷 정책을 잘못 손대는 순간 "
               "곧바로 공개됩니다. 지금 공개가 아니라도 안전장치가 없는 상태입니다.",
        "fn": _check_s3_public,
    },
    {
        "id": "rds-public",
        "requires": ("rds:instance",),
        "title": "RDS 인스턴스가 퍼블릭으로 열려 있음",
        "severity": "high",
        "standard": "CIS AWS 2.3.3",
        "why": "DB 인스턴스에 퍼블릭 IP 가 붙습니다. 보안그룹이 막고 있더라도 "
               "규칙 한 줄만 잘못되면 바로 노출됩니다.",
        "fn": _check_rds_public,
    },
    {
        "id": "iam-broad-policy",
        "requires": ("iam:role",),
        "title": "역할에 광범위한 권한이 붙어 있음",
        "severity": "high",
        "standard": "CIS AWS 1.16 (최소 권한)",
        "why": "FullAccess·AdministratorAccess 는 필요한 것보다 크게 줍니다. "
               "이 역할이 탈취되면 피해 범위가 그대로 권한 범위가 됩니다.",
        "fn": _check_iam_broad_policy,
    },
    {
        "id": "s3-encryption",
        "requires": ("s3:bucket",),
        "title": "S3 기본 암호화가 없음",
        "severity": "high",
        "standard": "CIS AWS 2.1.1",
        "why": "저장 데이터 암호화는 대부분의 고객사 보안 요건에 들어갑니다. "
               "기본 암호화가 없으면 앞으로 올라가는 객체가 평문으로 쌓입니다.",
        "fn": _check_s3_encryption,
    },
    {
        "id": "rds-single-az",
        "requires": ("rds:instance",),
        "title": "RDS 가 단일 AZ 로 떠 있음",
        "severity": "medium",
        "standard": "Well-Architected 신뢰성",
        "why": "AZ 하나가 빠지면 그대로 멈춥니다. 운영 등급 DB 라면 "
               "가용성 목표를 만족할 수 없습니다.",
        "fn": _check_rds_multi_az,
    },
    {
        "id": "s3-versioning",
        "requires": ("s3:bucket",),
        "title": "S3 버저닝이 꺼져 있음",
        "severity": "medium",
        "standard": "Well-Architected 신뢰성",
        "why": "실수로 덮어쓰거나 지운 객체를 되돌릴 수 없습니다. "
               "랜섬웨어로 암호화된 경우에도 마찬가지입니다.",
        "fn": _check_s3_versioning,
    },
    {
        "id": "missing-required-tags",
        "requires": (),
        "title": "필수 태그가 없음",
        "severity": "low",
        "standard": "태깅 정책",
        "why": "장애가 났을 때 누구를 불러야 할지, 이게 운영인지 개발인지를 "
               "리소스만 보고 알 수 없습니다. 비용 배분도 되지 않습니다.",
        "fn": _check_required_tags,
    },
]

CHECKS_BY_ID = {c["id"]: c for c in CHECKS}


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------

def psycopg_uri():
    return current_app.config["DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _connect():
    try:
        import psycopg
    except ImportError as e:
        raise ComplianceError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise ComplianceError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


def _table_ready(cur, name):
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


def load_snapshot(cur, snapshot_id, meta=None):
    # 무엇을 수집했는지 먼저 읽는다. 같은 커서로 리소스를 읽은 뒤에
    # 질의를 하나 더 던지면 앞의 결과가 덮어써진다.
    cur.execute(
        "SELECT collected_types FROM resource_snapshots WHERE snapshot_id = %s",
        (snapshot_id,),
    )
    row = cur.fetchone()
    collected = list(row[0]) if row and row[0] else None

    cur.execute(
        "SELECT resource_id, resource_type, attributes FROM resources "
        "WHERE snapshot_id = %s",
        (snapshot_id,),
    )
    return Snapshot(snapshot_id, _rows(cur), meta=meta, collected_types=collected)


def coverage(snap):
    """점검 항목마다 돌았는지 / 못 돌았는지.

    "위반 0건" 이 두 가지 뜻을 갖는 문제를 여기서 가른다.
      돌았고 0건  -> 봤는데 문제가 없다
      못 돌았음   -> 그 리소스를 아예 수집하지 않았다

    화면과 보고서에서는 정반대의 뜻이므로 반드시 구분해서 보여줘야 한다.
    """
    ran, skipped = [], []
    for check in CHECKS:
        need = check.get("requires", ())
        missing = [t for t in need if t not in snap.collected]
        if missing:
            skipped.append({**check, "missing": missing})
        else:
            ran.append(check)
    return {"ran": ran, "skipped": skipped}


def latest_snapshots(limit=50):
    """계정+리전마다 가장 최근 스냅샷 하나씩."""
    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur, "resource_snapshots"):
            raise ComplianceError("resource_snapshots 테이블이 없습니다.")
        cur.execute(
            """
            SELECT DISTINCT ON (account_id, region)
                   snapshot_id, account_id, region, source, collected_at
            FROM resource_snapshots
            WHERE complete
            ORDER BY account_id, region, collected_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return _rows(cur)


def exceptions(account_id=None):
    """살아 있는 예외만. 만료된 것은 효력이 없다."""
    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur, "compliance_exceptions"):
            return []
        sql = ("SELECT * FROM compliance_exceptions WHERE expires_at > now()")
        params = []
        if account_id:
            sql += " AND account_id = %s"
            params.append(account_id)
        cur.execute(sql + " ORDER BY expires_at", params)
        return _rows(cur)


def all_exceptions(account_id=None):
    """만료된 것까지 포함한 전체. 화면에서 이력을 보여주기 위한 것."""
    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur, "compliance_exceptions"):
            return []
        sql = "SELECT *, (expires_at <= now()) AS expired FROM compliance_exceptions"
        params = []
        if account_id:
            sql += " WHERE account_id = %s"
            params.append(account_id)
        cur.execute(sql + " ORDER BY expires_at DESC", params)
        return _rows(cur)


def add_exception(account_id, check_id, resource_id, reason, expires_at, approved_by=""):
    if check_id not in CHECKS_BY_ID:
        raise ComplianceError(f"알 수 없는 점검 항목입니다: {check_id}")
    if not (reason or "").strip():
        raise ComplianceError("사유를 적어야 합니다. 사유 없는 예외는 예외가 아닙니다.")
    if not expires_at:
        raise ComplianceError("만료일이 필요합니다. 끝나지 않는 예외는 두지 않습니다.")

    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur, "compliance_exceptions"):
            raise ComplianceError(
                "compliance_exceptions 테이블이 없습니다. "
                "flask --app run init-db 를 실행하세요."
            )
        cur.execute(
            """
            INSERT INTO compliance_exceptions
                (account_id, check_id, resource_id, reason, approved_by, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, check_id, resource_id) DO UPDATE
               SET reason = EXCLUDED.reason,
                   approved_by = EXCLUDED.approved_by,
                   expires_at = EXCLUDED.expires_at,
                   created_at = now()
            RETURNING id
            """,
            (account_id, check_id, resource_id or "", reason.strip(),
             approved_by, expires_at),
        )
        return cur.fetchone()[0]


def drop_exception(exception_id):
    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur, "compliance_exceptions"):
            raise ComplianceError("compliance_exceptions 테이블이 없습니다.")
        cur.execute(
            "DELETE FROM compliance_exceptions WHERE id = %s RETURNING id",
            (exception_id,),
        )
        if cur.fetchone() is None:
            raise ComplianceError("그런 예외가 없습니다.")


# ----------------------------------------------------------------------
# 평가
# ----------------------------------------------------------------------

def run_checks(snap, excused=None):
    """스냅샷 한 장을 점검한다. DB 를 보지 않는 순수 함수다.

    excused: 예외. 두 형태를 받는다.
        {(check_id, resource_id)}          - 사유 없이 제외만
        {(check_id, resource_id): "사유"}   - 사유까지

    resource_id 가 빈 문자열이면 그 항목 전체를 뺀다.

    예외에 걸린 것도 빼버리지 않고 excused=True 로 함께 돌려준다.
    목록에서 사라지면 "예외가 몇 개나 쌓여 있는지" 를 아무도 모르게 된다.
    """
    excused = excused or {}
    # set 으로 받으면 사유가 없는 dict 로 바꿔서 아래를 한 갈래로 만든다.
    if not hasattr(excused, "get"):
        excused = {key: "" for key in excused}

    found = []
    for check in CHECKS:
        for yielded in check["fn"](snap):
            # 점검 함수는 (리소스, 설명) 을 내놓는다. 같은 항목이라도
            # 상황에 따라 무게가 다를 때는 (리소스, 설명, 심각도) 로
            # 세 개를 내놓을 수 있다. IMDSv1 이 그렇다 - 붙은 역할이
            # 있으면 훔칠 자격증명이 있다는 뜻이라 무게가 달라진다.
            if len(yielded) == 3:
                resource_id, detail, severity = yielded
            else:
                resource_id, detail = yielded
                severity = check["severity"]
            # 리소스 하나짜리 예외를 먼저 본다. 계정 전체 예외보다
            # 구체적인 쪽이 사유도 정확하다.
            key = next(
                (k for k in ((check["id"], resource_id), (check["id"], ""))
                 if k in excused),
                None,
            )
            found.append({
                "check_id": check["id"],
                "title": check["title"],
                "severity": severity,
                "standard": check["standard"],
                "why": check["why"],
                "resource_id": resource_id,
                "detail": detail,
                "excused": key is not None,
                "excuse_reason": excused.get(key, "") if key else "",
            })

    found.sort(key=lambda v: (SEVERITY_ORDER[v["severity"]], v["check_id"], v["resource_id"]))
    return found


def evaluate(snapshot_id, account_id=None):
    """스냅샷 하나를 점검한다.

    계정을 주면 그 고객사의 구성 표준을 함께 반영한다. 필수 태그는
    고객사마다 다른데 REQUIRED_TAGS 는 코드에 박힌 값이라, 고객사가
    정한 것이 있으면 그쪽이 이긴다(런북·SLA 와 같은 규칙).
    """
    meta = {}
    if account_id:
        # 표준을 못 읽는다고 점검 전체가 멎으면 안 된다. 그때는 기본값으로
        # 돈다 - 다만 그건 '고객사 표준을 지킨다' 가 아니므로 meta 에
        # 남겨서 화면이 그 사실을 말할 수 있게 한다.
        try:
            from app.accounts import get_account
            from app.standards import required_tags_for

            account = get_account(account_id)
            if account and account.get("customer"):
                tags = required_tags_for(account["customer"])
                if tags:
                    meta["required_tags"] = tags
                    meta["required_tags_from"] = account["customer"]
        except Exception:                        # noqa: BLE001
            meta["required_tags_from"] = ""

    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur, "resources"):
            raise ComplianceError("resources 테이블이 없습니다.")

        # 예외 조회를 먼저 끝낸다. 같은 커서로 중간에 다른 질의를 던지면
        # 앞선 결과가 덮어써진다 - 이 프로젝트에서 두 번 겪은 실수다.
        excused = {}
        if _table_ready(cur, "compliance_exceptions") and account_id:
            cur.execute(
                "SELECT check_id, resource_id, reason FROM compliance_exceptions "
                "WHERE account_id = %s AND expires_at > now()",
                (account_id,),
            )
            excused = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        snap = load_snapshot(cur, snapshot_id, meta=meta)

    return run_checks(snap, excused), snap


def summarize(violations, snap=None):
    """심각도별 개수. 예외로 뺀 것과 못 돌린 항목은 따로 센다.

    snap 을 주면 "수집하지 않아 점검하지 못한 항목" 수도 함께 센다.
    이 수가 0 이 아니면 '위반 0건' 을 '안전' 으로 읽으면 안 된다.
    """
    live = [v for v in violations if not v["excused"]]
    counts = {s: 0 for s in SEVERITIES}
    for v in live:
        counts[v["severity"]] += 1
    return {
        "total": len(live),
        "by_severity": counts,
        "excused": len(violations) - len(live),
        "worst": next((s for s in SEVERITIES if counts[s]), None),
        "skipped": len(coverage(snap)["skipped"]) if snap is not None else 0,
    }


# ----------------------------------------------------------------------
# 시간축 - Config 가 잘 답해주지 않는 부분
# ----------------------------------------------------------------------

def timeline(account_id, region, limit=20):
    """이 계정+리전의 최근 스냅샷들을 순서대로 점검한다.

    스냅샷마다 '어떤 (항목, 리소스) 가 위반이었나' 를 집합으로 들고 있다가
    이웃한 스냅샷끼리 비교해서 새로 생긴 것과 사라진 것을 뽑는다.
    """
    with _connect() as conn, conn.cursor() as cur:
        if not _table_ready(cur, "resources"):
            raise ComplianceError("resources 테이블이 없습니다.")

        excused = {}
        if _table_ready(cur, "compliance_exceptions"):
            cur.execute(
                "SELECT check_id, resource_id, reason FROM compliance_exceptions "
                "WHERE account_id = %s AND expires_at > now()",
                (account_id,),
            )
            excused = {(r[0], r[1]): r[2] for r in cur.fetchall()}

        cur.execute(
            """
            SELECT snapshot_id, collected_at FROM resource_snapshots
            WHERE account_id = %s AND region = %s AND complete
            ORDER BY collected_at DESC LIMIT %s
            """,
            (account_id, region, limit),
        )
        snaps = list(reversed(_rows(cur)))   # 오래된 것부터

        points = []
        for s in snaps:
            snap = load_snapshot(cur, s["snapshot_id"])
            violations = run_checks(snap, excused)
            live = {(v["check_id"], v["resource_id"]) for v in violations if not v["excused"]}
            points.append({
                "snapshot_id": s["snapshot_id"],
                "collected_at": s["collected_at"],
                "resources": len(snap),
                # keys 는 예외를 뺀 것. 새로 생김/해소됨 비교에 쓴다.
                "keys": live,
                # all_keys 는 예외까지 포함한 것. "언제부터 이랬나" 는
                # 예외로 덮어둔 항목에도 답할 수 있어야 한다 - 오히려
                # 예외를 연장할지 판단할 때 그 날짜가 필요하다.
                "all_keys": {(v["check_id"], v["resource_id"]) for v in violations},
                "summary": summarize(violations),
            })

    # 이웃한 스냅샷 비교.
    for i, p in enumerate(points):
        before = points[i - 1]["keys"] if i else set()
        p["opened"] = sorted(p["keys"] - before)
        p["closed"] = sorted(before - p["keys"])
        # 가장 오래된 스냅샷은 비교 대상이 없어서 전부 '새로 생김' 으로 나온다.
        # 그대로 두면 그날 위반이 쏟아진 것처럼 읽힌다.
        p["first"] = (i == 0)

    return points


def recurring(points):
    """고쳤다가 다시 열린 것.

    닫힌 적이 있는데 그 뒤에 다시 열린 (항목, 리소스). 지금 상태만 보면
    처음 열린 것과 세 번째 열린 것이 똑같아 보인다. 재발은 대개
    "고쳤다" 는 보고가 임시 조치였거나, 무언가가 되돌리고 있다는 뜻이다.
    """
    counts = {}
    for p in points:
        if p.get("first"):
            # 첫 스냅샷은 비교 대상이 없다. 여기서 세면 한 번만 열린 것도
            # 두 번으로 잡혀서 재발이 아닌 것이 재발로 올라온다.
            continue
        for key in p["opened"]:
            counts[key] = counts.get(key, 0) + 1

    out = []
    for key, times in counts.items():
        if times < 2:
            continue
        check = CHECKS_BY_ID.get(key[0], {})
        out.append({
            "check_id": key[0],
            "title": check.get("title", key[0]),
            "severity": check.get("severity", "low"),
            "resource_id": key[1],
            "times": times,
        })
    out.sort(key=lambda v: (-v["times"], SEVERITY_ORDER.get(v["severity"], 9)))
    return out


def first_seen(points):
    """(항목, 리소스) 마다 언제부터 위반이었는지.

    지금도 위반인 것만 돌려준다. "이거 언제부터 이랬어요?" 는
    고객사 보고에서 반드시 나오는 질문이다.

    예외로 덮어둔 항목도 함께 센다. 예외를 연장할지 판단하려면
    그게 언제부터 그랬는지를 알아야 한다.
    """
    if not points:
        return {}
    current = points[-1].get("all_keys", points[-1]["keys"])
    out = {}
    for p in points:
        for key in p.get("all_keys", p["keys"]):
            if key in current and key not in out:
                out[key] = p["collected_at"]
    return out
