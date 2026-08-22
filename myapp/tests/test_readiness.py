# tests/test_readiness.py
# 온보딩 준비도.
#
# 점검 함수는 DB 를 보지 않는 순수 함수라 facts 를 손으로 만들어 먹인다.
# 컴플라이언스와 같은 방식이다 - 자료 모으기(gather)와 판정(check)을
# 나눠 둔 이유가 이것이다.

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
        "missing_tables": [],
    }
    base.update(over)
    return base


def status_of(results, check_id):
    return next(r["status"] for r in results if r["id"] == check_id)


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
