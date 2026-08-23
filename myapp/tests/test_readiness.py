# tests/test_readiness.py
# 온보딩 준비도.
#
# 점검 함수는 DB 를 보지 않는 순수 함수라 facts 를 손으로 만들어 먹인다.
# 컴플라이언스와 같은 방식이다 - 자료 모으기(gather)와 판정(check)을
# 나눠 둔 이유가 이것이다.

import re
from datetime import datetime, timedelta, timezone

import pytest

from app import readiness as R


def facts(**over):
    """전부 갖춰진 고객사. 테스트마다 필요한 것만 망가뜨린다."""
    base = {
        "customer": "가고객",
        "accounts": [{
            "account_id": "123456789012", "alias": "prod", "regions": ["ap-northeast-2"],
            "enabled": True, "role_arn": "arn:aws:iam::123456789012:role/msp",
            "external_id": "x",
        }],
        "alarm_count": 12,
        "last_alarm": datetime.now(timezone.utc),
        "sla_targets": [{"severity": "critical", "minutes": 15, "customer": "가고객"}],
        "oncall": [{"name": "김당직", "level": 1, "slack_id": "U01", "customer": "가고객"}],
        "last_snapshot": datetime.now(timezone.utc),
        "runbook_gaps": [],
        "work_orders": 3,
        "routines": [{"name": "월간 점검", "interval_days": 30, "active": True,
                      "last_done_at": datetime.now(timezone.utc)}],
        "contacts": [{"kind": "emergency", "name": "김운영"},
                     {"kind": "report", "name": "박보고"}],
        "missing_tables": [],
    }
    base.update(over)
    return base


def status_of(results, check_id):
    return next(r["status"] for r in results if r["id"] == check_id)


def test_helper_mirrors_gather():
    """이 파일의 facts() 가 gather() 와 어긋나면 점검 함수가 KeyError 로 터진다.

    점검을 하나 추가하면서 gather 에 자료를 늘렸는데 여기를 안 고치면,
    화면은 멀쩡한데 테스트만 무더기로 깨진다. 어느 쪽이 문제인지 바로
    알 수 있게 이 한 줄을 둔다.
    """
    import inspect

    source = inspect.getsource(R.gather)
    # gather 가 facts 에 처음 채워 넣는 키들.
    declared = set(re.findall(r'^\s{8}"(\w+)":', source, re.M))
    assert declared <= set(facts()), (
        "gather() 에는 있는데 테스트 헬퍼에 없는 키: "
        f"{sorted(declared - set(facts()))}"
    )


class TestAllGood:
    def test_everything_passes(self):
        results = R.check(facts())
        assert all(r["status"] == "ok" for r in results), [
            (r["id"], r["detail"]) for r in results if r["status"] != "ok"
        ]

    def test_summary_says_ready(self):
        s = R.summarize(R.check(facts()))
        assert s["ready"] is True
        assert s["blocking"] == 0
        assert s["done"] == s["total"]


class TestAccounts:
    def test_no_account(self):
        results = R.check(facts(accounts=[]))
        assert status_of(results, "account-registered") == "missing"

    def test_all_accounts_disabled(self):
        acct = facts()["accounts"][0] | {"enabled": False}
        results = R.check(facts(accounts=[acct]))
        assert status_of(results, "account-registered") == "warn"

    def test_demo_account_is_not_ready(self):
        """역할이 없으면 화면에는 값이 보이지만 전부 합성 자료다."""
        acct = facts()["accounts"][0] | {"role_arn": ""}
        results = R.check(facts(accounts=[acct]))
        assert status_of(results, "assume-role") == "missing"

    def test_partially_demo_is_a_warning(self):
        real = facts()["accounts"][0]
        demo = real | {"account_id": "999988887777", "role_arn": ""}
        results = R.check(facts(accounts=[real, demo]))
        assert status_of(results, "assume-role") == "warn"

    def test_empty_regions(self):
        acct = facts()["accounts"][0] | {"regions": []}
        results = R.check(facts(accounts=[acct]))
        assert status_of(results, "regions") == "missing"


