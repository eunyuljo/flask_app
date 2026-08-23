# tests/test_samples.py
# 샘플 알람이 '실제로 들어오는 모양' 인가.
#
# ── 이 파일이 생긴 이유 ─────────────────────────────────────────────
# 예전 샘플은 우리 형식(msg/level/source)으로 만들어 events 에 직접
# INSERT 했다. 그래서 두 가지가 프로젝트 내내 안 보였다.
#
#   1) 진짜 CloudWatch 페이로드가 정규화에 거부되고 있었다.
#      화면은 200건으로 가득 차 있었지만 그 200건은 전부 우리가
#      우리 형식으로 만들어 넣은 것이었다.
#   2) account_id 가 비어 있었다. SLA 는 200건을 전부 '미귀속' 으로 셌고
#      고객사 축 화면은 영원히 비어 있었다.
#
# 샘플이 진짜 경로를 밟지 않으면 샘플로서 값어치가 없다. 여기서 지키는
# 것은 그 한 가지다.

import json
from datetime import datetime, timezone

import pytest

from api.normalize_handler import UNPARSED_TYPE, normalize
from app import samples


ACCOUNTS = [
    {"account_id": "111111111111", "regions": ["ap-northeast-2", "us-east-1"]},
    {"account_id": "222222222222", "regions": ["ap-northeast-2"]},
]
RESOURCES = {
    "111111111111": ["i-0a1b2c3d4e5f6a7b8", "i-0999888877776666f"],
    "222222222222": ["i-0c0ffee0c0ffee0c0"],
}


def made(count=200, hours=48, seed=7, accounts=None, resources=None):
    return samples.make(count, hours,
                        ACCOUNTS if accounts is None else accounts,
                        RESOURCES if resources is None else resources,
                        seed=seed)


class TestEverythingSurvivesTheRealPath:
    """만든 페이로드는 정규화기를 그대로 통과해야 한다.

    어댑터가 깨지면 이 테스트가 깨진다. 그게 목적이다.
    """

    def test_nothing_is_rejected(self):
        for raw, kind in made(300):
            try:
                normalize(raw)
            except ValueError as e:
                pytest.fail(f"{kind} 페이로드가 거부됐습니다: {e}\n{json.dumps(raw)[:300]}")

    def test_every_sender_is_represented(self):
        kinds = {kind for _, kind in made(600)}
        assert kinds == {k for k, _ in samples.MIX}

    def test_aws_senders_are_read_by_adapters(self):
        wanted = {"cloudwatch": "cloudwatch_alarm", "guardduty": "guardduty",
                  "health": "health", "eventbridge": "eventbridge"}
        for raw, kind in made(600):
            if kind in wanted:
                assert normalize(raw)["meta"]["adapter"] == wanted[kind]

    def test_self_monitoring_goes_through_the_alias_table(self):
        """자체 형식은 어댑터가 아니라 별칭표가 읽는다. 둘 다 살아 있어야 한다."""
        seen = False
        for raw, kind in made(400):
            if kind == "monitoring":
                record = normalize(raw)
                assert record["meta"].get("adapter") is None
                assert record["event_type"] != UNPARSED_TYPE
                assert record["message"].strip()
                seen = True
        assert seen

    def test_unknown_senders_land_as_unparsed_not_lost(self):
        """어댑터 없는 발신자가 0건이면 '정규화 못 함' 화면을 영영 못 본다."""
        seen = False
        for raw, kind in made(400):
            if kind == "unknown":
                assert normalize(raw)["event_type"] == UNPARSED_TYPE
                seen = True
        assert seen


class TestAttribution:
    """계정이 안 붙으면 SLA·리포트·고객사 화면이 전부 이 이벤트를 흘려버린다."""

    def test_aws_events_carry_a_registered_account(self):
        known = {a["account_id"] for a in ACCOUNTS}
        for raw, kind in made(400):
            if kind in ("cloudwatch", "guardduty", "health", "eventbridge"):
                assert normalize(raw)["account_id"] in known

    def test_self_monitoring_has_no_account_and_that_is_correct(self):
        """자체 모니터링은 보통 호스트 이름만 안다. 계정을 지어내지 않는다."""
        for raw, kind in made(400):
            if kind == "monitoring":
                assert normalize(raw)["account_id"] == ""

    def test_regions_come_from_the_account(self):
        allowed = {r for a in ACCOUNTS for r in a["regions"]}
        for raw, kind in made(400):
            if kind in ("guardduty", "health", "eventbridge"):
                assert raw["region"] in allowed

    def test_falls_back_when_no_accounts_are_registered(self):
        """계정이 없으면 계정 없는 AWS 알람을 지어내느니 자체 형식으로 대신한다."""
        kinds = {kind for _, kind in made(200, accounts=[], resources={})}
        assert kinds <= {"monitoring", "unknown"}


