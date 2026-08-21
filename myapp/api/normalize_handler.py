# api/normalize_handler.py
# AWS Lambda 핸들러. 들쭉날쭉한 원본 이벤트를 정해진 형태로 정규화하고, DB 에 적재한 뒤,
# 심각도가 높으면 알람(SNS)을 발송한다. Flask 를 import 하지 않는다 - 순수 파이썬 함수다.

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

# ----------------------------------------------------------------------
# 정규화 규칙
# ----------------------------------------------------------------------
# 실무에서 들어오는 이벤트는 보내는 쪽마다 필드 이름이 제각각이다.
#   어떤 시스템은 "level", 어떤 시스템은 "severity", 어떤 시스템은 "priority"
# 이걸 하나의 형태로 맞추는 게 정규화(normalize)다.

# 별칭 -> 표준 필드명
FIELD_ALIASES = {
    "level": "severity",
    "priority": "severity",
    "msg": "message",
    "description": "message",
    "text": "message",
    "type": "event_type",
    "kind": "event_type",
    "category": "event_type",
    "from": "source",
    "origin": "source",
    "service": "source",
    "timestamp": "occurred_at",
    "time": "occurred_at",
    "ts": "occurred_at",
}

# 심각도 표기 흔들림을 4단계로 통일한다.
SEVERITY_MAP = {
    "critical": "critical", "crit": "critical", "fatal": "critical",
    "p1": "critical", "5": "critical", "emergency": "critical",
    "error": "error", "err": "error", "high": "error", "p2": "error", "4": "error",
    "warn": "warning", "warning": "warning", "medium": "warning",
    "p3": "warning", "3": "warning",
    "info": "info", "information": "info", "low": "info",
    "debug": "info", "p4": "info", "2": "info", "1": "info",
}

# 이 심각도 이상이면 알람을 쏜다.
ALARM_SEVERITIES = {"critical", "error"}

VALID_SEVERITIES = ("critical", "error", "warning", "info")


def _now_iso():
    """항상 UTC 기준 ISO8601 문자열을 만든다.

    타임존 없는 시각(naive datetime)을 쓰면 서버 위치에 따라 값이 달라져서
    나중에 로그를 맞춰볼 때 반드시 문제가 된다.
    """
    return datetime.now(timezone.utc).isoformat()


def _normalize_timestamp(value):
    """제각각인 시각 표기를 ISO8601 로 맞춘다. 못 알아보면 현재 시각을 쓴다."""
    if not value:
        return _now_iso()

    # 유닉스 타임스탬프(숫자)로 들어오는 경우
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()

    text = str(value).strip()
    try:
        # "2026-08-20T14:00:00Z" 의 Z 는 fromisoformat 이 못 읽으므로 바꿔준다.
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _now_iso()

    # 타임존이 없으면 UTC 로 간주한다.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


# 지문을 만들기 전에 메시지에서 '매번 달라지는 부분'을 지우는 규칙.
# 위에서부터 순서대로 적용된다. 순서가 중요하다 - 숫자를 먼저 지우면
# UUID 나 인스턴스 ID 가 조각나서 알아볼 수 없게 된다.
MASK_RULES = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\barn:aws[a-z0-9-]*:\S+", re.I), "<arn>"),
    (re.compile(r"\b(?:i|vol|sg|subnet|eni|ami|snap|vpc|rtb|igw|acl|fs|db)-[0-9a-f]{6,}\b", re.I), "<id>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<time>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?"), "<n>"),
)

# meta 안에 '이 알람의 이름'이 들어 있으면 그걸 지문의 기준으로 삼는다.
# CloudWatch 알람은 AlarmName 이, Prometheus/Alertmanager 는 alertname 이 그 역할을 한다.
# 이름이 있으면 메시지 본문이 어떻게 흔들리든 같은 알람으로 묶인다.
ALARM_NAME_KEYS = ("alarmname", "alarm_name", "alertname", "alert_name")


def message_template(message):
    """메시지에서 매번 달라지는 값을 자리표시자로 바꾼 '틀'을 만든다.

        "CPU usage 92.4% on i-0abc123456789def0"
        -> "cpu usage <n>% on <id>"

    이 틀이 같으면 같은 종류의 사건으로 본다.
    """
    text = message.lower()
    for pattern, placeholder in MASK_RULES:
        text = pattern.sub(placeholder, text)
    # 자리표시자로 바뀌면서 생긴 공백 차이를 없앤다.
    return " ".join(text.split())


