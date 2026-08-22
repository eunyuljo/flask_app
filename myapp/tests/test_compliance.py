# tests/test_compliance.py
# 컴플라이언스 점검.
#
# 점검 함수는 DB 를 보지 않는 순수 함수라 스냅샷을 손으로 만들어 먹인다.
# 이게 규칙을 표가 아니라 코드에 둔 이유이기도 하다 - 조건식을 문자열로
# 저장했다면 이 테스트를 쓸 수 없다.

from datetime import datetime, timedelta, timezone

import pytest

from app import compliance as C


def snap(*items):
    """리소스 목록으로 스냅샷 하나를 만든다."""
    return C.Snapshot(1, list(items))


def sg(resource_id, *ingress):
    return {"resource_id": resource_id, "resource_type": "ec2:security_group",
            "attributes": {"ingress": list(ingress)}}


def instance(resource_id, groups, state="running", tags=None):
    return {"resource_id": resource_id, "resource_type": "ec2:instance",
            "attributes": {"state": state, "security_groups": list(groups),
                           "tags": tags if tags is not None else {"Name": "x", "Env": "prod"}}}


def bucket(resource_id, **attrs):
    base = {"public_access_blocked": True, "versioning": "Enabled", "encryption": "AES256"}
    base.update(attrs)
    return {"resource_id": resource_id, "resource_type": "s3:bucket", "attributes": base}


def ids(violations, check_id):
    return sorted(v["resource_id"] for v in violations if v["check_id"] == check_id)


class TestIngressParsing:
    @pytest.mark.parametrize("rule, expected", [
        ("22/tcp:0.0.0.0/0", (22, "tcp", "0.0.0.0/0")),
        ("443/TCP:10.0.0.0/8", (443, "tcp", "10.0.0.0/8")),
        ("*/all:0.0.0.0/0", (None, "all", "0.0.0.0/0")),
        ("8080/tcp:sg-web", (8080, "tcp", "sg-web")),
    ])
    def test_parses(self, rule, expected):
        assert C._parse_ingress(rule) == expected

    @pytest.mark.parametrize("rule", ["", "이상한값", "22:tcp:0.0.0.0/0", None])
    def test_unparsable_returns_none(self, rule):
        assert C._parse_ingress(rule)[0] is None

    def test_unparsable_rule_is_not_reported(self):
        """읽지 못한 규칙을 위반으로 올리면 목록이 거짓으로 찬다."""
        v = C.run_checks(snap(sg("sg-1", "형식을 모르는 값")))
        assert ids(v, "sg-admin-port-open") == []


class TestSecurityGroup:
    def test_ssh_open_to_world(self):
        v = C.run_checks(snap(sg("sg-1", "22/tcp:0.0.0.0/0")))
        assert ids(v, "sg-admin-port-open") == ["sg-1"]

    def test_ssh_open_to_internal_is_fine(self):
        v = C.run_checks(snap(sg("sg-1", "22/tcp:10.0.0.0/8")))
        assert ids(v, "sg-admin-port-open") == []

    def test_ipv6_any_also_counts(self):
        """0.0.0.0/0 만 보고 ::/0 을 놓치면 반쪽만 막은 것이다."""
        v = C.run_checks(snap(sg("sg-1", "3389/tcp:::/0")))
        assert ids(v, "sg-admin-port-open") == ["sg-1"]

    def test_https_open_to_world_is_not_a_violation(self):
        """웹은 열려 있는 게 정상이다. 여기서 걸리면 아무도 목록을 안 본다."""
        v = C.run_checks(snap(sg("sg-1", "443/tcp:0.0.0.0/0", "80/tcp:0.0.0.0/0")))
        assert v == []

    def test_db_port_open(self):
        v = C.run_checks(snap(sg("sg-1", "3306/tcp:0.0.0.0/0")))
        assert ids(v, "sg-db-port-open") == ["sg-1"]

    def test_all_ports_open(self):
        v = C.run_checks(snap(sg("sg-1", "*/-1:0.0.0.0/0")))
        assert ids(v, "sg-all-ports-open") == ["sg-1"]

    def test_severity_is_critical(self):
        v = C.run_checks(snap(sg("sg-1", "22/tcp:0.0.0.0/0")))
        assert v[0]["severity"] == "critical"


