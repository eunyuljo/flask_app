# tests/test_graph.py
# 영향 범위.
#
# '의존 관계도' 라고 부르지 않는다. 수집기가 훑는 것은 세 종류뿐이라
# 전체 관계도를 그렸다고 하면 화면에 안 보이는 것을 없는 것으로 읽게 된다.
#
# 지키는 것:
#   1. 방향이 맞다 (고쳤을 때 흔들리는 쪽을 찾는다)
#   2. 실체 없는 점을 만들지 않는다
#   3. '간선 없음' 을 '영향 없음' 으로 말하지 않는다

import pytest

from app import graph
from app.compliance import Snapshot


def snap(items, collected=None):
    return Snapshot(1, items, collected_types=collected)


def inst(rid, sgs, name=None):
    return {"resource_id": rid, "resource_type": "ec2:instance",
            "attributes": {"security_groups": sgs,
                           "tags": {"Name": name or rid}}}


def sg(rid, name=None, allows=None, vpc="vpc-1"):
    return {"resource_id": rid, "resource_type": "ec2:security_group",
            "attributes": {"name": name or rid, "vpc": vpc,
                           "ingress": [], "ingress_groups": allows or []}}


def bucket(rid):
    return {"resource_id": rid, "resource_type": "s3:bucket",
            "attributes": {"versioning": "Enabled"}}


class TestBuild:
    def test_instance_uses_security_group(self):
        s = snap([inst("i-1", ["sg-web"]), sg("sg-web")])
        edges = graph.build(s)
        assert edges == [{"from": "i-1", "to": "sg-web", "kind": "uses_sg",
                          "label": graph.KINDS["uses_sg"]}]

    def test_security_group_referencing_another(self):
        """가장 흔한 의존 관계인데, 수집기가 이 값을 버리고 있었다."""
        s = snap([sg("sg-web"), sg("sg-db", allows=["sg-web"])])
        edges = graph.build(s)
        assert edges == [{"from": "sg-db", "to": "sg-web", "kind": "allows_sg",
                          "label": graph.KINDS["allows_sg"]}]

    def test_direction_is_who_depends_on_whom(self):
        """db-sg 가 web-sg 를 허용하면, web-sg 를 고쳤을 때 흔들리는 것은 db-sg 다."""
        s = snap([sg("sg-web"), sg("sg-db", allows=["sg-web"])])
        edge = graph.build(s)[0]
        assert edge["from"] == "sg-db"
        assert edge["to"] == "sg-web"

    def test_edge_to_a_missing_resource_is_dropped(self):
        """이름만 있고 실체가 없는 점이 화면에 생기면 안 된다."""
        s = snap([inst("i-1", ["sg-다른계정것"])])
        assert graph.build(s) == []

    def test_no_relationship_data_no_edges(self):
        """예전에 찍힌 스냅샷에는 ingress_groups 가 없다."""
        old = {"resource_id": "sg-1", "resource_type": "ec2:security_group",
               "attributes": {"name": "a", "vpc": "v", "ingress": []}}
        assert graph.build(snap([old])) == []


class TestImpact:
    def scene(self):
        # web-01, web-02 -> sg-web ; sg-db 는 sg-web 을 허용 ; db-01 -> sg-db
        return snap([
            inst("i-web1", ["sg-web"], "web-01"),
            inst("i-web2", ["sg-web"], "web-02"),
            inst("i-db1", ["sg-db"], "db-01"),
            sg("sg-web", "web-sg"),
            sg("sg-db", "db-sg", allows=["sg-web"]),
            bucket("assets"),
        ])

    def test_first_level(self):
        got = graph.impact(self.scene(), "sg-web", depth=1)
        names = {i["name"] for i in got["levels"][0]}
        assert names == {"web-01", "web-02", "db-sg"}

    def test_second_level_reaches_the_db_instance(self):
        """sg-web 을 고치면 db-sg 를 거쳐 db-01 까지 흔들린다.
        작업 승인 직전에 알아야 하는 것이 정확히 이것이다."""
        got = graph.impact(self.scene(), "sg-web", depth=2)
        second = {i["name"] for i in got["levels"][1]}
        assert "db-01" in second

    def test_depth_limits_the_walk(self):
        got = graph.impact(self.scene(), "sg-web", depth=1)
        assert len(got["levels"]) == 1

    def test_no_double_counting(self):
        """한 번 나온 리소스가 다음 단계에 또 나오면 건수가 부풀려진다."""
        got = graph.impact(self.scene(), "sg-web", depth=5)
        seen = [i["resource_id"] for level in got["levels"] for i in level]
        assert len(seen) == len(set(seen))

    def test_leaf_has_no_impact(self):
        got = graph.impact(self.scene(), "i-web1")
        assert got["found"] is True
        assert got["levels"] == []

    def test_unknown_resource(self):
        got = graph.impact(self.scene(), "없는것")
        assert got["found"] is False
        assert got["levels"] == []

    def test_names_come_from_tags_or_group_name(self):
        got = graph.impact(self.scene(), "sg-web", depth=1)
        assert got["target_name"] == "web-sg"
        assert {i["name"] for i in got["levels"][0]} >= {"web-01"}


class TestHonesty:
    def test_islands_are_reported(self):
        """S3 버킷은 관계를 수집하지 않아 언제나 섬이다.
        '연결이 없다' 가 아니라 '못 봤다' 이므로 화면에 남아야 한다."""
        s = snap([inst("i-1", ["sg-1"]), sg("sg-1"), bucket("assets")])
        got = {i["resource_id"] for i in graph.islands(s)}
        assert got == {"assets"}

    def test_islands_are_not_selectable(self):
        """고르면 언제나 '영향 없음' 이 나오는데, 그건 사실이 아니다."""
        s = snap([inst("i-1", ["sg-1"]), sg("sg-1"), bucket("assets")])
        assert "assets" not in {n["resource_id"] for n in graph.nodes(s)}

    def test_blind_list_is_not_empty(self):
        """무엇을 못 보는지 적어두지 않으면 짧은 목록이 '의존 없음' 으로 읽힌다."""
        assert len(graph.BLIND) >= 4
        assert any("로드밸런서" in b for b in graph.BLIND)
        assert any("RDS" in b for b in graph.BLIND)


class TestCollectorKeepsGroupRefs:
    def test_demo_baseline_has_a_group_reference(self):
        """데모 자료에 이 관계가 없으면 화면을 만들어 볼 수가 없다."""
        from app.collect import _baseline

        groups = [i for i in _baseline("123456789012")
                  if i["resource_type"] == "ec2:security_group"]
        assert any(i["attributes"].get("ingress_groups") for i in groups)

    def test_aws_collector_reads_user_id_group_pairs(self):
        """응답에 이미 들어 있는 값이라 API 를 더 부르지 않는다."""
        import inspect

        from app import collect

        source = inspect.getsource(collect.aws_resources)
        assert "UserIdGroupPairs" in source


@pytest.mark.db
class TestRoutes:
    def test_page(self, db_client):
        assert db_client.get("/resources/impact").status_code == 200

    def test_page_states_its_limits(self, db_client):
        body = db_client.get("/resources/impact").get_data(as_text=True)
        assert "볼 수 없는 것" in body
        assert "의존이 없다" in body
