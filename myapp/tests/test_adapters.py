# tests/test_adapters.py
# AWS 가 실제로 보내는 페이로드가 정규화를 통과하는지.
#
# 이 파일이 생긴 이유: 지금까지 events 테이블에 쌓인 것은 전부 seed-events 가
# 우리 형식으로 만들어 넣은 것이었고, 진짜 CloudWatch 페이로드는 한 번도
# 통과한 적이 없었다. 별칭표와 겹치는 이름이 AWSAccountId 하나뿐이라
# "message 는 비어 있을 수 없습니다" 로 통째로 거부됐다.
#
# 그래서 여기 있는 페이로드는 손으로 예쁘게 만든 것이 아니라 AWS 문서에
# 실린 모양 그대로다. 모양이 바뀌면 이 테스트가 먼저 깨져야 한다.

import json

import pytest

from api import adapters
from api.normalize_handler import UNPARSED_TYPE, normalize


# ----------------------------------------------------------------------
# 진짜 페이로드
# ----------------------------------------------------------------------

CLOUDWATCH = {
    "AlarmName": "prod-web-cpu-high",
    "AlarmDescription": "EC2 CPU over 80% for 10 minutes",
    "AWSAccountId": "123456789012",
    "NewStateValue": "ALARM",
    "NewStateReason": (
        "Threshold Crossed: 1 datapoint [92.4 (20/08/26 14:00:00)] "
        "was greater than the threshold (80.0)."
    ),
    "StateChangeTime": "2026-08-20T14:05:31.000+0000",
    "Region": "Asia Pacific (Seoul)",
    "OldStateValue": "OK",
    "Trigger": {
        "MetricName": "CPUUtilization",
        "Namespace": "AWS/EC2",
        "Statistic": "AVERAGE",
        "Dimensions": [{"value": "i-0abc123456789def0", "name": "InstanceId"}],
        "Period": 300,
        "EvaluationPeriods": 2,
        "ComparisonOperator": "GreaterThanThreshold",
        "Threshold": 80.0,
    },
}


def sns_envelope(inner):
    """CloudWatch 알람이 SNS 를 거쳐 올 때의 봉투."""
    return {
        "Records": [{
            "EventSource": "aws:sns",
            "Sns": {
                "Type": "Notification",
                "TopicArn": "arn:aws:sns:ap-northeast-2:123456789012:ops-alarms",
                "Subject": 'ALARM: "prod-web-cpu-high" in Asia Pacific (Seoul)',
                "Message": json.dumps(inner),
                "Timestamp": "2026-08-20T14:05:32.000Z",
            },
        }]
    }


GUARDDUTY = {
    "version": "0",
    "id": "cd2d702e-ab31-411b-9344-793ce56b1bc7",
    "detail-type": "GuardDuty Finding",
    "source": "aws.guardduty",
    "account": "123456789012",
    "time": "2026-08-20T18:22:33Z",
    "region": "ap-northeast-2",
    "detail": {
        "schemaVersion": "2.0",
        "accountId": "123456789012",
        "id": "f1b2c3d4e5f6",
        "type": "UnauthorizedAccess:EC2/SSHBruteForce",
        "severity": 8,
        "title": "SSH brute force attacks against i-0abc123456789def0.",
        "description": "EC2 instance has been involved in SSH brute force attacks.",
        "createdAt": "2026-08-20T18:00:00.000Z",
        "updatedAt": "2026-08-20T18:22:00.000Z",
        "resource": {
            "resourceType": "Instance",
            "instanceDetails": {"instanceId": "i-0abc123456789def0"},
        },
    },
}

HEALTH = {
    "version": "0",
    "id": "7bf73129-1428-4cd3-a780-95db273d1602",
    "detail-type": "AWS Health Event",
    "source": "aws.health",
    "account": "123456789012",
    "time": "2026-08-20T06:27:57Z",
    "region": "ap-northeast-2",
    "detail": {
        "eventArn": "arn:aws:health:ap-northeast-2::event/EC2/AWS_EC2_X/AWS_EC2_X_1",
        "service": "EC2",
        "eventTypeCode": "AWS_EC2_INSTANCE_STORE_DRIVE_PERFORMANCE_DEGRADED",
        "eventTypeCategory": "issue",
        "startTime": "2026-08-20T05:01:10Z",
        "eventDescription": [
            {"language": "en_US",
             "latestDescription": "A instance store drive is degraded."}
        ],
    },
}

