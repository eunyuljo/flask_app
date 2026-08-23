# tests/test_alarm_link.py
# 알람과 리소스를 잇는다. 그리고 무엇을 말하지 않는가.
#
# ── 오래 못 했던 이유 ───────────────────────────────────────────────
# alarm_advice.py 머리말에 "들어온 이벤트에 리소스 id 가 없다" 고 적혀
# 있었다. 어댑터가 CloudWatch 차원을 읽으면서 그 제약이 사라졌다.
#
# ── 절반만 사라졌다 ─────────────────────────────────────────────────
# 알람이 왔다  -> 그 알람은 걸려 있다.        사실이다.
# 알람이 안 왔다 -> 아무 말도 못 한다.        설정이 없어서일 수도,
#                                             임계를 한 번도 안 넘어서일 수도.
#
# 이 파일이 지키는 것의 대부분은 두 번째다. '없음' 이라고 말하지 않는 것.
# SLA 에서 '계정을 들여다본 것' 과 '알람에 대응한 것' 을 구분하지 못했던
# 실수를 여기서 되풀이하지 않는다.

import pytest

from app import alarm_advice, alarm_link


def rule(rule_id="ec2-cpu"):
    return alarm_advice.RULES_BY_ID[rule_id]


def evidence(**metrics):
    return {"metrics": metrics, "count": sum(metrics.values()),
            "alarms": {}, "severities": {}, "worst": "warning", "last_at": None}


class TestJudge:
    def test_alarm_arrived_means_it_is_configured(self):
        state, hit = alarm_link.judge(rule("ec2-cpu"), evidence(CPUUtilization=4))
        assert state == "confirmed"
        assert hit == ["CPUUtilization"]

    def test_no_alarm_is_not_proof_of_absence(self):
        """이 한 줄이 이 모듈에서 제일 중요하다.

        알람이 안 왔다는 것은 설정이 없다는 뜻이 아니다. 임계를 한 번도
        안 넘었을 수도 있다.
        """
        state, hit = alarm_link.judge(rule("ec2-cpu"), evidence(NetworkIn=2))
        assert state == "unsure"
        assert hit == []

    def test_never_returns_a_state_that_means_missing(self):
        assert set(alarm_link.STATES) == {"confirmed", "unsure", "not_metric"}
        for label in alarm_link.STATES.values():
            assert "없" not in label

    def test_no_evidence_at_all_is_unsure_not_missing(self):
        assert alarm_link.judge(rule("ec2-cpu"), None)[0] == "unsure"

    def test_non_metric_rules_are_separated_from_unsure(self):
        """확인해서 근거가 없는 것과, 확인 자체를 못 하는 것은 다르다."""
        assert alarm_link.judge(rule("ec2-role-misuse"), None)[0] == "not_metric"
        assert alarm_link.judge(rule("s3-delete-burst"), evidence())[0] == "not_metric"

    def test_any_of_the_rule_metrics_confirms(self):
        state, hit = alarm_link.judge(
            rule("ec2-status-check"), evidence(StatusCheckFailed_System=1))
        assert state == "confirmed"
        assert hit == ["StatusCheckFailed_System"]

    def test_zero_count_does_not_confirm(self):
        assert alarm_link.judge(rule("ec2-cpu"), evidence(CPUUtilization=0))[0] == "unsure"


class TestEveryRuleDeclaresItsMetrics:
    """규칙에 metrics 를 안 적으면 조용히 '확인 불가' 로 빠진다.

    빠뜨린 것인지 일부러 비운 것인지 구분되지 않으므로, 지표 알람인
    규칙에는 반드시 있어야 한다.
    """

    @pytest.mark.parametrize("r", alarm_advice.RULES, ids=lambda r: r["id"])
    def test_metrics_key_exists(self, r):
        assert "metrics" in r
        assert isinstance(r["metrics"], tuple)

    @pytest.mark.parametrize("r", alarm_advice.RULES, ids=lambda r: r["id"])
    def test_empty_metrics_only_for_non_metric_rules(self, r):
        """비어 있다면 needs 나 metric 설명에 지표가 아니라는 근거가 있어야 한다."""
        if r["metrics"]:
            return
        blob = f"{r['metric']} {r['needs']}"
        assert "CloudTrail" in blob, f"{r['id']} 가 왜 지표 알람이 아닌지 안 적혀 있습니다"


