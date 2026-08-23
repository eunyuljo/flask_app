# tests/test_alarm_advice.py
# 리소스 기준 알람 권고.
#
# 이 표가 하는 말은 하나다. "이런 알람을 걸어야 합니다."
# 걸려 있는지는 모니터링 서버에 있고 이 도구는 그 설정을 읽지 않는다.
#
# 지키는 것:
#   1. "없다" 고 말하지 않는다 (권고까지만)
#   2. 일반론이 아니라 이 리소스의 속성으로 갈린다
#   3. 추가 비용·설정이 드는 것을 숨기지 않는다
#   4. 알람 대상이 아닌 리소스도 이유를 밝힌다

import pytest

from app import alarm_advice
from app.compliance import Snapshot


def inst(rid="i-1", **attrs):
    base = {"instance_type": "m6i.large", "state": "running",
            "tags": {"Name": rid}}
    base.update(attrs)
    return {"resource_id": rid, "resource_type": "ec2:instance",
            "attributes": base}


def bucket(rid="b-1", **attrs):
    base = {"versioning": "Enabled", "encryption": "AES256"}
    base.update(attrs)
    return {"resource_id": rid, "resource_type": "s3:bucket", "attributes": base}


def snap(items, collected=None):
    return Snapshot(1, items, collected_types=collected)


def ids(row):
    return {r["id"] for r in row["rules"]}


def only(rows, resource_id):
    return next(r for r in rows if r["resource_id"] == resource_id)


class TestTailoring:
    """일반론이면 검색해서 나온다. 이 리소스의 속성으로 갈려야 값어치가 있다."""

    def test_burstable_gets_credit_alarm(self):
        rows = alarm_advice.advise(snap([inst("i-t3", instance_type="t3.small")]))
        assert "ec2-cpu-credit" in ids(only(rows, "i-t3"))

    def test_non_burstable_does_not(self):
        """m6i 에 크레딧 알람을 권고하면 그 목록 전체를 안 믿게 된다."""
        rows = alarm_advice.advise(snap([inst("i-m6", instance_type="m6i.large")]))
        assert "ec2-cpu-credit" not in ids(only(rows, "i-m6"))

    @pytest.mark.parametrize("family", ["t2", "t3", "t3a", "t4g"])
    def test_every_burstable_family(self, family):
        rows = alarm_advice.advise(
            snap([inst("i-1", instance_type=f"{family}.medium")]))
        assert "ec2-cpu-credit" in ids(only(rows, "i-1"))

    def test_public_ip_gets_network_alarm(self):
        rows = alarm_advice.advise(snap([
            inst("i-pub", public_ip="203.0.113.10"),
            inst("i-priv", public_ip=None),
        ]))
        assert "ec2-network-spike" in ids(only(rows, "i-pub"))
        assert "ec2-network-spike" not in ids(only(rows, "i-priv"))

    def test_iam_profile_gets_credential_alarm(self):
        rows = alarm_advice.advise(snap([
            inst("i-role", iam_profile="web-role"),
            inst("i-none", iam_profile=""),
        ]))
        assert "ec2-role-misuse" in ids(only(rows, "i-role"))
        assert "ec2-role-misuse" not in ids(only(rows, "i-none"))

    def test_unversioned_bucket_gets_delete_alarm(self):
        rows = alarm_advice.advise(snap([
            bucket("b-off", versioning="Disabled"),
            bucket("b-on", versioning="Enabled"),
        ]))
        assert "s3-delete-burst" in ids(only(rows, "b-off"))
        assert "s3-delete-burst" not in ids(only(rows, "b-on"))


class TestStoppedInstances:
    def test_stopped_gets_no_metric_alarms(self):
        rows = alarm_advice.advise(snap([inst("i-off", state="stopped")]))
        assert only(rows, "i-off")["rules"] == []

    def test_stopped_says_why(self):
        """목록에서 그냥 빠지면 빠뜨린 것인지 대상이 아닌 것인지 모른다."""
        rows = alarm_advice.advise(snap([inst("i-off", state="stopped")]))
        assert "stopped" in only(rows, "i-off")["note"]

    def test_unknown_state_is_treated_as_running(self):
        """꺼져 있다고 단정해 권고에서 빼면 진짜 필요한 알람이 사라진다."""
        item = {"resource_id": "i-?", "resource_type": "ec2:instance",
                "attributes": {"instance_type": "t3.small"}}
        assert only(alarm_advice.advise(snap([item])), "i-?")["rules"]


