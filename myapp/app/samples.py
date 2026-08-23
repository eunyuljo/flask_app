# app/samples.py
# 샘플 알람을 '실제로 들어오는 모양' 그대로 만든다.
#
# ── 왜 새로 만들었나 ────────────────────────────────────────────────
# 예전 seed-events 는 우리 형식(msg/level/source)으로 이벤트를 만들어
# events 테이블에 직접 INSERT 했다. 두 가지가 잘못됐다.
#
#   1) 모양이 진짜가 아니었다. 그래서 진짜 CloudWatch 페이로드가 정규화에
#      거부되고 있다는 사실이 프로젝트 내내 드러나지 않았다. 화면은
#      200건으로 가득 차 있었고, 그 200건은 전부 우리가 우리 형식으로
#      만들어 넣은 것이었다.
#   2) account_id 를 안 넣었다. 그래서 SLA 가 200건을 전부 '미귀속' 으로
#      셌고, 고객사 축 화면은 영원히 비어 있었다.
#
# 여기서는 AWS 가 실제로 보내는 모양을 만들고, 정규화기를 그대로 통과시킨다.
# 어댑터가 깨지면 샘플 생성도 깨진다. 그게 목적이다 - 샘플이 진짜 경로를
# 밟지 않으면 샘플로서 값어치가 없다.

import json
import random
from datetime import datetime, timedelta, timezone


# ----------------------------------------------------------------------
# 발신자 구성
# ----------------------------------------------------------------------
# 실제 MSP 가 받는 알람은 한 곳에서 오지 않는다. 이 비율이 화면을
# 현실에 가깝게 만든다.
#
#   cloudwatch  가장 많다. 대부분의 알람이 여기서 온다.
#   자체 모니터링 MSP 는 보통 자기 모니터링 서버를 따로 돌린다.
#                여기가 우리 형식(msg/level)으로 보내는 쪽이다.
#   guardduty   드물지만 오면 심각하다.
#   health      AWS 가 알려주는 것. 드물다.
#   eventbridge 상태 변화 알림.
#   모르는 발신자 새로 붙인 도구가 아직 어댑터가 없는 상태.
#                0으로 두면 "정규화 못 함" 화면을 영영 못 본다.
MIX = (
    ("cloudwatch", 55),
    ("monitoring", 22),
    ("eventbridge", 10),
    ("guardduty", 5),
    ("health", 3),
    ("unknown", 5),
)

# 같은 알람 이름이 반복돼야 지문이 묶이고, 묶여야 순위표·억제·런북이
# 의미를 갖는다. 이름을 매번 새로 만들면 200건이 200종이 된다.
CLOUDWATCH_ALARMS = (
    # (알람 이름, 네임스페이스, 지표, 설명, 임계값)
    ("prod-web-cpu-high",      "AWS/EC2",        "CPUUtilization",
     "웹 서버 CPU 80% 초과", 80.0),
    ("prod-web-statuscheck",   "AWS/EC2",        "StatusCheckFailed",
     "인스턴스 상태 검사 실패", 1.0),
    ("prod-db-connections",    "AWS/RDS",        "DatabaseConnections",
     "DB 커넥션 수 임계 초과", 180.0),
    ("prod-db-freestorage",    "AWS/RDS",        "FreeStorageSpace",
     "DB 여유 공간 부족", 10737418240.0),
    ("prod-alb-5xx",           "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count",
     "ALB 5xx 응답 급증", 50.0),
    ("prod-alb-unhealthy",     "AWS/ApplicationELB", "UnHealthyHostCount",
     "비정상 대상 발생", 1.0),
    ("prod-lambda-errors",     "AWS/Lambda",     "Errors",
     "Lambda 오류율 상승", 10.0),
    ("batch-queue-backlog",    "AWS/SQS",        "ApproximateAgeOfOldestMessage",
     "배치 큐 적체", 900.0),
)

