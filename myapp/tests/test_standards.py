# tests/test_standards.py
# 고객사별 구성 표준.
#
# 컴플라이언스가 '누구에게나 통하는 모범사례' 라면 여기는 '이 고객사와
# 합의한 것' 이다. 실제로 compliance.REQUIRED_TAGS 가 코드에 박혀 있어서
# 고객사가 둘째부터 맞지 않았다.
#
# 지키는 것:
#   1. 평가할 수 없는 규칙은 만들 수 없다
#   2. 수집하지 않은 것을 '위반 없음' 으로 세지 않는다
#   3. 고객사 표준이 컴플라이언스 기본값을 이긴다
#   4. 못 읽는 값은 저장 단계에서 막는다

import pytest

from app import standards
from app.standards import StandardError


class FakeSnap:
    """compliance.Snapshot 의 필요한 부분만."""

    def __init__(self, items, collected=None):
        self.items = items
        self.collected = set(collected or {i["resource_type"] for i in items})
        self.meta = {}

    def of_type(self, resource_type):
        return [i for i in self.items if i["resource_type"] == resource_type]


def instance(rid, **attrs):
    base = {"instance_type": "t3.micro", "tags": {"Name": "web-01", "Env": "prod"}}
    base.update(attrs)
    return {"resource_id": rid, "resource_type": "ec2:instance", "attributes": base}


def run(rule, value, items, collected=None):
    spec = standards.RULES[rule]
    parsed = spec["parse"](value)
    return list(spec["check"](parsed, FakeSnap(items, collected)))


class TestRequiredTags:
    def test_missing_tag_is_found(self):
        got = run("required_tags", "Name, Env, Owner", [instance("i-1")])
        assert len(got) == 1
        assert "Owner" in got[0][1]

    def test_all_present_is_clean(self):
        got = run("required_tags", "Name, Env",
                  [instance("i-1", tags={"Name": "a", "Env": "prod"})])
        assert got == []

    def test_empty_value_is_rejected(self):
        with pytest.raises(StandardError):
            standards.RULES["required_tags"]["parse"]("  ,  ")

    def test_resource_without_tags_is_not_a_violation(self):
        """수집기가 태그를 안 담는 종류가 있다. 안 본 것을 위반으로 만들면 안 된다."""
        sg = {"resource_id": "sg-1", "resource_type": "ec2:instance",
              "attributes": {"instance_type": "t3.micro"}}
        assert run("required_tags", "Name", [sg]) == []


class TestInstanceTypes:
    def test_disallowed_type(self):
        got = run("instance_types", "t3.micro, t3.small",
                  [instance("i-1", instance_type="m6i.4xlarge")])
        assert len(got) == 1
        assert "m6i.4xlarge" in got[0][1]

    def test_allowed_type(self):
        assert run("instance_types", "t3.micro", [instance("i-1")]) == []


class TestTagValues:
    def test_format_is_enforced(self):
        with pytest.raises(StandardError) as e:
            standards.RULES["tag_values"]["parse"]("Env prod")
        assert "형식으로 적으세요" in str(e.value)

    def test_wrong_value(self):
        got = run("tag_values", "Env=prod|stg",
                  [instance("i-1", tags={"Name": "a", "Env": "production"})])
        assert len(got) == 1
        assert "production" in got[0][1]

    def test_right_value(self):
        assert run("tag_values", "Env=prod|stg",
                   [instance("i-1", tags={"Env": "prod"})]) == []

    def test_missing_tag_is_left_to_required_tags(self):
        """같은 리소스가 두 규칙에서 두 번 나오면 위반 수가 부풀려진다."""
        assert run("tag_values", "Env=prod", [instance("i-1", tags={"Name": "a"})]) == []


class TestNamePrefix:
    def test_wrong_prefix(self):
        got = run("name_prefix", "wcorp-", [instance("i-1", tags={"Name": "web-01"})])
        assert len(got) == 1

    def test_right_prefix(self):
        assert run("name_prefix", "web-", [instance("i-1", tags={"Name": "web-01"})]) == []

    def test_empty_prefix_is_rejected(self):
        with pytest.raises(StandardError):
            standards.RULES["name_prefix"]["parse"]("   ")