class TestNotAlarmed:
    def test_security_group_is_explained_not_omitted(self):
        sg = {"resource_id": "sg-1", "resource_type": "ec2:security_group",
              "attributes": {"name": "web", "ingress": []}}
        row = only(alarm_advice.advise(snap([sg])), "sg-1")
        assert row["rules"] == []
        assert "변경 추적" in row["note"]


class TestHonesty:
    def test_extra_cost_is_disclosed(self):
        """에이전트나 데이터 이벤트가 필요한 것을 숨기면
        '권고했는데 왜 안 걸려 있냐' 가 나중에 나온다."""
        needs = {r["id"]: r["needs"] for r in alarm_advice.RULES}
        assert "에이전트" in needs["ec2-disk-memory"]
        assert "CloudTrail" in needs["s3-delete-burst"]
        assert "요금" in needs["s3-delete-burst"]

    def test_every_rule_explains_itself(self):
        for rule in alarm_advice.RULES:
            assert rule["why"] and rule["metric"] and rule["hint"], rule["id"]
            assert rule["level"] in alarm_advice.LEVELS, rule["id"]

    def test_rules_only_target_collected_types(self):
        """수집하지 않는 종류에 규칙을 걸면 영원히 권고 0건이다."""
        from app.collect import COLLECTED_TYPES

        for rule in alarm_advice.RULES:
            assert rule["type"] in COLLECTED_TYPES, rule["id"]

    def test_module_never_claims_an_alarm_is_missing(self):
        """'없다' 는 말을 코드가 하지 않는지 본다.

        이 표는 걸려 있는지 확인하지 않는다. 그런데 화면 문구가 슬쩍
        '알람 없음' 으로 바뀌기 쉬운 자리라, 그 말이 들어오면 실패시킨다.
        """
        import inspect

        source = inspect.getsource(alarm_advice)
        for phrase in ("알람이 없습니다", "알람 없음", "미설정"):
            assert phrase not in source, phrase


class TestGrouping:
    def scene(self):
        return snap([
            inst("i-1", instance_type="t3.small", public_ip="1.2.3.4"),
            inst("i-2", instance_type="t3.small"),
            inst("i-3", instance_type="m6i.large"),
            bucket("b-1", versioning="Disabled"),
        ])

    def test_by_rule_counts_targets(self):
        rows = alarm_advice.advise(self.scene())
        groups = {g["id"]: g for g in alarm_advice.by_rule(rows)}
        assert len(groups["ec2-cpu-credit"]["targets"]) == 2     # t3 둘
        assert len(groups["ec2-status-check"]["targets"]) == 3   # 인스턴스 셋

    def test_essential_first(self):
        rows = alarm_advice.advise(self.scene())
        groups = alarm_advice.by_rule(rows)
        assert groups[0]["level"] == "essential"

    def test_empty_groups_are_dropped(self):
        rows = alarm_advice.advise(snap([bucket("b-1", versioning="Enabled")]))
        assert all(g["targets"] for g in alarm_advice.by_rule(rows))

    def test_summary(self):
        counts = alarm_advice.summarize(alarm_advice.advise(self.scene()))
        assert counts["resources"] == 4
        assert counts["total"] == sum(counts["by_level"].values())


@pytest.mark.db
class TestForCustomer:
    def test_unknown_customer(self, db_app, db_uri):
        with db_app.app_context():
            with pytest.raises(alarm_advice.AdviceError):
                alarm_advice.for_customer("없는고객사")

    def test_real_customer(self, db_app, db_uri):
        from app.customer import names

        with db_app.app_context():
            who = names()
            if not who:
                pytest.skip("등록된 고객사가 없습니다")
            data = alarm_advice.for_customer(who[0])

        assert data["snapshots"]
        assert set(data) >= {"rows", "groups", "counts", "missing_types"}
        for row in data["rows"]:
            assert row["account_id"] and row["region"]