# 자체 모니터링 서버가 보내는 형식. 필드 이름을 일부러 섞는다 -
# 정규화의 별칭표가 실제로 흡수하는지 눈으로 볼 수 있게.
MONITORING_MESSAGES = (
    "디스크 사용률 {n}% 초과", "응답 지연 {n}ms 감지", "메모리 사용률 {n}%",
    "커넥션 풀 {n}% 점유", "헬스체크 실패 {n}회 연속", "느린 쿼리 {n}ms",
    "배포 완료 (빌드 #{n})",
)
MONITORING_HOSTS = ("web-01", "web-02", "pay-api", "api-gw", "batch", "db-primary")
# (원본 표기, 가중치) - info 가 흔하고 critical 이 드물게
MONITORING_SEVERITIES = (
    ("FATAL", 2), ("p1", 1), ("error", 7), ("high", 3),
    ("warn", 13), ("p3", 5), ("info", 38), ("debug", 11),
)

GUARDDUTY_FINDINGS = (
    ("UnauthorizedAccess:EC2/SSHBruteForce", 8,
     "EC2 인스턴스가 SSH 무차별 대입 공격에 연루되었습니다."),
    ("Recon:EC2/PortProbeUnprotectedPort", 5,
     "보호되지 않은 포트가 외부에서 탐지되었습니다."),
    ("CryptoCurrency:EC2/BitcoinTool.B", 8,
     "암호화폐 채굴 관련 도메인과 통신했습니다."),
    ("Policy:IAMUser/RootCredentialUsage", 3,
     "루트 자격증명이 사용되었습니다."),
)

HEALTH_EVENTS = (
    ("AWS_EC2_INSTANCE_STORE_DRIVE_PERFORMANCE_DEGRADED", "EC2", "issue",
     "인스턴스 스토어 드라이브 성능이 저하되었습니다."),
    ("AWS_RDS_MAINTENANCE_SCHEDULED", "RDS", "scheduledChange",
     "예정된 유지 관리가 계획되었습니다."),
    ("AWS_EC2_INSTANCE_REBOOT_MAINTENANCE_SCHEDULED", "EC2", "scheduledChange",
     "인스턴스 재부팅 유지 관리가 예정되었습니다."),
)

EVENTBRIDGE_EVENTS = (
    ("aws.ec2", "EC2 Instance State-change Notification", {"state": "terminated"}),
    ("aws.ec2", "EC2 Instance State-change Notification", {"state": "stopped"}),
    ("aws.rds", "RDS DB Instance Event", {"Message": "DB instance restarted"}),
)

# 아직 어댑터가 없는 발신자. 실무에서 새 도구를 붙이면 꼭 이런 게 하나 생긴다.
UNKNOWN_SENDERS = (
    {"alert_source": "k8s-operator", "cluster": "prod-eks",
     "reason": "PodEvicted", "count": 3},
    {"probe": "synthetic-canary", "endpoint": "/healthz",
     "latency_ms": 4210, "ok": False},
)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def sns_wrap(inner, when, topic_account="000000000000"):
    """CloudWatch 알람이 SNS 구독으로 올 때의 봉투.

    봉투째 오는 것이 정상이다. 벗겨서 보내면 실제와 달라진다.

    봉투의 시각도 알람 시각에서 뽑는다. now() 를 쓰면 --seed 를 줘도 매번
    다른 값이 나와서, 화면을 두고 "이 알람 말인데" 를 할 수 없다.
    실제로도 SNS 타임스탬프는 상태 변화 직후다.
    """
    return {
        "Records": [{
            "EventSource": "aws:sns",
            "EventSubscriptionArn":
                f"arn:aws:sns:ap-northeast-2:{topic_account}:ops-alarms:sub",
            "Sns": {
                "Type": "Notification",
                "TopicArn": f"arn:aws:sns:ap-northeast-2:{topic_account}:ops-alarms",
                "Subject": "ALARM 알림",
                "Message": json.dumps(inner, ensure_ascii=False),
                "Timestamp": _iso(when + timedelta(seconds=2)),
            },
        }]
    }