class TestConfirm:
    def rows(self):
        return [{
            "resource_id": "i-1", "type": "ec2:instance", "name": "web",
            "note": "", "rules": [rule("ec2-cpu"), rule("ec2-cpu-credit")],
        }]

    def test_attaches_state_to_each_rule(self):
        got = alarm_link.confirm(self.rows(),
                                 {"i-1": evidence(CPUUtilization=3)})
        states = {r["id"]: r["state"] for r in got[0]["rules"]}
        assert states == {"ec2-cpu": "confirmed", "ec2-cpu-credit": "unsure"}

    def test_attaches_the_alarm_summary_to_the_resource(self):
        got = alarm_link.confirm(self.rows(), {"i-1": evidence(CPUUtilization=3)})
        assert got[0]["alarms"]["count"] == 3

    def test_resource_without_alarms_gets_none_not_zero(self):
        """0 을 넣으면 '알람이 0건 왔다' 로 읽힌다. 그건 못 봤다는 뜻이다."""
        got = alarm_link.confirm(self.rows(), {})
        assert got[0]["alarms"] is None

    def test_does_not_mutate_the_input(self):
        original = self.rows()
        alarm_link.confirm(original, {"i-1": evidence(CPUUtilization=1)})
        assert "state" not in original[0]["rules"][0]


class TestSummarize:
    def test_percentage_excludes_what_cannot_be_checked(self):
        """확인할 수 없는 규칙까지 분모에 넣으면 영원히 100%가 안 나온다."""
        rows = alarm_link.confirm([{
            "resource_id": "i-1", "type": "ec2:instance", "name": "", "note": "",
            "rules": [rule("ec2-cpu"), rule("ec2-role-misuse")],
        }], {"i-1": evidence(CPUUtilization=1)})
        got = alarm_link.summarize(rows)
        assert got["confirmed"] == 1
        assert got["not_metric"] == 1
        assert got["checkable"] == 1
        assert got["confirmed_pct"] == 100

    def test_empty(self):
        assert alarm_link.summarize([])["confirmed_pct"] == 0


@pytest.mark.db
class TestAgainstRealData:
    def test_seen_returns_resources(self, db_app, db_uri):
        with db_app.app_context():
            got = alarm_link.seen()
        if not got:
            pytest.skip("리소스가 붙은 이벤트가 없습니다 (seed-events 를 실행하세요)")
        one = next(iter(got.values()))
        assert one["count"] >= 1
        assert one["worst"] in alarm_link.SEVERITY_RANK
        assert one["last_at"] is not None

    def test_default_window_is_wide_enough_for_monthly_alarms(self):
        """7일로 잡으면 월 1회 도는 배치 알람이 통째로 '모름' 이 된다."""
        import inspect
        assert inspect.signature(alarm_link.seen).parameters["hours"].default >= 24 * 28

    def test_advice_reports_whether_it_checked(self, db_app, db_uri):
        from app.customer import names

        with db_app.app_context():
            who = names()
            if not who:
                pytest.skip("등록된 고객사가 없습니다")
            try:
                data = alarm_advice.for_customer(who[0])
            except alarm_advice.AdviceError as e:
                pytest.skip(str(e))

        # 대조했는지 안 했는지를 반드시 말해야 한다. 안 하고 조용히
        # 넘어가면 '모름' 이 '확인해봤는데 없더라' 로 읽힌다.
        assert "checked" in data
        if data["checked"]:
            assert data["link_counts"] is not None
            for row in data["rows"]:
                for r in row["rules"]:
                    assert r["state"] in alarm_link.STATES
        else:
            assert data["check_error"]

    def test_groups_carry_per_target_state(self, db_app, db_uri):
        from app.customer import names

        with db_app.app_context():
            who = names()
            if not who:
                pytest.skip("등록된 고객사가 없습니다")
            try:
                data = alarm_advice.for_customer(who[0])
            except alarm_advice.AdviceError as e:
                pytest.skip(str(e))
        if not data["checked"]:
            pytest.skip("대조하지 못했습니다")
        for group in data["groups"]:
            assert group["confirmed"] + group["unsure"] <= len(group["targets"])
            for target in group["targets"]:
                assert target["state"] in alarm_link.STATES


@pytest.mark.db
class TestInventoryShowsAlarms:
    def test_column_appears(self, db_client):
        body = db_client.get("/resources/inventory").get_data(as_text=True)
        assert "최근 알람" in body

    def test_blank_instead_of_zero(self, db_client):
        """알람이 안 온 리소스에 0 을 찍으면 '확인해봤더니 0건' 으로 읽힌다.

        사실은 못 본 것이다. 이 프로젝트에서 그 둘을 섞은 적이 몇 번 있었다.
        """
        import re

        body = db_client.get("/resources/inventory").get_data(as_text=True)
        assert not re.search(r"(?<!\d)0건", body)

    def test_alarm_names_are_capped(self, db_client):
        """한 리소스에 알람 이름이 열 개씩 붙으면 표가 읽히지 않는다."""
        body = db_client.get("/resources/inventory").get_data(as_text=True)
        assert "외 " in body or "prod-" in body