class TestCrossResource:
    """리소스 하나만 봐서는 답할 수 없는 점검.

    Config 의 규칙 하나로는 다루기 어려운 부분이다. 스냅샷은 통째로
    들고 있으니 인스턴스에서 보안그룹을 찾아보면 된다.
    """

    def test_instance_using_open_sg_is_flagged(self):
        v = C.run_checks(snap(
            sg("sg-open", "22/tcp:0.0.0.0/0"),
            instance("i-1", ["sg-open"]),
        ))
        assert ids(v, "ec2-exposed-admin-port") == ["i-1"]

    def test_open_sg_with_no_instance_is_only_the_sg(self):
        """붙은 인스턴스가 없으면 치우면 되는 문제고, 붙어 있으면 뚫린 것이다."""
        v = C.run_checks(snap(sg("sg-open", "22/tcp:0.0.0.0/0")))
        assert ids(v, "sg-admin-port-open") == ["sg-open"]
        assert ids(v, "ec2-exposed-admin-port") == []

    def test_stopped_instance_is_not_exposed(self):
        v = C.run_checks(snap(
            sg("sg-open", "22/tcp:0.0.0.0/0"),
            instance("i-1", ["sg-open"], state="stopped"),
        ))
        assert ids(v, "ec2-exposed-admin-port") == []

    def test_instance_on_a_safe_sg_is_fine(self):
        v = C.run_checks(snap(
            sg("sg-open", "22/tcp:0.0.0.0/0"),
            sg("sg-safe", "443/tcp:0.0.0.0/0"),
            instance("i-1", ["sg-safe"]),
        ))
        assert ids(v, "ec2-exposed-admin-port") == []


class TestS3:
    def test_clean_bucket_passes(self):
        assert C.run_checks(snap(bucket("b"))) == []

    def test_public_access_not_blocked(self):
        v = C.run_checks(snap(bucket("b", public_access_blocked=False)))
        assert ids(v, "s3-public-access") == ["b"]

    def test_missing_field_counts_as_not_blocked(self):
        """수집하지 못한 것을 '안전함' 으로 보면 안 된다."""
        item = bucket("b")
        del item["attributes"]["public_access_blocked"]
        v = C.run_checks(snap(item))
        assert ids(v, "s3-public-access") == ["b"]

    @pytest.mark.parametrize("value", ["None", "none", "Unknown", "", None])
    def test_encryption_missing(self, value):
        v = C.run_checks(snap(bucket("b", encryption=value)))
        assert ids(v, "s3-encryption") == ["b"]

    def test_versioning_off(self):
        v = C.run_checks(snap(bucket("b", versioning="Disabled")))
        assert ids(v, "s3-versioning") == ["b"]


class TestTags:
    def test_missing_required_tag(self):
        v = C.run_checks(snap(instance("i-1", [], tags={"Name": "x"})))
        assert ids(v, "missing-required-tags") == ["i-1"]

    def test_empty_tag_value_counts_as_missing(self):
        v = C.run_checks(snap(instance("i-1", [], tags={"Name": "x", "Env": ""})))
        assert ids(v, "missing-required-tags") == ["i-1"]

    def test_resource_without_tag_support_is_skipped(self):
        """태그를 붙일 수 없는 리소스까지 걸면 목록이 쓸모없어진다."""
        v = C.run_checks(snap(bucket("b")))
        assert ids(v, "missing-required-tags") == []


class TestRdsAndIam:
    def test_public_rds(self):
        v = C.run_checks(snap({"resource_id": "db", "resource_type": "rds:instance",
                               "attributes": {"public": True, "multi_az": True}}))
        assert ids(v, "rds-public") == ["db"]

    def test_single_az(self):
        v = C.run_checks(snap({"resource_id": "db", "resource_type": "rds:instance",
                               "attributes": {"public": False, "multi_az": False}}))
        assert ids(v, "rds-single-az") == ["db"]

    @pytest.mark.parametrize("policy", [
        "AdministratorAccess", "AmazonS3FullAccess", "PowerUserAccess",
    ])
    def test_broad_policy(self, policy):
        v = C.run_checks(snap({"resource_id": "r", "resource_type": "iam:role",
                               "attributes": {"policies": [policy]}}))
        assert ids(v, "iam-broad-policy") == ["r"]

    def test_narrow_policy_is_fine(self):
        v = C.run_checks(snap({"resource_id": "r", "resource_type": "iam:role",
                               "attributes": {"policies": ["AmazonS3ReadOnlyAccess"]}}))
        assert ids(v, "iam-broad-policy") == []