class TestResourceLinking:
    def test_cloudwatch_dimensions_point_at_real_resources(self):
        known = {r for ids in RESOURCES.values() for r in ids}
        found = 0
        for raw, kind in made(400):
            if kind != "cloudwatch":
                continue
            meta = normalize(raw)["meta"]
            if meta.get("resource_id"):
                assert meta["resource_id"] in known
                found += 1
        assert found, "리소스가 붙은 CloudWatch 알람이 하나도 없습니다"

    def test_resource_belongs_to_the_same_account(self):
        for raw, kind in made(400):
            if kind != "cloudwatch":
                continue
            record = normalize(raw)
            resource_id = record["meta"].get("resource_id")
            if resource_id:
                assert resource_id in RESOURCES[record["account_id"]]

    def test_works_without_any_collected_resources(self):
        """리소스를 아직 수집 안 했어도 샘플은 만들어져야 한다."""
        for raw, _ in made(200, resources={}):
            normalize(raw)


class TestShapesAreReal:
    def test_cloudwatch_comes_in_an_sns_envelope(self):
        """봉투째 오는 것이 정상이다. 벗겨서 보내면 실제와 달라진다."""
        for raw, kind in made(200):
            if kind == "cloudwatch":
                assert "Records" in raw
                assert raw["Records"][0]["Sns"]["Message"]
                inner = json.loads(raw["Records"][0]["Sns"]["Message"])
                assert "AlarmName" in inner and "NewStateValue" in inner
                return
        pytest.fail("cloudwatch 샘플이 없습니다")

    def test_recovery_is_included_too(self):
        """OK 도 이벤트로 받는다. 복구됐다는 사실도 당직자가 알아야 한다."""
        states = set()
        for raw, kind in made(400):
            if kind == "cloudwatch":
                states.add(json.loads(raw["Records"][0]["Sns"]["Message"])["NewStateValue"])
        assert "ALARM" in states and "OK" in states

    def test_alarm_names_repeat_so_fingerprints_group(self):
        """이름을 매번 새로 만들면 200건이 200종이 된다. 그러면 순위표도
        억제도 런북도 의미가 없어진다."""
        prints = [normalize(raw)["fingerprint"]
                  for raw, kind in made(300) if kind == "cloudwatch"]
        assert len(prints) > 3 * len(set(prints))

    def test_guardduty_severity_is_numeric_in_the_payload(self):
        for raw, kind in made(400):
            if kind == "guardduty":
                assert isinstance(raw["detail"]["severity"], (int, float))
                return
        pytest.fail("guardduty 샘플이 없습니다")


class TestReproducible:
    """--seed 를 주면 같은 샘플이 나와야 한다. 화면을 두고 "이 알람 말인데"
    를 하려면 필요하다.

    기준 시각도 함께 고정한다. seed 만으로는 안 된다 - 시각이 '지금부터
    거슬러' 라서 부를 때마다 절대 시각이 달라진다.
    """

    PINNED = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def twice(self, seed):
        return [json.dumps(samples.make(50, 48, ACCOUNTS, RESOURCES,
                                        seed=seed, now=self.PINNED),
                           sort_keys=True, default=str) for _ in range(2)]

    def test_same_seed_gives_the_same_samples(self):
        first, second = self.twice(3)
        assert first == second

    def test_different_seeds_differ(self):
        assert self.twice(3)[0] != self.twice(4)[0]

    def test_seed_alone_still_repeats_the_structure(self):
        """시각을 안 고정해도 어느 알람이 몇 번 나오는지는 같아야 한다."""
        def names(seed):
            return [kind for _, kind in made(80, seed=seed)]
        assert names(11) == names(11)

    def test_no_side_effects(self):
        """DB 도 네트워크도 건드리지 않는다. 부르는 쪽이 적재를 맡는다."""
        import ast
        import pathlib
        src = (pathlib.Path(__file__).parent.parent / "app" / "samples.py").read_text("utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & {"psycopg", "boto3", "flask", "requests", "anthropic"})


class TestSpread:
    def test_events_land_inside_the_window(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        for raw, _ in made(200, hours=48):
            record = normalize(raw)
            occurred = datetime.fromisoformat(record["occurred_at"])
            assert now - timedelta(hours=49) <= occurred <= now + timedelta(minutes=1)

    def test_severity_is_not_all_one_level(self):
        levels = {normalize(raw)["severity"] for raw, _ in made(400)}
        assert len(levels) >= 3