EVENTBRIDGE = {
    "version": "0",
    "id": "6a7e8feb-b491-4cf7-a9f1-bf3703467718",
    "detail-type": "EC2 Instance State-change Notification",
    "source": "aws.ec2",
    "account": "123456789012",
    "time": "2026-08-20T09:00:00Z",
    "region": "ap-northeast-2",
    "resources": ["arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0abc123456789def0"],
    "detail": {"instance-id": "i-0abc123456789def0", "state": "terminated"},
}

ALL_REAL = {
    "cloudwatch": (sns_envelope(CLOUDWATCH), "cloudwatch_alarm"),
    "guardduty": (GUARDDUTY, "guardduty"),
    "health": (HEALTH, "health"),
    "eventbridge": (EVENTBRIDGE, "eventbridge"),
}


class TestRealPayloadsPass:
    """이게 이 층이 생긴 이유다. 하나라도 실패하면 알람이 사라진다."""

    @pytest.mark.parametrize("name", sorted(ALL_REAL))
    def test_normalizes_without_error(self, name):
        payload, expected_adapter = ALL_REAL[name]
        record = normalize(payload)
        assert record["event_type"] != UNPARSED_TYPE, f"{name} 을 못 읽었다"
        assert record["meta"]["adapter"] == expected_adapter

    @pytest.mark.parametrize("name", sorted(ALL_REAL))
    def test_message_is_not_empty(self, name):
        assert normalize(ALL_REAL[name][0])["message"].strip()

    @pytest.mark.parametrize("name", sorted(ALL_REAL))
    def test_account_id_survives(self, name):
        # 계정이 안 붙으면 고객사에 못 묶이고, 그러면 SLA 도 리포트도 비어 있다.
        assert normalize(ALL_REAL[name][0])["account_id"] == "123456789012"

    def test_old_normalizer_would_have_rejected_cloudwatch(self):
        """어댑터를 빼면 예전 그대로 못 읽는다.

        어댑터가 정말 일을 하고 있는지를 확인한다. 별칭표만으로 CloudWatch 가
        통과한다면 이 층은 있을 이유가 없다.
        """
        aliased = {"msg", "message", "text", "description", "level",
                   "severity", "priority"}
        sent = {k.lower() for k in CLOUDWATCH}
        assert not (aliased & sent)


class TestEnvelope:
    def test_sns_envelope_is_unwrapped(self):
        assert adapters.unwrap(sns_envelope(CLOUDWATCH))["AlarmName"] == "prod-web-cpu-high"

    def test_sqs_body_is_unwrapped(self):
        raw = {"Records": [{"body": json.dumps({"AlarmName": "x", "NewStateValue": "OK"})}]}
        assert adapters.unwrap(raw)["AlarmName"] == "x"

    def test_non_json_body_becomes_the_message(self):
        """JSON 이 아니어도 본문을 버리지 않는다."""
        raw = {"Records": [{"Sns": {"Message": "디스크가 꽉 찼습니다"}}]}
        assert adapters.unwrap(raw)["message"] == "디스크가 꽉 찼습니다"

    def test_plain_payload_passes_through(self):
        payload = {"msg": "x"}
        assert adapters.unwrap(payload) is payload


class TestCloudWatch:
    def test_reason_becomes_the_message(self):
        assert "Threshold Crossed" in normalize(sns_envelope(CLOUDWATCH))["message"]

    def test_namespace_becomes_the_source(self):
        assert normalize(sns_envelope(CLOUDWATCH))["source"] == "aws/ec2"

    def test_dimension_becomes_a_resource(self):
        """알람을 리소스에 붙이는 유일한 연결 고리."""
        meta = normalize(sns_envelope(CLOUDWATCH))["meta"]
        assert meta["resource_id"] == "i-0abc123456789def0"
        assert meta["resource_dimension"] == "InstanceId"

    def test_dimension_key_case_does_not_matter(self):
        """AWS 가 name/Name 을 섞어 보낸다."""
        upper = json.loads(json.dumps(CLOUDWATCH))
        upper["Trigger"]["Dimensions"] = [
            {"Name": "InstanceId", "Value": "i-0abc123456789def0"}
        ]
        assert normalize(upper)["meta"]["resource_id"] == "i-0abc123456789def0"

    def test_state_change_time_is_used(self):
        assert normalize(sns_envelope(CLOUDWATCH))["occurred_at"].startswith("2026-08-20T14:05")

    def test_ok_state_is_kept_but_quiet(self):
        """복구도 이벤트로 받는다. 다만 알람으로 나가지는 않는다."""
        ok = dict(CLOUDWATCH, NewStateValue="OK",
                  NewStateReason="Threshold not crossed")
        record = normalize(ok)
        assert record["severity"] == "info"
        assert record["meta"]["alarm_state"] == "OK"

    def test_alarm_state_is_not_critical_by_default(self):
        """ALARM 을 전부 critical 로 올리면 전부 critical 이 된다.

        무엇이 급한지는 알람 이름·설명을 보고 사람이 규칙으로 정한다.
        """
        assert normalize(sns_envelope(CLOUDWATCH))["severity"] == "warning"