class TestAlarms:
    def test_no_alarms_arriving(self):
        """조용한 것과 끊긴 것은 화면에서 똑같아 보인다."""
        results = R.check(facts(alarm_count=0, last_alarm=None))
        assert status_of(results, "alarm-arriving") == "missing"

    def test_missing_events_table_is_unknown_not_missing(self):
        """자료가 없는 것과 설정이 없는 것은 다르다.
        테이블이 없는데 '알람이 안 온다' 고 하면 엉뚱한 곳을 고치게 된다."""
        results = R.check(facts(missing_tables=["events"]))
        assert status_of(results, "alarm-arriving") == "unknown"


class TestSla:
    def test_no_target(self):
        results = R.check(facts(sla_targets=[]))
        assert status_of(results, "sla-target") == "missing"

    def test_only_the_shared_default_is_a_warning(self):
        """app/sla.py 는 전용 목표가 없으면 기본값으로 판정한다.
        여기서 '없음' 이라고 하면 두 화면이 서로 다른 말을 한다.
        그렇다고 '완료' 로 두면 계약과 다른 값으로 도는 걸 못 잡는다."""
        default = {"severity": "critical", "minutes": 30, "customer": ""}
        results = R.check(facts(sla_targets=[default]))
        assert status_of(results, "sla-target") == "warn"

    def test_missing_table_is_unknown(self):
        results = R.check(facts(missing_tables=["sla_targets"]))
        assert status_of(results, "sla-target") == "unknown"


class TestOncall:
    def test_nobody(self):
        results = R.check(facts(oncall=[]))
        assert status_of(results, "oncall") == "missing"

    def test_member_without_slack_id_is_a_warning(self):
        """Slack ID 가 없으면 멘션이 안 걸린다. 등록은 됐는데 안 불린다."""
        member = {"name": "박팀장", "level": 2, "slack_id": "", "customer": ""}
        results = R.check(facts(oncall=[member]))
        assert status_of(results, "oncall") == "warn"
        assert "박팀장" in next(r["detail"] for r in results if r["id"] == "oncall")

    def test_only_shared_members_is_a_warning(self):
        member = {"name": "김당직", "level": 1, "slack_id": "U01", "customer": ""}
        results = R.check(facts(oncall=[member]))
        assert status_of(results, "oncall") == "warn"


class TestSnapshot:
    def test_none(self):
        results = R.check(facts(last_snapshot=None))
        assert status_of(results, "snapshot") == "missing"

    def test_stale(self):
        old = datetime.now(timezone.utc) - timedelta(days=R.SNAPSHOT_STALE_DAYS + 3)
        results = R.check(facts(last_snapshot=old))
        assert status_of(results, "snapshot") == "warn"

    def test_fresh(self):
        results = R.check(facts())
        assert status_of(results, "snapshot") == "ok"


class TestRunbooksAndWork:
    def test_gaps_are_a_warning(self):
        gaps = [{"fingerprint": "abc", "times": 8, "sample": "디스크 사용률 95%"}]
        results = R.check(facts(runbook_gaps=gaps))
        assert status_of(results, "runbook") == "warn"

    def test_no_work_orders(self):
        results = R.check(facts(work_orders=0))
        assert status_of(results, "work-evidence") == "missing"


class TestOrdering:
    def test_unfinished_items_come_first(self):
        results = R.check(facts(sla_targets=[], work_orders=0))
        assert results[0]["status"] != "ok"

    def test_required_before_optional_among_unfinished(self):
        """선택 항목이 필수보다 위에 오면 무엇부터 할지 알 수 없다."""
        results = R.check(facts(sla_targets=[], work_orders=0))
        unfinished = [r for r in results if r["status"] != "ok"]
        levels = [R.LEVEL_ORDER[r["level"]] for r in unfinished]
        assert levels == sorted(levels)


class TestSummary:
    def test_optional_gap_does_not_block(self):
        """선택 항목이 비었다고 고객사를 못 받는 건 아니다."""
        s = R.summarize(R.check(facts(work_orders=0)))
        assert s["ready"] is True
        assert s["done"] < s["total"]

    def test_required_gap_blocks(self):
        s = R.summarize(R.check(facts(sla_targets=[])))
        assert s["ready"] is False
        assert s["blocking"] == 1

    def test_required_warning_also_blocks(self):
        """'있긴 한데 온전하지 않다' 를 통과시키면, Slack ID 없는 담당자만
        등록해두고 준비가 끝난 줄 알게 된다."""
        member = {"name": "박팀장", "level": 1, "slack_id": "", "customer": "가고객"}
        s = R.summarize(R.check(facts(oncall=[member])))
        assert s["ready"] is False

    def test_unknown_is_counted_separately(self):
        """판단 불가를 '됐다' 로도 '안 됐다' 로도 세면 안 된다."""
        s = R.summarize(R.check(facts(missing_tables=["events", "sla_targets"])))
        assert s["unknown"] == 2