class TestOrderingAndSummary:
    def test_critical_comes_first(self):
        v = C.run_checks(snap(
            bucket("b", versioning="Disabled"),          # medium
            sg("sg-1", "22/tcp:0.0.0.0/0"),              # critical
        ))
        assert v[0]["severity"] == "critical"

    def test_severity_order_is_not_alphabetical(self):
        """문자열로 정렬하면 critical < high < low < medium 이 된다."""
        assert C.SEVERITY_ORDER["high"] < C.SEVERITY_ORDER["medium"]
        assert C.SEVERITY_ORDER["medium"] < C.SEVERITY_ORDER["low"]

    def test_summary_counts(self):
        v = C.run_checks(snap(sg("sg-1", "22/tcp:0.0.0.0/0", "3306/tcp:0.0.0.0/0")))
        s = C.summarize(v)
        assert s["total"] == 2
        assert s["by_severity"]["critical"] == 2
        assert s["worst"] == "critical"

    def test_worst_is_none_when_clean(self):
        assert C.summarize([])["worst"] is None


class TestExcused:
    def test_excused_stays_in_the_list(self):
        """목록에서 지워버리면 예외가 몇 개 쌓였는지 아무도 모르게 된다."""
        v = C.run_checks(snap(sg("sg-1", "22/tcp:0.0.0.0/0")),
                         excused={("sg-admin-port-open", "sg-1")})
        assert len(v) == 1
        assert v[0]["excused"] is True

    def test_excused_is_not_counted(self):
        v = C.run_checks(snap(sg("sg-1", "22/tcp:0.0.0.0/0")),
                         excused={("sg-admin-port-open", "sg-1")})
        s = C.summarize(v)
        assert s["total"] == 0
        assert s["excused"] == 1

    def test_account_wide_exception(self):
        v = C.run_checks(snap(sg("sg-1", "22/tcp:0.0.0.0/0"), sg("sg-2", "22/tcp:0.0.0.0/0")),
                         excused={("sg-admin-port-open", "")})
        assert all(x["excused"] for x in v if x["check_id"] == "sg-admin-port-open")

    def test_exception_does_not_leak_to_other_checks(self):
        """보안그룹 규칙을 예외로 뺐다고 그 그룹을 쓰는 인스턴스까지
        빠지면 안 된다. 노출은 그대로다."""
        v = C.run_checks(
            snap(sg("sg-open", "22/tcp:0.0.0.0/0"), instance("i-1", ["sg-open"])),
            excused={("sg-admin-port-open", "sg-open")},
        )
        exposed = [x for x in v if x["check_id"] == "ec2-exposed-admin-port"]
        assert exposed and exposed[0]["excused"] is False


class TestTimelineMath:
    """시간축 계산. DB 없이 points 를 손으로 만들어 확인한다."""

    def _points(self, *keysets):
        points = []
        for i, keys in enumerate(keysets):
            points.append({"keys": set(keys), "collected_at": f"t{i}"})
        for i, p in enumerate(points):
            before = points[i - 1]["keys"] if i else set()
            p["opened"] = sorted(p["keys"] - before)
            p["closed"] = sorted(before - p["keys"])
            p["first"] = (i == 0)
        return points

    def test_recurring_needs_two_openings(self):
        k = ("sg-admin-port-open", "sg-1")
        # 열림 -> 닫힘 -> 다시 열림
        points = self._points([], [k], [], [k])
        assert C.recurring(points)[0]["times"] == 2

    def test_opened_once_is_not_recurring(self):
        k = ("sg-admin-port-open", "sg-1")
        assert C.recurring(self._points([], [k], [k])) == []

    def test_first_snapshot_does_not_count_as_an_opening(self):
        """첫 스냅샷은 비교 대상이 없어서 전부 '새로 생김' 으로 나온다.
        여기서 세면 한 번만 열린 것이 재발로 올라온다."""
        k = ("sg-admin-port-open", "sg-1")
        assert C.recurring(self._points([k], [], [k])) == []

    def test_first_seen_returns_the_earliest(self):
        k = ("sg-admin-port-open", "sg-1")
        points = self._points([k], [k], [k])
        assert C.first_seen(points)[k] == "t0"

    def test_first_seen_skips_already_fixed(self):
        """지금 위반이 아닌 것은 '언제부터' 를 물을 이유가 없다."""
        k = ("sg-admin-port-open", "sg-1")
        assert C.first_seen(self._points([k], [])) == {}

    def test_first_seen_of_empty_history(self):
        assert C.first_seen([]) == {}

    def test_first_seen_covers_excused_items(self):
        """예외로 덮어둔 항목도 '언제부터' 를 알아야 한다.

        예외를 연장할지 판단하려면 그게 언제부터 그랬는지가 필요하다.
        keys 는 예외를 뺀 집합이라 all_keys 를 따로 본다.
        """
        k = ("sg-admin-port-open", "sg-1")
        points = [
            {"keys": set(), "all_keys": {k}, "collected_at": "t0"},
            {"keys": set(), "all_keys": {k}, "collected_at": "t1"},
        ]
        assert C.first_seen(points)[k] == "t0"


