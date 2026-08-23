# api/normalize_handler.py
# AWS Lambda 핸들러. 들쭉날쭉한 원본 이벤트를 정해진 형태로 정규화하고, DB 에 적재한 뒤,
# 심각도가 높으면 알람(SNS)을 발송한다. Flask 를 import 하지 않는다 - 순수 파이썬 함수다.

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from api.adapters import adapt

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
    # 계정 번호. 보내는 쪽마다 이름이 다르다.
    #   recipientaccountid : CloudTrail
    #   awsaccountid       : CloudWatch 알람이 SNS 로 보낼 때
    "account": "account_id",
    "accountid": "account_id",
    "aws_account_id": "account_id",
    "awsaccountid": "account_id",
    "recipientaccountid": "account_id",
}

# AWS 계정 번호는 12자리 숫자다. 형식이 다르면 계정으로 쓰지 않는다.
# 엉뚱한 값이 들어오면 이벤트가 없는 계정에 묶여 조용히 사라진다.
ACCOUNT_ID_RE = re.compile(r"^\d{12}$")

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


# 어댑터가 알아보지 못한 페이로드에 붙는 표시.
# event_type 으로 두는 이유: 화면과 질의가 이미 event_type 으로 걸러내고 있어서
# 새 컬럼을 만들지 않아도 "정규화 못 한 것만" 을 뽑을 수 있다.
UNPARSED_TYPE = "unparsed"

# 원본을 meta 에 남길 때의 상한. 이걸 넘으면 잘라서 넣되, 잘렸다는 사실을 같이 남긴다.
RAW_KEEP = 4000


def _shape_key(payload):
    """모르는 페이로드를 '모양' 으로 묶는 열쇠.

    최상위 키 이름들만 본다. 값은 매번 다르지만 모양은 발신자마다 같다.

    지문에 페이로드 내용을 쓰면 안 된다. 같은 발신자가 보낸 100건이 전부
    다른 지문이 되어 목록이 밀리고, 사람이 "이 발신자 하나만 붙이면 된다" 는
    사실을 볼 수 없게 된다. 모양으로 묶으면 미확인 목록에 한 줄로 뜬다.
    """
    if not isinstance(payload, dict):
        return f"type:{type(payload).__name__}"
    return "keys:" + ",".join(sorted(str(k).strip().lower() for k in payload))


def _unparsed(payload, source_hint=""):
    """알아보지 못한 페이로드를 버리지 않고 레코드로 만든다.

    이 프로젝트에서 제일 피해온 실패 방식이 '조용히 사라지는 것' 이다.
    새 발신자가 생겼을 때 예외로 떨어뜨리면 알람은 없던 일이 되고,
    없어졌다는 사실조차 아무 화면에도 안 뜬다.

    심각도는 warning 으로 고정한다.
      - info 로 두면 목록 아래로 가라앉아 아무도 안 본다.
      - error/critical 로 두면 파싱 실패로 사람을 새벽에 깨우게 된다.
        내용을 못 읽었으니 급한 건인지 아닌지 우리는 모른다.
      warning 은 ALARM_SEVERITIES 밖이라 SNS 로도 나가지 않는다.
    """
    try:
        dumped = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        dumped = str(payload)

    meta = {
        "unparsed": True,
        "raw_payload": dumped[:RAW_KEEP],
        "shape": _shape_key(payload),
    }
    if len(dumped) > RAW_KEEP:
        meta["raw_truncated"] = True

    source = str(source_hint or "unknown").strip().lower() or "unknown"
    message = f"정규화 못 함: {dumped}"

    # 지문은 모양으로 직접 계산한다. _fingerprint() 를 거치면 message_template
    # 이 키 이름까지 마스킹해서(<hex>, <n>) 서로 다른 발신자가 한 지문으로
    # 뭉칠 수 있다. 여기서는 모양이 곧 열쇠라 가공하면 안 된다.
    fingerprint = hashlib.sha256(
        f"{UNPARSED_TYPE}|{source}|{meta['shape']}".encode("utf-8")
    ).hexdigest()[:16]

    return {
        "event_id": uuid.uuid4().hex,
        "event_type": UNPARSED_TYPE,
        "source": source,
        "severity": "warning",
        "message": message[:1000],
        # 페이로드 안의 시각을 믿고 읽을 수 없다(어떤 키가 시각인지 모른다).
        # 받은 시각을 쓴다.
        "occurred_at": _now_iso(),
        "received_at": _now_iso(),
        "fingerprint": fingerprint,
        "account_id": "",
        "meta": meta,
    }


