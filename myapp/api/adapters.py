# api/adapters.py
# AWS 가 실제로 보내는 모양을 표준 필드로 옮긴다.
#
# ── 왜 필요했나 ────────────────────────────────────────────────────
# 정규화기의 별칭표(FIELD_ALIASES)는 msg/level/service 처럼 일반적인 이름만
# 안다. 그런데 CloudWatch 알람은 AlarmName/NewStateReason/NewStateValue 로
# 보내고, 겹치는 이름이 AWSAccountId 하나뿐이다. 그래서 진짜 CloudWatch
# 페이로드를 넣으면 "message 는 비어 있을 수 없습니다" 로 통째로 거부됐다.
#
# 지금까지 쌓인 200건은 전부 seed-events 가 우리 모양으로 만들어 넣은
# 것이고, 진짜 AWS 이벤트는 한 번도 통과한 적이 없었다.
#
# ── 모르는 형태를 버리지 않는다 ─────────────────────────────────────
# 어댑터가 못 알아본 페이로드를 예외로 떨어뜨리면, 새 발신자가 생길
# 때마다 알람이 조용히 사라진다. 그건 이 프로젝트에서 가장 피해온 일이다
# (무서운 건 실패가 아니라 침묵).
#
# 못 알아보면 원본을 그대로 meta 에 넣고 "정규화 못 함" 으로 적재한다.
# 화면에서 그 사실이 보이고, 사람이 어댑터를 하나 더 쓰면 된다.
#
# ── 왜 모델을 쓰지 않는가 ───────────────────────────────────────────
# 정규화는 결정적이어야 한다. 같은 페이로드에 같은 지문이 나와야 억제도
# 집계도 성립한다. 모델이 매번 조금씩 다르게 파싱하면 그 전제가 무너진다.
# (모르는 형태를 보고 "이렇게 매핑하면 될 것 같다" 는 초안을 만드는 데는
#  쓸 수 있다. 그건 사람이 확인해서 이 파일에 코드로 고정한다.)

import json
import re

# 리소스 id 로 인정할 모양. 지문에 쓰지 않고 meta 에만 넣는다 -
# 지문에 들어가면 인스턴스마다 다른 지문이 되어 같은 알람이 안 묶인다.
RESOURCE_ID_RE = re.compile(
    r"^(?:i|vol|sg|subnet|eni|ami|snap|vpc|rtb|igw|acl|fs|db|nat|tgw)-[0-9a-f]{6,}$",
    re.I,
)


def _first(mapping, *keys):
    """여러 후보 키 중 값이 있는 첫 번째. 발신자마다 이름이 조금씩 다르다."""
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def unwrap(raw):
    """SNS/SQS 봉투를 벗긴다.

    CloudWatch 알람은 SNS 를 거쳐 오고, 그때 실제 내용은
    Records[0].Sns.Message 안에 문자열로 들어 있다. 봉투째 넘기면
    어댑터가 알아볼 것이 하나도 없다.

    봉투가 아니면 그대로 돌려준다.
    """
    if not isinstance(raw, dict):
        return raw

    records = raw.get("Records")
    if isinstance(records, list) and records:
        first = records[0]
        if isinstance(first, dict):
            body = None
            if isinstance(first.get("Sns"), dict):
                body = first["Sns"].get("Message")
            elif "body" in first:              # SQS
                body = first["body"]
            if isinstance(body, str):
                try:
                    inner = json.loads(body)
                except ValueError:
                    # JSON 이 아니면 본문 자체가 메시지다. 버리지 않는다.
                    return {"message": body, "source": "sns"}
                if isinstance(inner, dict):
                    return inner
    return raw


# ----------------------------------------------------------------------
# 발신자별 어댑터
# ----------------------------------------------------------------------
# 각 어댑터는 (알아보는가, 표준 필드로 옮기기) 두 가지만 한다.
# 심각도 판정 같은 뒷일은 정규화기가 이어서 한다.