def cloudwatch(rng, account_id, region, when, resource_id=None):
    """CloudWatch 알람 페이로드 하나."""
    name, namespace, metric, description, threshold = rng.choice(CLOUDWATCH_ALARMS)

    # OK 도 섞는다. 복구됐다는 사실도 당직자가 받는다. 다만 드물게 -
    # 실제로도 ALARM 이 훨씬 많다.
    state = rng.choices(["ALARM", "OK", "INSUFFICIENT_DATA"], weights=[76, 20, 4])[0]
    value = round(threshold * rng.uniform(1.02, 1.9), 1)

    dimensions = []
    if resource_id:
        dimensions.append({"name": _dimension_name(namespace), "value": resource_id})

    inner = {
        "AlarmName": name,
        "AlarmDescription": description,
        "AWSAccountId": account_id,
        "AlarmConfigurationUpdatedTimestamp": _iso(when - timedelta(days=30)),
        "NewStateValue": state,
        "NewStateReason": (
            f"Threshold Crossed: 1 datapoint [{value} ({when:%d/%m/%y %H:%M:%S})] "
            f"was greater than the threshold ({threshold})."
            if state == "ALARM" else
            f"Threshold Crossed: 1 datapoint was not greater than the threshold ({threshold})."
        ),
        "StateChangeTime": _iso(when),
        "Region": region,
        "AlarmArn": f"arn:aws:cloudwatch:{region}:{account_id}:alarm:{name}",
        "OldStateValue": "OK" if state == "ALARM" else "ALARM",
        "Trigger": {
            "MetricName": metric,
            "Namespace": namespace,
            "StatisticType": "Statistic",
            "Statistic": "AVERAGE",
            "Unit": None,
            "Dimensions": dimensions,
            "Period": 300,
            "EvaluationPeriods": 2,
            "ComparisonOperator": "GreaterThanThreshold",
            "Threshold": threshold,
        },
    }
    return sns_wrap(inner, when)


def _dimension_name(namespace):
    """네임스페이스마다 리소스를 가리키는 차원 이름이 다르다."""
    return {
        "AWS/EC2": "InstanceId",
        "AWS/RDS": "DBInstanceIdentifier",
        "AWS/ApplicationELB": "LoadBalancer",
        "AWS/Lambda": "FunctionName",
        "AWS/SQS": "QueueName",
    }.get(namespace, "InstanceId")


def monitoring(rng, when):
    """자체 모니터링 서버가 보내는 형식.

    필드 이름을 매번 다르게 고른다. 정규화의 별칭표가 이걸 흡수한다.
    계정 정보가 없는 것도 실제와 같다 - 자체 모니터링은 보통 호스트 이름만 안다.
    """
    values = [s for s, _ in MONITORING_SEVERITIES]
    weights = [w for _, w in MONITORING_SEVERITIES]
    return {
        rng.choice(["msg", "message", "text"]):
            rng.choice(MONITORING_MESSAGES).format(n=rng.randint(1, 999)),
        rng.choice(["level", "severity", "priority"]):
            rng.choices(values, weights=weights)[0],
        rng.choice(["source", "service", "origin"]): rng.choice(MONITORING_HOSTS),
        rng.choice(["type", "kind"]):
            rng.choice(["disk", "http", "deploy", "auth", "query"]),
        "occurred_at": _iso(when),
    }


def guardduty(rng, account_id, region, when, resource_id=None):
    finding_type, severity, description = rng.choice(GUARDDUTY_FINDINGS)
    detail = {
        "schemaVersion": "2.0",
        "accountId": account_id,
        "region": region,
        "id": rng.randbytes(8).hex(),
        "type": finding_type,
        "severity": severity,
        "title": description,
        "description": description,
        "createdAt": _iso(when - timedelta(minutes=20)),
        "updatedAt": _iso(when),
        "service": {"count": rng.randint(1, 40)},
    }
    if resource_id:
        detail["resource"] = {"resourceType": "Instance",
                              "instanceDetails": {"instanceId": resource_id}}
    return {
        "version": "0", "id": rng.randbytes(8).hex(),
        "detail-type": "GuardDuty Finding", "source": "aws.guardduty",
        "account": account_id, "time": _iso(when), "region": region,
        "resources": [], "detail": detail,
    }


