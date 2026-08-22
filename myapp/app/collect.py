# app/collect.py
# AWS 리소스를 한 벌 읽어와 스냅샷 항목 목록으로 만든다.
# 원래 cli.py 안에 있었지만, 작업 기록 화면에서도 같은 수집이 필요해져서 꺼냈다.
# 여기에는 click 이 없다 - CLI 와 웹 양쪽에서 그대로 쓸 수 있어야 하기 때문이다.

import random


# 이 수집기가 훑는 리소스 종류.
#
# 스냅샷에 함께 적어둔다. 그래야 컴플라이언스 점검이 "RDS 를 봤는데 깨끗함"
# 과 "RDS 를 아예 안 봤음" 을 구분할 수 있다. 둘 다 화면에서는 '0' 으로
# 보이는데, 뜻은 정반대다.
#
# 여기에 종류를 추가할 때는 aws_resources() 와 demo 쪽을 함께 고쳐야 한다.
# 목록에만 적고 실제로 안 모으면, 없는 것을 '봤다' 고 주장하는 셈이 된다.
COLLECTED_TYPES = ("ec2:instance", "ec2:security_group", "s3:bucket")


class CollectError(Exception):
    """수집에 실패했을 때. CLI 는 ClickException 으로, 웹은 화면 안내로 바꾼다."""


# ----------------------------------------------------------------------
# 데모 수집
# ----------------------------------------------------------------------
# 자격증명 없이도 '어제와 오늘의 차이' 를 만들어보기 위한 합성 리소스다.
# 이전 스냅샷이 있으면 그걸 가져와 일부만 바꾼다.

def _baseline(account_id):
    """첫 수집에서 쓸 기준 인프라."""
    return [
        {"resource_id": f"i-0a1b2c3d", "resource_type": "ec2:instance",
         "attributes": {"instance_type": "t3.medium", "state": "running",
                        "security_groups": ["sg-web"],
                        "tags": {"Name": "web-01", "Env": "prod"},
                        "public_ip": "203.0.113.10", "imds": "required",
                        "imds_endpoint": "enabled", "iam_profile": "web-role"}},
        {"resource_id": f"i-0e4f5a6b", "resource_type": "ec2:instance",
         "attributes": {"instance_type": "t3.small", "state": "running",
                        "security_groups": ["sg-web"],
                        "tags": {"Name": "web-02", "Env": "prod"},
                        "public_ip": None, "imds": "optional",
                        "imds_endpoint": "enabled", "iam_profile": "web-role"}},
        {"resource_id": "sg-web", "resource_type": "ec2:security_group",
         "attributes": {"name": "web-sg", "vpc": "vpc-0aaa",
                        "ingress": ["443/tcp:0.0.0.0/0", "22/tcp:10.0.0.0/8"]}},
        {"resource_id": "sg-db", "resource_type": "ec2:security_group",
         "attributes": {"name": "db-sg", "vpc": "vpc-0aaa",
                        "ingress": ["5432/tcp:10.0.1.0/24"]}},
        {"resource_id": f"{account_id}-assets", "resource_type": "s3:bucket",
         "attributes": {"public_access_blocked": True, "versioning": "Enabled",
                        "encryption": "AES256"}},
        {"resource_id": f"{account_id}-logs", "resource_type": "s3:bucket",
         "attributes": {"public_access_blocked": True, "versioning": "Disabled",
                        "encryption": "None"}},
    ]


def _mutate(previous, drift):
    """이전 리소스를 조금 바꾼다. drift 확률로 항목마다 변경/삭제가 일어난다."""
    items = []
    for item in previous:
        roll = random.random()
        if roll > drift:
            items.append(item)
            continue

        attrs = dict(item["attributes"])
        kind = item["resource_type"]
        if kind == "ec2:instance":
            roll2 = random.random()
            if roll2 < 0.4:
                attrs["instance_type"] = random.choice(
                    ["t3.small", "t3.medium", "t3.large", "m5.large"]
                )
            elif roll2 < 0.7:
                attrs["state"] = random.choice(["running", "stopped"])
            else:
                # 고쳤다가 다시 열리는 상황을 만든다. 시간축의 '재발' 이
                # 실제로 잡히는지 데모 자료로도 볼 수 있어야 한다.
                attrs["imds"] = random.choice(["required", "optional"])
        elif kind == "ec2:security_group":
            ingress = list(attrs.get("ingress", []))
            if random.random() < 0.5:
                ingress.append(f"{random.choice([80, 3306, 8080])}/tcp:0.0.0.0/0")
            elif ingress:
                ingress.pop()
            attrs["ingress"] = ingress
        elif kind == "s3:bucket":
            attrs["versioning"] = random.choice(["Enabled", "Disabled"])
        items.append({**item, "attributes": attrs})

    # 가끔 새 리소스가 생긴다.
    if random.random() < drift:
        items.append({
            "resource_id": f"i-0{random.randint(10**6, 10**7 - 1):x}",
            "resource_type": "ec2:instance",
            "attributes": {"instance_type": "t3.micro", "state": "running",
                           "security_groups": ["sg-web"],
                           "tags": {"Name": "batch-tmp", "Env": "dev"},
                           "public_ip": "203.0.113.77", "imds": "optional",
                           "imds_endpoint": "enabled", "iam_profile": ""},
        })
    return items