class TestFingerprintGrouping:
    """지문이 제 역할을 해야 억제도 집계도 런북 연결도 성립한다."""

    def test_same_alarm_different_measurement_groups(self):
        a = normalize(sns_envelope(CLOUDWATCH))
        other = dict(CLOUDWATCH, NewStateReason="Threshold Crossed: 1 datapoint [87.1] ...")
        b = normalize(sns_envelope(other))
        assert a["fingerprint"] == b["fingerprint"]

    def test_same_alarm_different_instance_groups(self):
        """리소스 id 는 meta 로만 간다.

        지문에 넣으면 인스턴스마다 다른 지문이 되어 런북을 인스턴스 수만큼
        써야 한다. 리소스는 meta 에 남아 있으니 잃는 것도 없다.
        """
        other = json.loads(json.dumps(CLOUDWATCH))
        other["Trigger"]["Dimensions"] = [
            {"name": "InstanceId", "value": "i-0999888877776666f"}
        ]
        a, b = normalize(CLOUDWATCH), normalize(other)
        assert a["fingerprint"] == b["fingerprint"]
        assert a["meta"]["resource_id"] != b["meta"]["resource_id"]

    def test_different_alarm_names_do_not_group(self):
        other = dict(CLOUDWATCH, AlarmName="prod-db-connections-high")
        assert normalize(CLOUDWATCH)["fingerprint"] != normalize(other)["fingerprint"]

    def test_guardduty_groups_by_finding_type(self):
        other = json.loads(json.dumps(GUARDDUTY))
        other["detail"]["id"] = "다른발견"
        other["detail"]["description"] = "Another instance was involved."
        assert normalize(GUARDDUTY)["fingerprint"] == normalize(other)["fingerprint"]


class TestGuardDuty:
    @pytest.mark.parametrize("score,expected", [
        (8.5, "critical"), (7, "critical"),
        (5, "error"), (4, "error"),
        (2, "warning"), (0.1, "warning"),
        (0, "info"),
    ])
    def test_numeric_severity_becomes_four_levels(self, score, expected):
        """GuardDuty 는 1~10 숫자를 보낸다. SEVERITY_MAP 은 문자열만 안다."""
        payload = json.loads(json.dumps(GUARDDUTY))
        payload["detail"]["severity"] = score
        assert normalize(payload)["severity"] == expected

    def test_instance_becomes_a_resource(self):
        assert normalize(GUARDDUTY)["meta"]["resource_id"] == "i-0abc123456789def0"


class TestHealth:
    def test_issue_is_an_error(self):
        assert normalize(HEALTH)["severity"] == "error"

    def test_scheduled_change_is_only_a_warning(self):
        """예정된 변경으로 사람을 깨우지 않는다."""
        payload = json.loads(json.dumps(HEALTH))
        payload["detail"]["eventTypeCategory"] = "scheduledChange"
        assert normalize(payload)["severity"] == "warning"

    def test_description_becomes_the_message(self):
        assert normalize(HEALTH)["message"] == "A instance store drive is degraded."


class TestEventBridge:
    def test_detail_type_groups_the_event(self):
        assert normalize(EVENTBRIDGE)["meta"]["alarmname"] == \
            "EC2 Instance State-change Notification"

    def test_resource_id_is_picked_from_detail(self):
        assert normalize(EVENTBRIDGE)["meta"]["resource_id"] == "i-0abc123456789def0"

    def test_narrow_adapters_win(self):
        """GuardDuty·Health 도 EventBridge 모양이다. 순서가 뒤집히면 전부 여기로 빨린다."""
        assert normalize(GUARDDUTY)["meta"]["adapter"] == "guardduty"
        assert normalize(HEALTH)["meta"]["adapter"] == "health"

    def test_adapter_order_puts_eventbridge_last(self):
        names = [name for name, _ in adapters.ADAPTERS]
        assert names[-1] == "eventbridge"