def health(rng, account_id, region, when):
    code, service, category, description = rng.choice(HEALTH_EVENTS)
    return {
        "version": "0", "id": rng.randbytes(8).hex(),
        "detail-type": "AWS Health Event", "source": "aws.health",
        "account": account_id, "time": _iso(when), "region": region,
        "resources": [],
        "detail": {
            "eventArn": f"arn:aws:health:{region}::event/{service}/{code}/{code}_1",
            "service": service,
            "eventTypeCode": code,
            "eventTypeCategory": category,
            "startTime": _iso(when),
            "eventDescription": [{"language": "ko_KR",
                                  "latestDescription": description}],
        },
    }


def eventbridge(rng, account_id, region, when, resource_id=None):
    source, detail_type, detail = rng.choice(EVENTBRIDGE_EVENTS)
    detail = dict(detail)
    if resource_id and source == "aws.ec2":
        detail["instance-id"] = resource_id
    return {
        "version": "0", "id": rng.randbytes(8).hex(),
        "detail-type": detail_type, "source": source,
        "account": account_id, "time": _iso(when), "region": region,
        "resources": [], "detail": detail,
    }


def unknown(rng, when):
    """어댑터가 아직 없는 발신자.

    이걸 0으로 두면 "정규화 못 함" 화면을 영영 못 본다. 실무에서 새 도구를
    붙이면 꼭 하나 생기는 상태이므로 샘플에도 있어야 한다.
    """
    payload = dict(rng.choice(UNKNOWN_SENDERS))
    payload["ts"] = int(when.timestamp())
    return payload


# ----------------------------------------------------------------------
# 조합
# ----------------------------------------------------------------------

def make(count, hours, accounts, resources=None, seed=None, now=None):
    """샘플 페이로드를 만든다. (원본 페이로드, 발신자 이름) 목록.

    accounts  : [{"account_id": ..., "regions": [...]}, ...]
                등록된 고객사 계정. 여기에 묶여야 SLA·리포트·고객사 화면이
                채워진다. 예전 샘플은 계정을 안 넣어서 전부 '미귀속' 이었다.
    resources : {계정: [리소스 id, ...]} 실제로 수집된 리소스.
                차원에 실제 있는 인스턴스를 넣어야 알람과 리소스가 이어진다.
    seed      : 주면 같은 값이 나온다. 화면을 두고 이야기할 때 필요하다.
    now       : 기준 시각. 안 주면 지금. seed 만으로는 완전히 같은 값이
                나오지 않는다 - 시각이 '지금부터 거슬러' 라서 부를 때마다
                다르다. 테스트는 여기를 고정해서 그 흔들림을 없앤다.

    DB 도 네트워크도 건드리지 않는다. 부르는 쪽이 적재를 맡는다.
    """
    rng = random.Random(seed)
    resources = resources or {}
    now = now or datetime.now(timezone.utc)

    kinds = [k for k, _ in MIX]
    weights = [w for _, w in MIX]

    out = []
    for _ in range(count):
        # 최근일수록 촘촘하게. 균등분포로 두면 추이 그래프가 밋밋하다.
        when = now - timedelta(hours=rng.random() ** 1.6 * hours)
        kind = rng.choices(kinds, weights=weights)[0]

        if kind == "monitoring":
            out.append((monitoring(rng, when), kind))
            continue
        if kind == "unknown":
            out.append((unknown(rng, when), kind))
            continue

        if not accounts:
            # 계정이 하나도 등록되어 있지 않으면 AWS 발신자를 만들 수 없다.
            # 계정 없는 알람을 지어내느니 자체 모니터링으로 대신한다.
            out.append((monitoring(rng, when), "monitoring"))
            continue

        account = rng.choice(accounts)
        account_id = account["account_id"]
        region = rng.choice(account.get("regions") or ["ap-northeast-2"])
        pool = resources.get(account_id) or []
        resource_id = rng.choice(pool) if pool else None

        if kind == "cloudwatch":
            out.append((cloudwatch(rng, account_id, region, when, resource_id), kind))
        elif kind == "guardduty":
            out.append((guardduty(rng, account_id, region, when, resource_id), kind))
        elif kind == "health":
            out.append((health(rng, account_id, region, when), kind))
        else:
            out.append((eventbridge(rng, account_id, region, when, resource_id), kind))

    return out