def demo_resources(uri, account_id, region, drift=0.15):
    """합성 리소스 목록을 만든다.

    drift=0 이면 이전 스냅샷을 그대로 복사한다. 작업 기록에서 '작업 전' 을
    찍을 때 쓰는 값이다. 아무 작업도 안 했는데 리소스가 저절로 바뀌면
    증적이 의미를 잃기 때문이다.
    """
    import psycopg

    with psycopg.connect(uri) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.resources')")
        if cur.fetchone()[0] is None:
            raise CollectError(
                "resources 테이블이 없습니다. flask --app run init-db 를 먼저 실행하세요."
            )
        cur.execute(
            """
            SELECT snapshot_id FROM resource_snapshots
            WHERE complete AND source = 'demo' AND account_id = %s AND region = %s
            ORDER BY snapshot_id DESC LIMIT 1
            """,
            (account_id, region),
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
        return _baseline(account_id)
    if drift <= 0:
        return [dict(i) for i in previous]
    return _mutate(previous, drift)


# ----------------------------------------------------------------------
# 실제 AWS 수집
# ----------------------------------------------------------------------
# 읽기 전용 호출만 한다(describe/list/get). ReadOnlyAccess 수준이면 충분하다.
#
# 주의: 이 경로는 검증되지 않았다. 자격증명이 있는 환경에서 직접 확인해야 한다.

def aws_resources(region, env=None):
    """실제 AWS 에서 리소스를 읽어온다.

    env: aws_session.get_env() 가 돌려준 임시 자격증명 환경변수.
         주면 그 계정으로, 없으면 기본 자격증명 체인으로 읽는다.
         고객사 계정을 읽으려면 반드시 env 를 줘야 한다.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise CollectError("boto3 가 설치되어 있지 않습니다.")

    kwargs = {"region_name": region}
    if env:
        # 파일이나 프로필을 거치지 않고 값으로 직접 넘긴다.
        kwargs.update(
            aws_access_key_id=env.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY"),
            aws_session_token=env.get("AWS_SESSION_TOKEN") or None,
        )

    items = []
    try:
        account_id = boto3.client("sts", **kwargs).get_caller_identity()["Account"]
        ec2 = boto3.client("ec2", **kwargs)

        # paginator 를 쓴다. 리소스가 많으면 한 번에 다 오지 않는다.
        for page in ec2.get_paginator("describe_instances").paginate():
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    # 아래 세 값은 describe_instances 응답에 이미 들어 있다.
                    # API 를 한 번도 더 부르지 않고 점검 항목이 늘어난다.
                    #
                    # imds: IMDSv1 이 열려 있으면 애플리케이션의 SSRF 한 방으로
                    #   인스턴스 역할의 임시 자격증명이 통째로 나간다.
                    # public_ip: 보안그룹이 열려 있어도 퍼블릭 IP 가 없으면
                    #   인터넷에서 직접 닿지는 않는다. 이 값이 있어야
                    #   '열려 있을 수 있다' 와 '지금 닿는다' 를 가른다.
                    # iam_profile: 붙은 역할이 있으면 IMDSv1 의 피해 범위가
                    #   그 역할의 권한 범위가 된다.
                    meta = inst.get("MetadataOptions", {})
                    profile = (inst.get("IamInstanceProfile") or {}).get("Arn", "")
                    items.append({
                        "resource_id": inst["InstanceId"],
                        "resource_type": "ec2:instance",
                        "attributes": {
                            "instance_type": inst.get("InstanceType"),
                            "state": inst.get("State", {}).get("Name"),
                            "security_groups": [g["GroupId"] for g in inst.get("SecurityGroups", [])],
                            "tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])},
                            "subnet": inst.get("SubnetId"),
                            # 키가 없으면 '모름', None 이면 '없음' 이다.
                            # 이 앱은 그 둘을 구분해서 다룬다.
                            "public_ip": inst.get("PublicIpAddress"),
                            "imds": meta.get("HttpTokens"),
                            "imds_endpoint": meta.get("HttpEndpoint"),
                            "iam_profile": profile.split("/")[-1] if profile else "",
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

        s3 = boto3.client("s3", **kwargs)
        for bucket in s3.list_buckets().get("Buckets", []):
            name = bucket["Name"]
            attrs = {}
            try:
                cfg = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
                attrs["public_access_blocked"] = all(cfg.values())
            except ClientError:
                # 설정 자체가 없는 경우. '차단 안 됨' 으로 본다.
                attrs["public_access_blocked"] = False
            try:
                attrs["versioning"] = s3.get_bucket_versioning(Bucket=name).get("Status", "Disabled")
            except ClientError:
                attrs["versioning"] = "Unknown"
            try:
                rules = s3.get_bucket_encryption(Bucket=name)[
                    "ServerSideEncryptionConfiguration"]["Rules"]
                attrs["encryption"] = (
                    rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
                    if rules else "None"
                )
            except ClientError:
                # ServerSideEncryptionConfigurationNotFoundError 도 여기로 온다.
                # 설정이 없는 것과 못 읽은 것을 구분하지 못하는데, 점검에서는
                # 둘 다 "확인되지 않음" 으로 다뤄야 해서 같은 값으로 둔다.
                attrs["encryption"] = "None"
            items.append({"resource_id": name, "resource_type": "s3:bucket", "attributes": attrs})

    except (BotoCoreError, ClientError) as e:
        raise CollectError(f"AWS 호출에 실패했습니다: {e}")

    return items, account_id
