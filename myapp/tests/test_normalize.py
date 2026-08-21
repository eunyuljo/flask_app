# tests/test_normalize.py
# Lambda 정규화와 지문(fingerprint) 테스트.
#
# 지문은 이 프로젝트에서 실제로 버그가 났던 자리다. 메시지 전문을 해시해서
# 같은 알람이 매번 다른 지문이 됐고, 그 위에 얹힌 억제·집계·런북이
# 전부 무의미했다. 그래서 여기를 제일 두껍게 덮는다.

import pytest

from api.normalize_handler import normalize, message_template


class TestFieldAliases:
    """보내는 쪽마다 다른 필드 이름을 표준 이름으로 모은다."""

    @pytest.mark.parametrize("alias", ["msg", "message", "text", "description"])
    def test_message_aliases(self, alias):
        assert normalize({alias: "디스크 부족"})["message"] == "디스크 부족"

    @pytest.mark.parametrize("alias", ["level", "severity", "priority"])
    def test_severity_aliases(self, alias):
        assert normalize({"msg": "x", alias: "FATAL"})["severity"] == "critical"

    @pytest.mark.parametrize("alias", ["source", "service", "origin", "from"])
    def test_source_aliases(self, alias):
        assert normalize({"msg": "x", alias: "pay-api"})["source"] == "pay-api"

    def test_unknown_fields_go_to_meta(self):
        record = normalize({"msg": "x", "host": "i-123", "InstanceId": "i-456"})
        assert record["meta"]["host"] == "i-123"
        assert record["meta"]["instanceid"] == "i-456"

    def test_empty_message_rejected(self):
        with pytest.raises(ValueError):
            normalize({"msg": "   "})

    def test_non_dict_rejected(self):
        with pytest.raises(ValueError):
            normalize("문자열")


class TestSeverity:
    @pytest.mark.parametrize("raw,expected", [
        ("FATAL", "critical"), ("crit", "critical"), ("p1", "critical"),
        ("error", "error"), ("high", "error"), ("p2", "error"),
        ("warn", "warning"), ("medium", "warning"),
        ("info", "info"), ("debug", "info"),
    ])
    def test_mapping(self, raw, expected):
        assert normalize({"msg": "x", "level": raw})["severity"] == expected

    def test_unknown_falls_back_to_info(self):
        assert normalize({"msg": "x", "level": "이상한값"})["severity"] == "info"


class TestMessageTemplate:
    """지문을 만들기 전에 '매번 달라지는 값' 을 지운다."""

    @pytest.mark.parametrize("message,expected", [
        ("CPU usage 92.4% on i-0abc123456789def0", "cpu usage <n>% on <id>"),
        ("Connection refused from 10.0.3.17:5432", "connection refused from <ip>:<n>"),
        ("timed out after 30000ms", "timed out after <n>ms"),
        ("Request 550e8400-e29b-41d4-a716-446655440000 failed", "request <uuid> failed"),
    ])
    def test_masking(self, message, expected):
        assert message_template(message) == expected

    @pytest.mark.parametrize("message", [
        "ec2 instance unreachable",     # ec2 의 2 는 지우면 안 된다
        "s3 bucket policy changed",     # s3 의 3 도 마찬가지
        "/dev/xvda1 mount failed",      # xvda1 의 1 도
    ])
    def test_does_not_eat_identifiers(self, message):
        # 앞에 단어 경계가 없는 숫자는 남아야 한다.
        assert "<n>" not in message_template(message)

    def test_unit_suffixed_numbers_are_masked(self):
        """숫자 뒤에 \\b 를 두면 '30000ms' 를 못 지운다(0 과 m 사이에 경계가 없다)."""
        a = message_template("timed out after 30000ms")
        b = message_template("timed out after 45000ms")
        assert a == b

    def test_resource_id_of_unusual_length(self):
        """길이가 표준(8/17)과 달라도 <id> 로 잡혀야 한다.

        <hex> 로 떨어지면 같은 알람이 다른 틀이 되어 묶임이 깨진다.
        """
        short = message_template("volume vol-0abc1234 is full")
        long_ = message_template("volume vol-0fedcba09876543210 is full")
        assert short == long_ == "volume <id> is full"


class TestFingerprint:
    def _fp(self, message, **kw):
        payload = {"msg": message, "service": "pay-api", "type": "http"}
        payload.update(kw)
        return normalize(payload)["fingerprint"]

    def test_same_alarm_different_values_group(self):
        """측정값만 다른 같은 알람은 같은 지문이어야 한다. 이게 핵심이다."""
        fps = {self._fp(f"CPU usage {v}% on i-0abc123456789def0")
               for v in ("92.4", "87.1", "71")}
        assert len(fps) == 1

    def test_different_alarms_do_not_group(self):
        assert self._fp("CPU usage 92%") != self._fp("Memory usage 92%")

    def test_source_separates(self):
        assert (self._fp("CPU 92%", service="web")
                != self._fp("CPU 92%", service="db"))

    def test_event_type_separates(self):
        assert (self._fp("CPU 92%", type="metric")
                != self._fp("CPU 92%", type="alert"))

    @pytest.mark.parametrize("key", ["AlarmName", "alarm_name", "alertname"])
    def test_alarm_name_wins_over_message(self, key):
        """meta 에 알람 이름이 있으면 메시지가 달라도 같은 지문."""
        a = self._fp("CPU 92%", **{key: "prod-web-cpu-high"})
        b = self._fp("완전히 다른 문장", **{key: "prod-web-cpu-high"})
        assert a == b

    def test_different_alarm_names_separate(self):
        a = self._fp("CPU 92%", AlarmName="prod-web-cpu")
        b = self._fp("CPU 92%", AlarmName="prod-db-cpu")
        assert a != b

    def test_is_stable_across_calls(self):
        assert self._fp("CPU 92%") == self._fp("CPU 92%")