def normalize(raw):
    """원본 이벤트(dict)를 표준 형태로 바꾼다.

    이 함수는 부수효과가 전혀 없다(DB 도 네트워크도 건드리지 않는다).
    그래서 테스트하기 쉽고, Flask 쪽에서도 그대로 재사용할 수 있다.

    순서: 어댑터 -> 별칭표 -> 표준 레코드.
    어댑터는 AWS 가 실제로 보내는 모양(CloudWatch/GuardDuty/Health/EventBridge)을
    표준 필드로 옮긴다. 별칭표만으로는 이 모양들을 못 읽었다 -
    CloudWatch 페이로드와 별칭표가 겹치는 이름은 AWSAccountId 하나뿐이었다.

    어댑터에는 모델을 쓰지 않는다. 정규화는 결정적이어야 한다.
    같은 페이로드에 매번 같은 지문이 나와야 억제도 집계도 런북 연결도 성립한다.
    """
    if not isinstance(raw, dict):
        raise ValueError("이벤트는 JSON 객체여야 합니다.")

    # 0) AWS 발신자 모양을 표준 필드로 옮긴다. 봉투(SNS/SQS)도 여기서 벗는다.
    #    아무도 못 알아보면 (원본, None) 이 온다 - 예외가 아니다.
    adapted, adapter_name = adapt(raw)
    if not isinstance(adapted, dict):
        return _unparsed(adapted)

    # 1) 필드 이름을 표준 이름으로 바꾼다.
    data = {}
    for key, value in adapted.items():
        standard_key = FIELD_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        # 이미 표준 이름으로 들어온 값이 있으면 그걸 우선한다.
        if standard_key not in data or data[standard_key] in (None, ""):
            data[standard_key] = value

    # 2) 문자열 값의 앞뒤 공백을 정리한다.
    message = str(data.get("message") or "").strip()
    if not message:
        # 여기서 두 가지를 갈라야 한다.
        #
        #  (a) 우리가 아는 필드로 왔는데 내용이 비었다 -> 보관할 게 없다. 거부한다.
        #      보낸 쪽이 우리 형식을 쓰고 있으니 고쳐 보내면 된다.
        #  (b) 아는 필드가 아예 없다 -> 모양을 모를 뿐 내용은 다 들어 있다.
        #      버리면 그 내용이 사라진다. "정규화 못 함" 으로 적재한다.
        if "message" in data:
            raise ValueError("message 는 비어 있을 수 없습니다.")
        return _unparsed(adapted, source_hint=data.get("source") or "")

    event_type = str(data.get("event_type") or "unknown").strip().lower()
    source = str(data.get("source") or "unknown").strip().lower()

    # 계정 번호. 형식이 맞을 때만 표준 필드로 올린다.
    account_id = str(data.get("account_id") or "").strip()
    if not ACCOUNT_ID_RE.match(account_id):
        account_id = ""

    # 3) 심각도를 4단계 중 하나로 맞춘다. 모르는 값이면 info 로 떨어뜨린다.
    raw_severity = str(data.get("severity") or "info").strip().lower()
    severity = SEVERITY_MAP.get(raw_severity, "info")

    # 4) 표준 필드에 없는 나머지는 meta 에 몰아넣어 버리지 않고 보관한다.
    #    지문이 meta 를 보기 때문에(알람 이름) 레코드보다 먼저 만들어야 한다.
    meta = {
        k: v for k, v in data.items()
        if k not in ("message", "event_type", "source", "severity",
                     "occurred_at", "account_id")
    }
    # 형식이 안 맞아 버린 값은 meta 에 남겨둔다. 보낸 쪽이 무엇을 줬는지
    # 확인할 수 있어야 고칠 수 있다.
    if data.get("account_id") and not account_id:
        meta["account_id_raw"] = data["account_id"]
    # 어느 어댑터가 읽었는지 남긴다. 지문이 이상할 때 어디를 고칠지 이게 알려준다.
    if adapter_name:
        meta["adapter"] = adapter_name

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
        "account_id": account_id,
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
                    event_id, event_type, source, severity, message,
                    occurred_at, received_at, fingerprint, account_id, meta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    record["event_id"], record["event_type"], record["source"],
                    record["severity"], record["message"], record["occurred_at"],
                    record["received_at"], record["fingerprint"],
                    record.get("account_id", ""),
                    json.dumps(record["meta"], ensure_ascii=False),
                ),
            )
    return {"stored": True}