class TestChecksTable:
    def test_ids_are_unique(self):
        ids = [c["id"] for c in R.CHECKS]
        assert len(ids) == len(set(ids))

    def test_every_check_says_how_to_fix(self):
        """'안 됐다' 만 알려주면 화면을 두 번 돌게 된다."""
        for c in R.CHECKS:
            assert c["how"], c["id"]
            assert c["why"], c["id"]
            assert c["level"] in R.LEVELS, c["id"]

    def test_endpoint_references_are_labelled(self):
        """화면에 'report.sla' 라고 찍으면 아는 사람만 읽는다."""
        for c in R.CHECKS:
            if c["how"].startswith("endpoint:"):
                assert c["how"][9:] in R.ENDPOINT_LABELS, c["id"]

    def test_labels_name_a_real_menu_path(self):
        """라벨이 없는 메뉴 경로를 가리키면 찾다가 포기한다.

        "리포트 > SLA 목표" 라고 적혀 있었는데 리포트 메뉴에는 SLA 가
        없었다. 카테고리 이름으로 시작하는 라벨은 그 카테고리에 실제로
        그 메뉴가 있어야 한다.
        """
        from app import nav

        by_endpoint = {i["endpoint"]: i for i in nav.ITEMS}
        labels = {c["label"]: c["id"] for c in nav.CATEGORIES}

        for endpoint, label in R.ENDPOINT_LABELS.items():
            if ">" not in label:
                continue
            head, tail = [p.strip() for p in label.split(">", 1)]
            assert head in labels, f"{endpoint}: 없는 카테고리 '{head}'"
            item = by_endpoint.get(endpoint)
            assert item, f"{endpoint}: 메뉴에 없는데 메뉴 경로처럼 적혀 있습니다"
            assert item["category"] == labels[head], (
                f"{endpoint}: 라벨은 '{head}' 인데 실제로는 "
                f"'{item['category']}' 에 있습니다"
            )
            assert item["label"] == tail, (
                f"{endpoint}: 라벨은 '{tail}' 인데 메뉴 이름은 "
                f"'{item['label']}' 입니다"
            )

    def test_every_check_returns_a_known_status(self):
        results = R.check(facts())
        for r in results:
            assert r["status"] in R.STATUS_LABEL


@pytest.mark.db
class TestAgainstRealData:
    def test_gather_runs(self, db_app, db_uri):
        from app.customer import names

        with db_app.app_context():
            all_names = names()
            if not all_names:
                pytest.skip("등록된 고객사가 없습니다")
            for name in all_names:
                results, f = R.evaluate(name)
                assert len(results) == len(R.CHECKS)
                assert R.summarize(results)["total"] == len(R.CHECKS)

    def test_unknown_customer_is_not_ready(self, db_app, db_uri):
        with db_app.app_context():
            results, _ = R.evaluate("있을리 없는 고객사")
            assert R.summarize(results)["ready"] is False


