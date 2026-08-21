# app/cli.py
# `flask --app run <명령>` 으로 실행할 수 있는 자체 CLI 명령들. DB 초기화처럼
# 웹 요청과 무관하지만 앱 설정(DB 주소 등)이 필요한 작업을 여기에 모은다.

import json
import os
import random
from datetime import datetime, timedelta, timezone

import click
from flask import current_app

# Lambda 핸들러의 정규화 함수를 그대로 가져다 쓴다.
# 샘플 데이터도 실제와 똑같은 경로를 거치게 하려는 것이다.
from api.normalize_handler import normalize
from app.resources import save_snapshot, psycopg_uri

# schema.sql 은 프로젝트 루트의 db/ 에 있다.
# app/cli.py 기준으로 두 단계 위로 올라가야 루트다.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(_PROJECT_ROOT, "db", "schema.sql")


def _psycopg_uri():
    """SQLAlchemy 형식 URI 를 psycopg 가 이해하는 형식으로 바꾼다.

    설정에는 "postgresql+psycopg://..." 로 적혀 있는데, 가운데 "+psycopg" 는
    SQLAlchemy 에게 어떤 드라이버를 쓸지 알려주는 표시일 뿐이다.
    psycopg 에 직접 넘길 때는 빼야 한다.
    """
    return current_app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def register_cli(app):
    """앱에 CLI 명령들을 등록한다. create_app() 에서 호출한다."""

    @app.cli.command("init-db")
    def init_db():
        """db/schema.sql 을 실행해 테이블과 인덱스를 만든다.

        이 함수 안에서 current_app.config 를 그냥 쓸 수 있는 이유:
        Flask CLI 명령은 실행될 때 자동으로 '앱 컨텍스트' 안에서 돌기 때문이다.
        (웹 요청이 아닌데도 앱 설정에 접근할 수 있는 게 이 덕분이다.)
        """
        try:
            import psycopg
        except ImportError:
            raise click.ClickException(
                "psycopg 가 설치되어 있지 않습니다. pip install -r requirements.txt 를 실행하세요."
            )

        if not os.path.exists(SCHEMA_PATH):
            raise click.ClickException(f"스키마 파일을 찾을 수 없습니다: {SCHEMA_PATH}")

        with open(SCHEMA_PATH, encoding="utf-8") as f:
            sql = f.read()

        uri = _psycopg_uri()
        try:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
        except psycopg.OperationalError as e:
            # 접속 자체가 안 되는 경우(DB 안 떠 있음, 비밀번호 틀림 등)
            raise click.ClickException(
                f"DB 에 접속하지 못했습니다.\n  {e}\n"
                "  docker compose up -d 로 DB 가 떠 있는지 확인하세요."
            )

        click.echo(f"완료: {SCHEMA_PATH} 적용됨")

    @app.cli.command("db-check")
    def db_check():
        """DB 접속과 테이블 상태를 점검한다. 설정이 맞는지 빠르게 확인할 때 쓴다."""
        try:
            import psycopg
        except ImportError:
            raise click.ClickException("psycopg 가 설치되어 있지 않습니다.")

        uri = _psycopg_uri()
        # 비밀번호가 화면과 로그에 찍히지 않도록 가린다.
        safe = uri
        if "@" in safe and "//" in safe:
            head, tail = safe.split("//", 1)
            if "@" in tail:
                creds, host = tail.split("@", 1)
                user = creds.split(":")[0]
                safe = f"{head}//{user}:***@{host}"
        click.echo(f"접속 대상: {safe}")

        try:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    click.echo(f"서버: {cur.fetchone()[0].split(',')[0]}")

                    cur.execute("SELECT to_regclass('public.events')")
                    if cur.fetchone()[0] is None:
                        click.echo("events 테이블: 없음 -> flask --app run init-db 를 실행하세요")
                        return

                    cur.execute("SELECT count(*) FROM events")
                    total = cur.fetchone()[0]
                    cur.execute(
                        "SELECT severity, count(*) FROM events GROUP BY severity ORDER BY 2 DESC"
                    )
                    rows = cur.fetchall()
        except psycopg.OperationalError as e:
            raise click.ClickException(f"DB 에 접속하지 못했습니다.\n  {e}")

        click.echo(f"events 테이블: 있음 ({total} 건)")
        for severity, count in rows:
            click.echo(f"  {severity:9} {count}")

    @app.cli.command("seed-events")
    @click.option("--count", default=200, help="만들 이벤트 수 (기본 200)")
    @click.option("--hours", default=48, help="몇 시간에 걸쳐 흩뿌릴지 (기본 48)")
    @click.option("--clear", is_flag=True, help="기존 이벤트를 모두 지우고 시작")
    def seed_events(count, hours, clear):
        """대시보드를 채워볼 샘플 이벤트를 만들어 넣는다.

        원본 표기를 일부러 제각각으로 만들어서(FATAL, p1, msg 등)
        Lambda 의 정규화가 실제로 동작하는 것도 함께 확인할 수 있게 했다.
        """
        try:
            import psycopg
        except ImportError:
            raise click.ClickException(
                "psycopg 가 설치되어 있지 않습니다. pip install -r requirements.txt 를 실행하세요."
            )

        # 실제로 들어올 법한 값들. 표기가 제각각인 것이 핵심이다.
        sources = ["web-01", "web-02", "pay-api", "api-gw", "batch", "db-primary"]
        types = ["disk", "http", "deploy", "auth", "query"]
        # (원본 심각도 표기, 가중치) - info 가 가장 흔하고 critical 이 드물게
        severities = [
            ("FATAL", 3), ("p1", 2),
            ("error", 8), ("high", 4),
            ("warn", 14), ("p3", 6),
            ("info", 40), ("debug", 13),
        ]
        messages = [
            "디스크 사용률 {n}% 초과", "응답 지연 {n}ms 감지", "결제 실패율 {n}% 급증",
            "메모리 사용률 {n}%", "커넥션 풀 {n}% 점유", "배포 완료 (빌드 #{n})",
            "헬스체크 실패 {n}회 연속", "느린 쿼리 {n}ms",
        ]

        sev_values = [s for s, _ in severities]
        sev_weights = [w for _, w in severities]
        now = datetime.now(timezone.utc)

        records = []
        for i in range(count):
            # 시간은 최근일수록 촘촘하게. 그냥 균등분포로 두면 그래프가 밋밋하다.
            offset = random.random() ** 1.6 * hours
            occurred = now - timedelta(hours=offset)
            raw = {
                # 필드 이름도 일부러 섞는다. 정규화가 별칭을 흡수하는지 보기 위함이다.
                random.choice(["msg", "message", "text"]):
                    random.choice(messages).format(n=random.randint(1, 999)),
                random.choice(["level", "severity", "priority"]):
                    random.choices(sev_values, weights=sev_weights)[0],
                random.choice(["source", "service", "origin"]): random.choice(sources),
                random.choice(["type", "kind"]): random.choice(types),
                "occurred_at": occurred.isoformat(),
                "seeded": True,
            }
            records.append(normalize(raw))

        uri = _psycopg_uri()
        try:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    if clear:
                        cur.execute("DELETE FROM events")
                        click.echo("기존 이벤트를 모두 삭제했습니다.")

                    # executemany 로 한 번에 보낸다. 건마다 왕복하면 느리다.
                    cur.executemany(
                        """
                        INSERT INTO events (
                            event_id, event_type, source, severity,
                            message, occurred_at, received_at, fingerprint, meta
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        [
                            (
                                r["event_id"], r["event_type"], r["source"], r["severity"],
                                r["message"], r["occurred_at"], r["received_at"],
                                r["fingerprint"], json.dumps(r["meta"], ensure_ascii=False),
                            )
                            for r in records
                        ],
                    )
                    cur.execute("SELECT count(*) FROM events")
                    total = cur.fetchone()[0]
                    cur.execute(
                        "SELECT severity, count(*) FROM events "
                        "GROUP BY severity ORDER BY 2 DESC"
                    )
                    rows = cur.fetchall()
        except psycopg.errors.UndefinedTable:
            raise click.ClickException(
                "events 테이블이 없습니다. flask --app run init-db 를 먼저 실행하세요."
            )
        except psycopg.OperationalError as e:
            raise click.ClickException(
                f"DB 에 접속하지 못했습니다.\n  {e}"
            )

        click.echo(f"{count} 건을 최근 {hours} 시간에 걸쳐 넣었습니다. (현재 총 {total} 건)")
        for severity, c in rows:
            click.echo(f"  {severity:9} {c}")
        click.echo("대시보드에서 확인하세요: http://127.0.0.1:5000/dashboard/")

    @app.cli.command("collect-resources")
    @click.option("--demo", is_flag=True, help="AWS 대신 합성 리소스를 만들어 넣는다")
    @click.option("--drift", default=0.15, help="--demo 에서 변경이 일어날 확률 (기본 0.15)")
    @click.option("--region", default=None, help="수집 리전 (기본: AWS_REGION 설정값)")
    def collect_resources(demo, drift, region):
        """AWS 리소스 상태를 한 벌 수집해 스냅샷으로 저장한다.

        두 번 이상 실행하면 /resources/ 화면에서 스냅샷 간 차이를 볼 수 있다.
        --demo 는 AWS 없이 합성 리소스를 만들며, 두 번째 실행부터 일부를
        무작위로 바꿔서 diff 가 어떻게 보이는지 확인할 수 있게 한다.
        """
        region = region or current_app.config["AWS_REGION"]
        uri = psycopg_uri()

        if demo:
            items, account_id = _demo_resources(uri, drift)
            source = "demo"
        else:
            items, account_id = _aws_resources(region)
            source = "aws"

        try:
            snapshot_id = save_snapshot(
                uri, items, account_id=account_id, region=region, source=source
            )
        except Exception as e:
            if type(e).__module__.split(".")[0] == "psycopg":
                raise click.ClickException(
                    f"DB 작업에 실패했습니다.\n  {e}\n"
                    "  resources 테이블이 없다면 flask --app run init-db 를 실행하세요."
                )
            raise

        click.echo(f"스냅샷 #{snapshot_id} 저장 ({source}, {region}, 리소스 {len(items)}개)")
        click.echo("차이 보기: http://127.0.0.1:5000/resources/")


def _demo_resources(uri, drift):
    """합성 리소스 목록을 만든다.

    이전 스냅샷이 있으면 그걸 가져와 일부만 바꾼다.
    그래야 '어제와 오늘의 차이' 가 그럴듯하게 나온다.
    """
    import json
    import psycopg

    account_id = "123456789012"

    with psycopg.connect(uri) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.resources')")
        if cur.fetchone()[0] is None:
            raise click.ClickException(
                "resources 테이블이 없습니다. flask --app run init-db 를 먼저 실행하세요."
            )
        cur.execute(
            "SELECT snapshot_id FROM resource_snapshots "
            "WHERE complete AND source = 'demo' ORDER BY snapshot_id DESC LIMIT 1"
        )
        row = cur.fetchone()
        previous = []
        if row:
            cur.execute(
                "SELECT resource_id, resource_type, attributes FROM resources "
                "WHERE snapshot_id = %s",
                (row[0],),
            )
            previous = [
                {"resource_id": r[0], "resource_type": r[1], "attributes": r[2]}
                for r in cur.fetchall()
            ]

    if not previous:
        # 첫 수집: 기준이 되는 인프라를 만든다.
        return [
            {"resource_id": "i-0a1b2c3d", "resource_type": "ec2:instance",
             "attributes": {"instance_type": "t3.medium", "state": "running",
                            "security_groups": ["sg-web"], "tags": {"Name": "web-01", "Env": "prod"}}},
            {"resource_id": "i-0e4f5a6b", "resource_type": "ec2:instance",
             "attributes": {"instance_type": "t3.small", "state": "running",
                            "security_groups": ["sg-app"], "tags": {"Name": "app-01", "Env": "prod"}}},
            {"resource_id": "sg-web", "resource_type": "ec2:security_group",
             "attributes": {"ingress": ["80/tcp:0.0.0.0/0", "443/tcp:0.0.0.0/0"], "vpc": "vpc-main"}},
            {"resource_id": "sg-app", "resource_type": "ec2:security_group",
             "attributes": {"ingress": ["8080/tcp:sg-web"], "vpc": "vpc-main"}},
            {"resource_id": "app-logs", "resource_type": "s3:bucket",
             "attributes": {"public_access_blocked": True, "versioning": "Enabled",
                            "encryption": "AES256"}},
            {"resource_id": "db-prod", "resource_type": "rds:instance",
             "attributes": {"engine": "postgres", "class": "db.t3.medium",
                            "multi_az": False, "public": False}},
            {"resource_id": "role-app", "resource_type": "iam:role",
             "attributes": {"policies": ["AmazonS3ReadOnlyAccess"], "max_session": 3600}},
        ], account_id

    # 이후 수집: 이전 것을 복사한 뒤 확률적으로 변화를 준다.
    import copy
    import random

    items = copy.deepcopy(previous)

    # 수집할 때마다 값이 달라지지만 의미는 없는 필드.
    # 정규화가 이걸 걷어내는지 확인하는 용도이기도 하다.
    for it in items:
        it["attributes"]["LastModified"] = _now_iso()

    mutations = [
        ("i-0a1b2c3d", "instance_type", "t3.large"),
        ("sg-web", "ingress", ["80/tcp:0.0.0.0/0", "443/tcp:0.0.0.0/0", "22/tcp:0.0.0.0/0"]),
        ("app-logs", "public_access_blocked", False),
        ("db-prod", "multi_az", True),
        ("role-app", "policies", ["AmazonS3FullAccess"]),
    ]
    for resource_id, field, value in mutations:
        if random.random() < drift:
            for it in items:
                if it["resource_id"] == resource_id:
                    it["attributes"][field] = value

    # 리소스가 생기거나 사라지는 경우
    if random.random() < drift:
        items.append({
            "resource_id": f"i-0new{random.randint(1000, 9999)}",
            "resource_type": "ec2:instance",
            "attributes": {"instance_type": "t3.micro", "state": "running",
                           "security_groups": ["sg-app"], "tags": {"Name": "worker", "Env": "prod"}},
        })
    if random.random() < drift and len(items) > 3:
        items = [it for it in items if it["resource_id"] != "i-0e4f5a6b"]

    return items, account_id


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _aws_resources(region):
    """실제 AWS 에서 리소스를 읽어온다.

    읽기 전용 호출만 한다(describe/list/get).
    필요한 권한은 ReadOnlyAccess 수준이면 충분하다.

    주의: 이 경로는 검증되지 않았다. 자격증명이 있는 환경에서 직접 확인해야 한다.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise click.ClickException("boto3 가 설치되어 있지 않습니다.")

    items = []
    try:
        account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]

        ec2 = boto3.client("ec2", region_name=region)

        # paginator 를 쓴다. 리소스가 많으면 한 번에 다 오지 않는다.
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    items.append({
                        "resource_id": inst["InstanceId"],
                        "resource_type": "ec2:instance",
                        "attributes": {
                            "instance_type": inst.get("InstanceType"),
                            "state": inst.get("State", {}).get("Name"),
                            "security_groups": [g["GroupId"] for g in inst.get("SecurityGroups", [])],
                            "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                            "subnet": inst.get("SubnetId"),
                        },
                    })

        for page in ec2.get_paginator("describe_security_groups").paginate():
            for sg in page["SecurityGroups"]:
                items.append({
                    "resource_id": sg["GroupId"],
                    "resource_type": "ec2:security_group",
                    "attributes": {
                        "name": sg.get("GroupName"),
                        "vpc": sg.get("VpcId"),
                        "ingress": [
                            f'{p.get("FromPort")}/{p.get("IpProtocol")}:{r.get("CidrIp")}'
                            for p in sg.get("IpPermissions", [])
                            for r in p.get("IpRanges", [])
                        ],
                    },
                })

        s3 = boto3.client("s3", region_name=region)
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            attrs = {}
            try:
                blocked = s3.get_public_access_block(Bucket=name)
                cfg = blocked["PublicAccessBlockConfiguration"]
                attrs["public_access_blocked"] = all(cfg.values())
            except ClientError:
                # 설정 자체가 없는 경우. '차단 안 됨' 으로 본다.
                attrs["public_access_blocked"] = False
            try:
                attrs["versioning"] = s3.get_bucket_versioning(Bucket=name).get("Status", "Disabled")
            except ClientError:
                attrs["versioning"] = "Unknown"
            items.append({"resource_id": name, "resource_type": "s3:bucket", "attributes": attrs})

    except (BotoCoreError, ClientError) as e:
        raise click.ClickException(f"AWS 호출에 실패했습니다.\n  {e}")

    return items, account_id