def _dimensions(trigger):
    """CloudWatch Trigger.Dimensions 를 {이름: 값} 으로.

    키 대소문자가 상황에 따라 다르다(name/Name). 둘 다 받는다.
    """
    out = {}
    for item in (trigger or {}).get("Dimensions") or []:
        if not isinstance(item, dict):
            continue
        name = _first(item, "name", "Name")
        value = _first(item, "value", "Value")
        if name and value:
            out[str(name)] = str(value)
    return out


def cloudwatch_alarm(raw):
    """CloudWatch 알람 (SNS 경유).

    NewStateValue 가 ALARM/OK/INSUFFICIENT_DATA 다. OK 도 이벤트로 받는다 -
    복구됐다는 사실도 당직자가 알아야 한다.
    """
    if not (raw.get("AlarmName") and "NewStateValue" in raw):
        return None

    trigger = raw.get("Trigger") or {}
    dims = _dimensions(trigger)
    state = str(raw.get("NewStateValue") or "").strip().upper()

    # 메시지는 이유를 우선한다. 이유가 없으면 설명, 그것도 없으면 이름.
    message = _first(raw, "NewStateReason", "AlarmDescription") or raw["AlarmName"]

    out = {
        "message": message,
        "event_type": "alarm",
        # 무엇이 낸 알람인가. 네임스페이스(AWS/EC2)가 가장 쓸모 있다.
        "source": trigger.get("Namespace") or "cloudwatch",
        # ALARM 이면 심각도를 정하지 않는다. 정규화기가 기본값(info)으로
        # 떨어뜨리는 것보다, 알람 이름·설명으로 사람이 규칙을 만드는 편이
        # 정확하다. 여기서 억지로 critical 로 올리면 전부 critical 이 된다.
        "severity": {"ALARM": "warning", "OK": "info",
                     "INSUFFICIENT_DATA": "warning"}.get(state, "info"),
        "occurred_at": raw.get("StateChangeTime"),
        "account_id": raw.get("AWSAccountId"),
        # 지문이 이 값을 본다(ALARM_NAME_KEYS). 메시지에 측정값이 섞여
        # 있어도 알람 이름으로 묶인다.
        "alarmname": raw["AlarmName"],
        "alarm_state": state,
        "metric": trigger.get("MetricName"),
        "namespace": trigger.get("Namespace"),
        "region": raw.get("Region"),
    }
    out.update(_resource_fields(dims))
    if dims:
        out["dimensions"] = dims
    return out


def _resource_fields(dims):
    """{이름: 값} 에서 리소스 id 를 뽑는다.

    이게 알람을 리소스에 붙이는 유일한 연결 고리다. 지금까지는 이벤트에
    리소스 id 가 없어서 "이 인스턴스에 어떤 알람이 왔나" 를 물을 수 없었다.

    지문에는 넣지 않는다. 넣으면 인스턴스마다 다른 지문이 되어 같은
    알람이 안 묶이고, 런북도 인스턴스마다 따로 써야 한다.
    """
    for name, value in (dims or {}).items():
        if RESOURCE_ID_RE.match(str(value)):
            return {"resource_id": str(value), "resource_dimension": name}
    return {}


def guardduty(raw):
    """GuardDuty 발견 항목 (EventBridge 경유).

    severity 가 1~10 숫자다. 정규화기의 SEVERITY_MAP 은 문자열만 알아서,
    여기서 4단계로 옮겨 준다.
    """
    detail = raw.get("detail")
    if not (isinstance(detail, dict) and raw.get("source") == "aws.guardduty"):
        return None

    score = detail.get("severity")
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0

    # AWS 가 문서에 적어둔 구간을 그대로 쓴다.
    if score >= 7:
        severity = "critical"
    elif score >= 4:
        severity = "error"
    elif score > 0:
        severity = "warning"
    else:
        severity = "info"

    resource = detail.get("resource") or {}
    instance = (resource.get("instanceDetails") or {}).get("instanceId")

    out = {
        "message": _first(detail, "description", "title") or detail.get("type", ""),
        "event_type": "security",
        "source": "guardduty",
        "severity": severity,
        "occurred_at": _first(detail, "updatedAt", "createdAt")
                       or raw.get("time"),
        "account_id": _first(raw, "account") or detail.get("accountId"),
        # 발견 유형이 알람 이름 역할을 한다.
        "alarmname": detail.get("type"),
        "guardduty_severity": score,
        "finding_id": detail.get("id"),
    }
    if instance:
        out["resource_id"] = instance
        out["resource_dimension"] = "instanceDetails"
    return out