class TestRuleRegistry:
    def test_every_rule_declares_what_it_needs(self):
        """requires 가 없으면 수집하지 않은 것을 '위반 없음' 으로 세게 된다."""
        for name, spec in standards.RULES.items():
            assert spec["requires"], name
            assert spec["label"] and spec["hint"] and spec["why"], name

    def test_every_rule_needs_only_collected_types(self):
        """수집기가 담지 않는 종류를 요구하면 그 규칙은 영원히 '보지 못함' 이다."""
        from app.collect import COLLECTED_TYPES

        for name, spec in standards.RULES.items():
            assert set(spec["requires"]) <= set(COLLECTED_TYPES), name


@pytest.mark.db
class TestStore:
    @pytest.fixture
    def clean(self, db_uri):
        yield
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM customer_standards WHERE customer = %s",
                        ("시험표준",))

    def test_bad_value_is_rejected_before_saving(self, db_app, clean):
        """못 읽는 값을 넣어두면 화면을 열 때마다 그 고객사가 깨진다."""
        with db_app.app_context():
            with pytest.raises(StandardError):
                standards.save("시험표준", "tag_values", "형식이아님")
            assert standards.listing("시험표준") == []

    def test_unknown_rule(self, db_app, clean):
        with db_app.app_context():
            with pytest.raises(StandardError) as e:
                standards.save("시험표준", "백업필수", "yes")
        assert "알 수 없는 규칙" in str(e.value)

    def test_save_twice_updates(self, db_app, clean):
        with db_app.app_context():
            standards.save("시험표준", "required_tags", "Name")
            standards.save("시험표준", "required_tags", "Name, Env")
            items = standards.listing("시험표준")
        assert len(items) == 1
        assert items[0]["value"] == "Name, Env"

    def test_required_tags_for(self, db_app, clean):
        with db_app.app_context():
            assert standards.required_tags_for("시험표준") is None
            standards.save("시험표준", "required_tags", "Name, Owner")
            assert standards.required_tags_for("시험표준") == ["Name", "Owner"]

    def test_none_not_empty_list(self, db_app, clean):
        """빈 목록을 주면 '태그를 하나도 요구하지 않는다' 가 되어
        규칙을 안 정한 고객사에서 기본 점검까지 꺼진다."""
        with db_app.app_context():
            assert standards.required_tags_for("규칙없는고객") is None


@pytest.mark.db
class TestOverridesCompliance:
    """고객사 표준이 컴플라이언스 기본값을 이긴다."""

    def test_meta_changes_the_check(self):
        from app.compliance import Snapshot, _check_required_tags

        items = [instance("i-1", tags={"Name": "a", "Env": "prod"})]

        # 기본값(Name, Env)으로는 깨끗하다
        snap = Snapshot(1, items)
        assert list(_check_required_tags(snap)) == []

        # 고객사가 Owner 를 요구하면 위반이 된다
        snap = Snapshot(1, items, meta={"required_tags": ["Name", "Env", "Owner"]})
        found = list(_check_required_tags(snap))
        assert len(found) == 1
        assert "Owner" in found[0][1]

    def test_check_stays_pure(self):
        """점검 함수가 DB 를 보면 테스트에서 손으로 만들어 먹일 수 없다.

        주석에는 standards 를 언급해도 된다. 코드에서 부르지만 않으면 된다 -
        그래서 문자열 검색이 아니라 AST 로 실제 호출과 import 만 본다.
        """
        import ast
        import inspect
        import textwrap

        from app import compliance

        tree = ast.parse(textwrap.dedent(
            inspect.getsource(compliance._check_required_tags)))

        called = {node.func.id for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert "_connect" not in called

        imported = [node for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))]
        assert imported == [], "점검 함수 안에서 무언가를 import 하고 있습니다"


@pytest.mark.db
class TestRoutes:
    def test_page(self, db_client):
        assert db_client.get("/customer/standards").status_code == 200

    def test_says_what_zero_means(self, db_client):
        body = db_client.get("/customer/standards").get_data(as_text=True)
        assert "볼 수가 없다" in body

    def test_bad_value_does_not_break_the_page(self, db_client):
        r = db_client.post("/customer/standards/save",
                           data={"customer": "가고객", "rule": "tag_values",
                                 "value": "형식아님"},
                           follow_redirects=True)
        assert r.status_code == 200
        assert "형식으로 적으세요" in r.get_data(as_text=True)