class TestMatrix:
    """고객사 × 점검 항목 표.

    온보딩 준비도와 점검 목록을 공유한다. 여기서 항목을 새로 정의하면
    두 화면이 반드시 어긋나므로, 그것부터 고정한다.
    """

    def fake_evaluate(self, monkeypatch, per_customer):
        """고객사 이름 -> facts 덮어쓰기. DB 없이 표를 만들어 본다."""
        def evaluate(name):
            f = facts(**per_customer.get(name, {}))
            return R.check(f), f
        monkeypatch.setattr(R, "evaluate", evaluate)

    def test_uses_the_same_checks_as_readiness(self, monkeypatch):
        self.fake_evaluate(monkeypatch, {})
        data = R.matrix(["가고객"])
        assert [c["id"] for c in data["checks"]] == [c["id"] for c in R.CHECKS]

    def test_every_customer_gets_every_check(self, monkeypatch):
        self.fake_evaluate(monkeypatch, {})
        data = R.matrix(["가고객", "나고객"])
        for row in data["rows"]:
            assert set(row["results"]) == {c["id"] for c in R.CHECKS}

    def test_gaps_list_who_is_missing(self, monkeypatch):
        self.fake_evaluate(monkeypatch, {"나고객": {"oncall": []}})
        data = R.matrix(["가고객", "나고객"])
        assert data["gaps"]["oncall"] == ["나고객"]

    def test_gaps_include_warnings_not_only_missing(self, monkeypatch):
        """'확인 필요' 도 빈 곳이다. ok 가 아닌 것을 전부 센다."""
        self.fake_evaluate(monkeypatch, {
            "나고객": {"oncall": [{"name": "김", "level": 1, "slack_id": "",
                                   "customer": "나고객"}]},
        })
        data = R.matrix(["가고객", "나고객"])
        assert data["gaps"]["oncall"] == ["나고객"]

    def test_worst_customer_first(self, monkeypatch):
        """다 채운 곳은 볼 일이 없다. 빈 곳이 많은 순으로 온다."""
        self.fake_evaluate(monkeypatch, {
            "빈곳많음": {"accounts": [], "oncall": [], "sla_targets": []},
        })
        data = R.matrix(["가고객", "빈곳많음"])
        assert data["rows"][0]["customer"] == "빈곳많음"

    def test_one_broken_customer_does_not_hide_the_others(self, monkeypatch):
        """계정 하나가 이상해서 표 전체가 안 보이면 다른 고객사의 빈 칸을 못 본다."""
        def evaluate(name):
            if name == "터진고객":
                raise R.ReadinessError("이 고객사를 읽지 못했습니다")
            f = facts()
            return R.check(f), f
        monkeypatch.setattr(R, "evaluate", evaluate)

        data = R.matrix(["터진고객", "가고객"])
        assert data["rows"][0]["error"] == "이 고객사를 읽지 못했습니다"
        assert data["rows"][0]["results"] == []
        # 멀쩡한 고객사는 그대로 채워진다.
        other = [r for r in data["rows"] if r["customer"] == "가고객"][0]
        assert other["summary"]["total"] == len(R.CHECKS)

    def test_broken_customer_is_not_counted_as_a_gap(self, monkeypatch):
        """읽지 못한 것을 '비었다' 로 세면 없는 문제를 만들어 낸다."""
        def evaluate(name):
            raise R.ReadinessError("못 읽음")
        monkeypatch.setattr(R, "evaluate", evaluate)

        data = R.matrix(["터진고객"])
        assert all(who == [] for who in data["gaps"].values())

    def test_no_customers(self, monkeypatch):
        self.fake_evaluate(monkeypatch, {})
        data = R.matrix([])
        assert data["rows"] == []
        assert set(data["gaps"]) == {c["id"] for c in R.CHECKS}


class TestNewChecks:
    def test_external_id_missing_is_flagged(self):
        results = R.check(facts(accounts=[{
            "account_id": "1", "alias": "", "regions": ["ap-northeast-2"],
            "enabled": True, "role_arn": "arn:...:role/R", "external_id": "",
        }]))
        assert status_of(results, "external-id") == "missing"

    def test_demo_only_is_unknown_not_ok(self):
        """확인할 대상이 없는 것을 '완료' 로 적으면 안 한 것을 한 것처럼 만든다."""
        results = R.check(facts(accounts=[{
            "account_id": "1", "alias": "", "regions": ["ap-northeast-2"],
            "enabled": True, "role_arn": "", "external_id": "",
        }]))
        assert status_of(results, "external-id") == "unknown"

    def test_routines_missing(self):
        assert status_of(R.check(facts(routines=[])), "routines") == "missing"

    def test_overdue_routine_is_a_warning(self):
        old = datetime.now(timezone.utc) - timedelta(days=90)
        results = R.check(facts(routines=[
            {"name": "월간 점검", "interval_days": 30, "active": True,
             "last_done_at": old},
        ]))
        assert status_of(results, "routines") == "warn"

    def test_missing_table_is_unknown(self):
        results = R.check(facts(missing_tables=["customer_routines"]))
        assert status_of(results, "routines") == "unknown"