class TestUnknownSendersAreKeptNotDropped:
    """이 프로젝트에서 제일 피해온 실패 방식이 '조용히 사라지는 것' 이다."""

    UNKNOWN = {"weird_sender": "datadog-ish", "payload": {"a": 1}, "tstamp": 1755000000}

    def test_does_not_raise(self):
        assert normalize(self.UNKNOWN)["event_type"] == UNPARSED_TYPE

    def test_original_payload_is_kept(self):
        meta = normalize(self.UNKNOWN)["meta"]
        assert meta["unparsed"] is True
        assert "datadog-ish" in meta["raw_payload"]

    def test_severity_is_warning(self):
        """info 면 아무도 안 보고, error 면 파싱 실패로 사람을 깨운다."""
        assert normalize(self.UNKNOWN)["severity"] == "warning"

    def test_does_not_page_anyone(self):
        from api.normalize_handler import ALARM_SEVERITIES
        assert normalize(self.UNKNOWN)["severity"] not in ALARM_SEVERITIES

    def test_same_shape_groups_into_one_fingerprint(self):
        """한 발신자가 100건을 보내도 목록에 한 줄로 떠야 어댑터를 붙일 마음이 든다."""
        other = {"weird_sender": "other", "payload": {"a": 2}, "tstamp": 1755009999}
        assert normalize(self.UNKNOWN)["fingerprint"] == normalize(other)["fingerprint"]

    def test_different_shapes_do_not_group(self):
        other = {"completely": "different", "shape": True}
        assert normalize(self.UNKNOWN)["fingerprint"] != normalize(other)["fingerprint"]

    def test_shape_is_recorded_for_the_screen(self):
        shape = normalize(self.UNKNOWN)["meta"]["shape"]
        assert shape.startswith("keys:")
        assert "weird_sender" in shape

    def test_huge_payload_is_truncated_and_says_so(self):
        from api.normalize_handler import RAW_KEEP
        big = {"blob": "가" * (RAW_KEEP * 2)}
        meta = normalize(big)["meta"]
        assert len(meta["raw_payload"]) <= RAW_KEEP
        assert meta["raw_truncated"] is True

    def test_our_own_shape_is_still_rejected_when_blank(self):
        """아는 필드로 왔는데 내용이 비었으면 보관할 게 없다. 이건 거부한다.

        모르는 모양(내용은 다 있다)과 다르다. 보낸 쪽이 우리 형식을 쓰고
        있으니 고쳐 보내면 된다.
        """
        with pytest.raises(ValueError):
            normalize({"msg": "   "})


class TestNoModelInTheNormalizePath:
    """정규화는 결정적이어야 한다.

    같은 페이로드에서 매번 같은 지문이 나와야 억제도 집계도 런북 연결도
    성립한다. 모델이 매번 조금씩 다르게 파싱하면 그 전제가 무너진다.
    """

    def test_adapters_import_no_model_client(self):
        import ast
        import pathlib
        for name in ("adapters.py", "normalize_handler.py"):
            src = (pathlib.Path(__file__).parent.parent / "api" / name).read_text("utf-8")
            imported = set()
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            assert "anthropic" not in imported, f"{name} 이 모델을 부른다"

    def test_same_payload_gives_same_fingerprint(self):
        runs = {normalize(sns_envelope(CLOUDWATCH))["fingerprint"] for _ in range(5)}
        assert len(runs) == 1


class TestOneBadAdapterDoesNotBlockTheRest:
    def test_exploding_adapter_is_skipped(self, monkeypatch):
        def boom(raw):
            raise RuntimeError("터짐")

        monkeypatch.setattr(
            adapters, "ADAPTERS",
            (("boom", boom),) + adapters.ADAPTERS,
        )
        mapped, name = adapters.adapt(GUARDDUTY)
        assert name == "guardduty"