class TestChecksTable:
    def test_ids_are_unique(self):
        seen = [c["id"] for c in C.CHECKS]
        assert len(seen) == len(set(seen))

    def test_every_check_has_a_standard_and_reason(self):
        """'왜 위반인가' 를 못 대면 고객사에 설명할 수 없다."""
        for c in C.CHECKS:
            assert c["standard"], c["id"]
            assert c["why"], c["id"]
            assert c["severity"] in C.SEVERITIES, c["id"]


@pytest.mark.db
class TestExceptionsTable:
    @pytest.fixture
    def clean(self, db_app, db_uri):
        import psycopg

        def purge():
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM compliance_exceptions WHERE account_id LIKE 'test-%'")

        purge()
        yield
        purge()

    def _later(self, days=30):
        return datetime.now(timezone.utc) + timedelta(days=days)

    def test_reason_is_required(self, db_app, clean):
        with db_app.app_context():
            with pytest.raises(C.ComplianceError):
                C.add_exception("test-a", "s3-versioning", "b", "   ", self._later())

    def test_expiry_is_required(self, db_app, clean):
        with db_app.app_context():
            with pytest.raises(C.ComplianceError):
                C.add_exception("test-a", "s3-versioning", "b", "사유", None)

    def test_unknown_check_is_rejected(self, db_app, clean):
        with db_app.app_context():
            with pytest.raises(C.ComplianceError):
                C.add_exception("test-a", "없는항목", "b", "사유", self._later())

    def test_round_trip(self, db_app, clean):
        with db_app.app_context():
            C.add_exception("test-a", "s3-versioning", "b", "고객사 승인", self._later())
            live = [e for e in C.exceptions("test-a")]
            assert len(live) == 1
            assert live[0]["reason"] == "고객사 승인"

    def test_expired_exception_has_no_effect(self, db_app, clean):
        """만료되면 위반이 자동으로 다시 떠야 한다."""
        with db_app.app_context():
            C.add_exception("test-a", "s3-versioning", "b", "지난 예외", self._later(-1))
            assert C.exceptions("test-a") == []
            # 이력에는 남는다.
            assert len(C.all_exceptions("test-a")) == 1
            assert C.all_exceptions("test-a")[0]["expired"] is True

    def test_re_registering_updates_in_place(self, db_app, clean):
        with db_app.app_context():
            C.add_exception("test-a", "s3-versioning", "b", "처음", self._later())
            C.add_exception("test-a", "s3-versioning", "b", "고친 사유", self._later())
            live = C.exceptions("test-a")
            assert len(live) == 1
            assert live[0]["reason"] == "고친 사유"

    def test_delete(self, db_app, clean):
        with db_app.app_context():
            eid = C.add_exception("test-a", "s3-versioning", "b", "사유", self._later())
            C.drop_exception(eid)
            assert C.exceptions("test-a") == []
            with pytest.raises(C.ComplianceError):
                C.drop_exception(eid)


@pytest.mark.db
class TestAgainstRealSnapshots:
    def test_evaluate_runs_on_stored_snapshots(self, db_app, db_uri):
        """실제로 쌓여 있는 스냅샷에서 터지지 않는지."""
        with db_app.app_context():
            targets = C.latest_snapshots()
            if not targets:
                pytest.skip("스냅샷이 없습니다 (collect-resources 필요)")
            for t in targets:
                violations, snap = C.evaluate(t["snapshot_id"], t["account_id"])
                assert isinstance(violations, list)
                summary = C.summarize(violations)
                assert summary["total"] <= len(violations)

    def test_timeline_is_ordered_oldest_first(self, db_app, db_uri):
        with db_app.app_context():
            targets = C.latest_snapshots()
            if not targets:
                pytest.skip("스냅샷이 없습니다")
            t = targets[0]
            points = C.timeline(t["account_id"], t["region"])
            times = [p["collected_at"] for p in points]
            assert times == sorted(times)
            if points:
                assert points[0]["first"] is True