def _fingerprint(event_type, source, message, meta=None):
    """같은 종류의 이벤트인지 판단하는 지문(해시).

    같은 에러가 1분에 500번 들어와도 지문이 같아야, '같은 지문은 5분에 한 번만
    알람' 같은 억제 규칙이나 '이 알람이 최근 몇 번 났나' 집계가 성립한다.

    주의: 메시지를 그대로 해시하면 안 된다. 실제 알람 메시지에는 측정값이
    섞여 있어서("CPU 92%" / "CPU 87%") 매번 다른 지문이 나오고,
    그러면 모든 이벤트가 서로 다른 종류로 취급되어 억제도 집계도 무의미해진다.

    한계: 숫자를 전부 지우므로 "HTTP 500" 과 "HTTP 404" 도 같은 지문이 된다.
    상태 코드처럼 값 자체가 사건의 종류를 가르는 경우는 메시지 본문이 아니라
    meta 에 담아 보내야 구분된다(위 ALARM_NAME_KEYS 참고).
    """
    meta = meta or {}
    key = ""
    for name in ALARM_NAME_KEYS:
        for k, v in meta.items():
            if str(k).strip().lower() == name and str(v).strip():
                key = str(v).strip().lower()
                break
        if key:
            break

    if not key:
        key = message_template(message)

    raw = f"{event_type}|{source}|{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize(raw):
    """원본 이벤트(dict)를 표준 형태로 바꾼다.

    이 함수는 부수효과가 전혀 없다(DB 도 네트워크도 건드리지 않는다).
    그래서 테스트하기 쉽고, Flask 쪽에서도 그대로 재사용할 수 있다.
    """
    if not isinstance(raw, dict):
        raise ValueError("이벤트는 JSON 객체여야 합니다.")

    # 1) 필드 이름을 표준 이름으로 바꾼다.
    data = {}
    for key, value in raw.items():
        standard_key = FIELD_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        # 이미 표준 이름으로 들어온 값이 있으면 그걸 우선한다.
        if standard_key not in data or data[standard_key] in (None, ""):
            data[standard_key] = value

    # 2) 문자열 값의 앞뒤 공백을 정리한다.
    message = str(data.get("message") or "").strip()
    if not message:
        raise ValueError("message 는 비어 있을 수 없습니다.")

    event_type = str(data.get("event_type") or "unknown").strip().lower()
    source = str(data.get("source") or "unknown").strip().lower()

    # 3) 심각도를 4단계 중 하나로 맞춘다. 모르는 값이면 info 로 떨어뜨린다.
    raw_severity = str(data.get("severity") or "info").strip().lower()
    severity = SEVERITY_MAP.get(raw_severity, "info")

    # 4) 표준 필드에 없는 나머지는 meta 에 몰아넣어 버리지 않고 보관한다.
    #    지문이 meta 를 보기 때문에(알람 이름) 레코드보다 먼저 만들어야 한다.
    meta = {
        k: v for k, v in data.items()
        if k not in ("message", "event_type", "source", "severity", "occurred_at")
    }

    # 5) 표준 레코드 완성
    return {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "source": source,
        "severity": severity,
        "message": message[:1000],          # 너무 긴 메시지는 자른다
        "occurred_at": _normalize_timestamp(data.get("occurred_at")),
        "received_at": _now_iso(),
        "fingerprint": _fingerprint(event_type, source, message, meta),
        "meta": meta,
    }


# ----------------------------------------------------------------------
# 부수효과: DB 적재 / 알람 발송
# ----------------------------------------------------------------------
# 이 두 함수는 '연결 정보가 없으면 조용히 건너뛴다'.
# 덕분에 AWS 없이 로컬에서도 핸들러를 그대로 실행해볼 수 있다.

def store_event(record):
    """정규화된 레코드를 DB 에 적재한다. DATABASE_URL 이 없으면 건너뛴다."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return {"stored": False, "reason": "DATABASE_URL 미설정"}

    # psycopg 는 Lambda 레이어나 배포 패키지에 포함되어야 한다.
    import psycopg

    # 값을 SQL 문자열에 직접 이어붙이지 않고 %s 자리표시자로 넘긴다(SQL 인젝션 방지).
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (
                    event_id, event_type, source, severity,
                    message, occurred_at, received_at, fingerprint, meta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    record["event_id"], record["event_type"], record["source"],
                    record["severity"], record["message"], record["occurred_at"],
                    record["received_at"], record["fingerprint"],
                    json.dumps(record["meta"], ensure_ascii=False),
                ),
            )
    return {"stored": True}


def send_alarm(record):
    """심각도가 높으면 SNS 로 알람을 발송한다. 토픽이 없으면 건너뛴다."""
    if record["severity"] not in ALARM_SEVERITIES:
        return {"alarmed": False, "reason": f"심각도 {record['severity']} 는 알람 대상 아님"}

    topic_arn = os.environ.get("ALARM_SNS_TOPIC_ARN", "")
    if not topic_arn:
        return {"alarmed": False, "reason": "ALARM_SNS_TOPIC_ARN 미설정"}

    import boto3

    boto3.client("sns").publish(
        TopicArn=topic_arn,
        Subject=f"[{record['severity'].upper()}] {record['source']} - {record['event_type']}"[:100],
        Message=json.dumps(record, ensure_ascii=False, indent=2),
    )
    return {"alarmed": True, "topic": topic_arn}


# ----------------------------------------------------------------------
# Lambda 진입점
# ----------------------------------------------------------------------
def lambda_handler(event, context=None):
    """AWS Lambda 가 호출하는 함수.

    event   : 호출한 쪽이 넘긴 JSON (여기서는 원본 이벤트)
    context : 실행 환경 정보(남은 시간, 요청 ID 등). 로컬 실행에서는 None 이어도 된다.

    Lambda 핸들러는 결국 '인자 두 개를 받는 평범한 파이썬 함수'다.
    그래서 아래 lambda_client.py 의 로컬 모드에서 그냥 직접 호출할 수 있다.
    """
    try:
        record = normalize(event)
    except ValueError as e:
        # 잘못된 입력은 재시도해도 소용없으므로 에러로 되돌려준다.
        return {"ok": False, "error": str(e)}

    store_result = store_event(record)
    alarm_result = send_alarm(record)

    return {
        "ok": True,
        "record": record,
        "store": store_result,
        "alarm": alarm_result,
    }