# ----------------------------------------------------------------------
# 한 바퀴: 진짜 페이로드 하나가 SLA 까지 가는가
# ----------------------------------------------------------------------
# 층마다 따로 테스트해도 사이가 끊겨 있을 수 있다. 실제로 지금까지 끊겨
# 있었다 - 정규화는 잘 돌았고 지문도 잘 나왔지만, 그 앞의 어댑터가 없어서
# 진짜 CloudWatch 페이로드는 1층에도 못 들어왔다.
#
# 여기서는 한 건을 끝까지 밀어 본다:
#   정규화 -> 지문 -> 적재 -> 런북 -> ack -> SLA

@pytest.mark.db
class TestOneAlarmAllTheWay:
    @pytest.fixture
    def chain(self, db_app, db_uri, monkeypatch):
        """이벤트 한 건과 런북 한 건을 넣었다가 지운다."""
        import psycopg

        from api.normalize_handler import store_event

        monkeypatch.setenv("DATABASE_URL", db_uri)

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("SELECT customer, account_id FROM aws_accounts LIMIT 1")
            row = cur.fetchone()
        if row is None:
            pytest.skip("등록된 고객사 계정이 없습니다")
        customer, account_id = row

        marker = "chain-check-알람-ZZQ"
        record = normalize(sns_envelope(
            dict(CLOUDWATCH, AlarmName=marker, AWSAccountId=account_id,
                 # 측정 창 안에 들어오게 방금 일어난 것으로 둔다.
                 StateChangeTime=None)))
        store_event(record)

        with db_app.app_context():
            from app.runbook import save
            save(title="한 바퀴 점검 절차", body="1. 확인\n2. 조치",
                 fingerprint=record["fingerprint"], customer="", author=marker)

        yield {"record": record, "customer": customer, "account_id": account_id,
               "marker": marker}

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE event_id = %s", (record["event_id"],))
            cur.execute("DELETE FROM runbooks WHERE author = %s", (marker,))

    def test_normalized_and_stored(self, db_app, chain):
        with db_app.app_context():
            from app import event_store
            stored = event_store.get(chain["record"]["event_id"])
            assert stored is not None
            assert stored["record"]["meta"]["adapter"] == "cloudwatch_alarm"

    def test_runbook_is_found_by_fingerprint(self, db_app, chain):
        """지문이 흔들리면 여기가 끊긴다. 절차를 써도 알람에 안 붙는다."""
        with db_app.app_context():
            from app.runbook import find
            assert find(chain["record"]["fingerprint"])["title"] == "한 바퀴 점검 절차"

    def test_ack_reaches_sla_as_a_fact_not_an_inference(self, db_app, chain):
        """SLA 가 이 알람을 '사람이 확인한 것' 으로 세는가.

        acked 와 inferred 를 나눠 세는 것이 핵심이다. 감사 로그에서 추론한
        것과 사람이 확인 버튼을 누른 것을 섞으면 지표가 실제보다 좋아 보인다.
        """
        with db_app.app_context():
            from app import event_store, sla

            event_store.acknowledge(chain["record"]["event_id"], "한바퀴")

            measured = sla.measure(chain["customer"], days=1)
            mine = [s for s in measured["by_severity"]
                    if s["severity"] == chain["record"]["severity"]][0]
            assert mine["total"] >= 1
            assert mine["acked"] >= 1


@pytest.mark.db
class TestUnparsedIsVisibleOnScreen:
    """못 읽은 발신자가 조용히 쌓이면 어댑터를 붙일 이유를 아무도 모른다."""

    @pytest.fixture
    def one_unparsed(self, db_uri, monkeypatch):
        import psycopg

        from api.normalize_handler import store_event

        monkeypatch.setenv("DATABASE_URL", db_uri)
        record = normalize({"zzq_unknown_sender": "x", "blob": {"a": 1}})
        store_event(record)
        yield record
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE event_id = %s", (record["event_id"],))

    def test_summary_groups_by_shape(self, db_app, one_unparsed):
        from app import event_store

        with db_app.app_context():
            summary = event_store.unparsed_summary()
        shapes = {s["shape"] for s in summary["shapes"]}
        assert one_unparsed["meta"]["shape"] in shapes
        assert summary["total"] >= 1

    def test_alarm_page_says_it_could_not_read_them(self, db_app, one_unparsed):
        db_app.config["WTF_CSRF_ENABLED"] = False
        client = db_app.test_client()
        client.post("/auth/login", data={"username": "admin", "password": "1234"})
        body = client.get("/alarm/").get_data(as_text=True)
        assert "정규화하지 못한 이벤트" in body
        assert "zzq_unknown_sender" in body