def health(raw):
    """AWS Health 이벤트 (EventBridge 경유)."""
    detail = raw.get("detail")
    if not (isinstance(detail, dict) and raw.get("source") == "aws.health"):
        return None

    descriptions = detail.get("eventDescription") or []
    text = ""
    if isinstance(descriptions, list) and descriptions:
        first = descriptions[0]
        if isinstance(first, dict):
            text = first.get("latestDescription") or ""

    category = str(detail.get("eventTypeCategory") or "").lower()

    return {
        "message": text or detail.get("eventTypeCode", ""),
        "event_type": "health",
        "source": detail.get("service", "health").lower(),
        # issue 는 지금 문제, 나머지는 예정된 변경·계정 알림이다.
        "severity": "error" if category == "issue" else "warning",
        "occurred_at": _first(detail, "startTime") or raw.get("time"),
        "account_id": raw.get("account"),
        "alarmname": detail.get("eventTypeCode"),
        "health_category": detail.get("eventTypeCategory"),
        "region": raw.get("region"),
    }


def eventbridge(raw):
    """그 밖의 EventBridge 이벤트.

    GuardDuty·Health 를 먼저 보고, 안 걸리면 여기로 온다. detail-type 이
    알람 이름 역할을 한다.
    """
    if not (raw.get("source") and raw.get("detail-type")):
        return None

    detail = raw.get("detail")
    if isinstance(detail, dict):
        message = _first(detail, "description", "message", "reason", "state") or ""
        if not message:
            # 본문을 통째로 넣는다. 자르지 않는 것은 정규화기가 한다.
            message = json.dumps(detail, ensure_ascii=False)[:500]
    else:
        message = str(detail or "")

    out = {
        "message": message or raw["detail-type"],
        "event_type": "event",
        "source": str(raw["source"]).replace("aws.", ""),
        "occurred_at": raw.get("time"),
        "account_id": raw.get("account"),
        "alarmname": raw["detail-type"],
        "region": raw.get("region"),
        "detail": detail if not isinstance(detail, dict) else None,
    }
    # detail 안에 리소스 id 가 들어 있으면 붙인다(예: instance-id).
    # CloudWatch 차원에서 뽑는 것과 같은 이유다 - 이게 없으면 "이 인스턴스에
    # 어떤 이벤트가 있었나" 를 물을 수 없다.
    if isinstance(detail, dict):
        out.update(_resource_fields(
            {k: v for k, v in detail.items() if isinstance(v, str)}))
    return out


# 순서가 중요하다. 좁은 것부터 본다 - GuardDuty 와 Health 도 EventBridge
# 모양이라, eventbridge 를 먼저 두면 전부 거기로 빨려 들어간다.
ADAPTERS = (
    ("cloudwatch_alarm", cloudwatch_alarm),
    ("guardduty", guardduty),
    ("health", health),
    ("eventbridge", eventbridge),
)


def adapt(raw):
    """봉투를 벗기고 알아보는 어댑터에 맡긴다.

    돌려주는 것: (표준 필드 dict, 어댑터 이름)
    아무도 못 알아보면 (원본 그대로, None).

    못 알아본 것을 예외로 만들지 않는다. 부르는 쪽이 그 사실을 보고
    "정규화 못 함" 으로 적재한다.
    """
    inner = unwrap(raw)
    if not isinstance(inner, dict):
        return raw, None

    for name, fn in ADAPTERS:
        try:
            mapped = fn(inner)
        except Exception:                        # noqa: BLE001
            # 어댑터 하나가 이상한 페이로드에 터져도 나머지는 봐야 한다.
            continue
        if mapped:
            # None 값은 빼고 넘긴다. 정규화기가 빈 값을 기본값으로
            # 채우는데, None 이 섞여 있으면 그 자리가 "unknown" 이 된다.
            return {k: v for k, v in mapped.items() if v not in (None, "")}, name

    return inner, None