def check_suppression(fingerprint):
    """이 지문의 알람을 지금 보내도 되는지 본다.

    돌려주는 값: (보내도 되는가, 이유)

    억제는 지문이 제 역할을 해야만 성립한다. 메시지 전문을 해시하던 시절에는
    같은 알람이 매번 다른 지문이 되어 이 판정이 항상 통과했다.

    DATABASE_URL 이 없으면 판정하지 않고 통과시킨다. 억제 못 해서 알람이
    더 가는 것보다, 판정에 실패했다고 알람을 막는 쪽이 훨씬 위험하다.
    """
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return True, ""

    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.alarm_rules')")
        if cur.fetchone()[0] is None:
            return True, ""

        cur.execute(
            "SELECT window_minutes, muted FROM alarm_rules WHERE fingerprint = %s",
            (fingerprint,),
        )
        row = cur.fetchone()
        if row is None:
            return True, ""

        window_minutes, muted = row
        if muted:
            _count_suppressed(cur, fingerprint)
            return False, "이 알람은 muted 로 설정되어 있습니다"

        if window_minutes <= 0:
            return True, ""

        # 마지막 발송이 창 안에 있으면 건너뛴다.
        cur.execute(
            """
            SELECT last_alarmed_at
              FROM alarm_state
             WHERE fingerprint = %s
               AND last_alarmed_at > now() - make_interval(mins => %s)
            """,
            (fingerprint, window_minutes),
        )
        if cur.fetchone() is not None:
            _count_suppressed(cur, fingerprint)
            return False, f"{window_minutes}분 안에 같은 알람을 이미 보냈습니다"

    return True, ""


def _count_suppressed(cur, fingerprint):
    """억제된 횟수를 센다. 규칙이 실제로 얼마나 줄여줬는지 나중에 보기 위함이다."""
    cur.execute(
        """
        INSERT INTO alarm_state (fingerprint, last_alarmed_at, sent_count, suppressed_count)
        VALUES (%s, now(), 0, 1)
        ON CONFLICT (fingerprint) DO UPDATE
            SET suppressed_count = alarm_state.suppressed_count + 1
        """,
        (fingerprint,),
    )


def _mark_sent(fingerprint):
    """발송 시각을 기록한다. 다음 억제 판정의 기준이 된다."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        return

    import psycopg

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.alarm_state')")
        if cur.fetchone()[0] is None:
            return
        cur.execute(
            """
            INSERT INTO alarm_state (fingerprint, last_alarmed_at, sent_count)
            VALUES (%s, now(), 1)
            ON CONFLICT (fingerprint) DO UPDATE
                SET last_alarmed_at = now(),
                    sent_count = alarm_state.sent_count + 1
            """,
            (fingerprint,),
        )


def send_alarm(record):
    """심각도가 높으면 SNS 로 알람을 발송한다. 토픽이 없으면 건너뛴다."""
    if record["severity"] not in ALARM_SEVERITIES:
        return {"alarmed": False, "reason": f"심각도 {record['severity']} 는 알람 대상 아님"}

    # 억제 판정은 토픽 확인보다 먼저 한다. 토픽이 없어 어차피 안 나가는
    # 상황에서도 '억제됐다' 는 사실은 세어둬야 규칙 효과를 볼 수 있다.
    allowed, reason = check_suppression(record["fingerprint"])
    if not allowed:
        return {"alarmed": False, "suppressed": True, "reason": reason}

    topic_arn = os.environ.get("ALARM_SNS_TOPIC_ARN", "")
    if not topic_arn:
        return {"alarmed": False, "reason": "ALARM_SNS_TOPIC_ARN 미설정"}

    import boto3

    boto3.client("sns").publish(
        TopicArn=topic_arn,
        Subject=f"[{record['severity'].upper()}] {record['source']} - {record['event_type']}"[:100],
        Message=json.dumps(record, ensure_ascii=False, indent=2),
    )
    _mark_sent(record["fingerprint"])
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