@pytest.mark.db
class TestRoutes:
    def test_page(self, db_client):
        assert db_client.get("/resources/alarm-advice").status_code == 200

    def test_page_states_what_it_is_not(self, db_client):
        """이 화면은 자기 한계를 화면에 적어야 한다.

        예전에는 "실제로 걸려 있는지는 확인하지 않았습니다" 였다. 어댑터가
        CloudWatch 차원을 읽으면서 한쪽 방향은 확인할 수 있게 됐다 -
        알람이 왔으면 걸려 있는 것이다.

        반대 방향은 여전히 못 한다. 알람이 안 왔다고 설정이 없는 것은
        아니다(임계를 한 번도 안 넘었을 수 있다). 그 구분이 화면에 있어야 한다.
        """
        body = db_client.get("/resources/alarm-advice").get_data(as_text=True)
        assert "모름" in body
        assert "'없음' 이 아닙니다" in body

    def test_page_never_claims_an_alarm_is_missing(self, db_client):
        """'알람 없음' 이라고 단정하면 이 표가 거짓말을 하게 된다."""
        body = db_client.get("/resources/alarm-advice").get_data(as_text=True)
        assert "알람 없음" not in body
        assert "걸려 있지 않습니다" not in body

    def test_excel(self, db_client, db_app):
        from app.customer import names

        with db_app.app_context():
            who = names()
        if not who:
            pytest.skip("등록된 고객사가 없습니다")

        r = db_client.get(f"/resources/alarm-advice.xlsx?customer={who[0]}")
        assert r.status_code == 200
        assert r.data[:2] == b"PK"          # xlsx 는 zip 이다
        assert "filename*=UTF-8" in r.headers["Content-Disposition"]

    def test_excel_for_unknown_customer_does_not_500(self, db_client):
        r = db_client.get("/resources/alarm-advice.xlsx?customer=없는고객",
                          follow_redirects=True)
        assert r.status_code == 200


class TestExcel:
    def test_sheets_and_todo_column(self):
        """받는 쪽이 채울 '처리 여부' 칸이 있어야 표로 쓸모가 있다."""
        openpyxl = pytest.importorskip("openpyxl")
        pytest.importorskip("xlsxwriter")

        from io import BytesIO

        from app.alarm_advice_xlsx import build

        rows = alarm_advice.advise(snap([
            inst("i-1", instance_type="t3.small"),
            bucket("b-1", versioning="Disabled"),
        ]))
        for row in rows:
            row["account_id"], row["region"] = "111122223333", "ap-northeast-2"

        data = {"customer": "가고객", "rows": rows,
                "groups": alarm_advice.by_rule(rows),
                "counts": alarm_advice.summarize(rows),
                "snapshots": [], "missing_types": []}

        wb = openpyxl.load_workbook(BytesIO(build(data)))
        assert wb.sheetnames == ["요약", "알람별", "리소스별"]
        headers = [c.value for c in wb["알람별"][1]]
        assert "처리 여부" in headers
        assert "추가로 필요한 것" in headers

    def test_summary_sheet_disclaims(self):
        """받는 쪽이 '현황표' 로 읽으면 이미 걸어둔 알람을 지울 수 있다."""
        openpyxl = pytest.importorskip("openpyxl")
        pytest.importorskip("xlsxwriter")

        from io import BytesIO

        from app.alarm_advice_xlsx import build

        data = {"customer": "가고객", "rows": [], "groups": [],
                "counts": alarm_advice.summarize([]),
                "snapshots": [], "missing_types": []}
        wb = openpyxl.load_workbook(BytesIO(build(data)))
        text = " ".join(str(c.value) for row in wb["요약"].iter_rows()
                        for c in row if c.value)
        assert "확인하지 않았습니다" in text
