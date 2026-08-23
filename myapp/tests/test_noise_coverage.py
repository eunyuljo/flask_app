# tests/test_noise_coverage.py
# 이 알람에 절차가 있어야 하는가, 있는가.
#
# ── 이 기능을 만든 이유 ─────────────────────────────────────────────
# runbooks 테이블이 0건이었다. 절차를 쓰는 화면도, AI 초안도, 알람에서
# 절차로 가는 링크도 전부 이미 있었는데 아무도 쓰지 않았다.
#
# 어느 지문부터 써야 하는지 아무 데서도 말해주지 않아서다. ranking() 은
# 진작부터 has_runbook 을 뽑고 있었고 화면도 줄마다 "절차 없음" 을 적고
# 있었지만, 30줄을 눈으로 훑어야 보였고 순서가 없었다.
#
# ── 억제 축과 섞지 않는다 ───────────────────────────────────────────
# advise() 의 verdict 는 "이 알람을 꺼도 되는가" 다. 여기는 "이 알람이
# 왔을 때 무엇을 해야 하는지 적혀 있는가" 다. 같은 알람이 양쪽에서 다른
# 답을 가질 수 있어서 한 값으로 뭉개면 둘 다 못 읽는다.

import pytest

from app import noise


def row(fingerprint="a", count=100, severity="info", has_runbook=False,
        event_type="alarm", **extra):
    return {"fingerprint": fingerprint, "c": count, "severity": severity,
            "has_runbook": has_runbook, "event_type": event_type,
            "sample": "예시", "source": "web", "muted": False,
            "window_minutes": None, **extra}


def link(incidents=1, origin=0):
    return {"incidents": incidents, "published": incidents, "origin": origin,
            "last_at": None, "last_title": "결제 장애", "last_id": 7}


def verdict(r, l=None):
    return noise.coverage(r, l)[0]


class TestCoverage:
    def test_runbook_present_is_covered(self):
        assert verdict(row(has_runbook=True)) == "covered"

    def test_incident_linked_without_a_runbook_is_the_most_urgent(self):
        """이 한 줄이 이 기능을 만든 이유다.

        장애로 이어진 적이 있다는 것은 무엇을 해야 하는지 이미 한 번
        겪었다는 뜻이다. 그런데 아무도 적어두지 않았다.
        """
        got, why = noise.coverage(row(count=1), link(incidents=2))
        assert got == "proven"
        assert "장애 2건" in why

    def test_high_severity_needs_a_runbook_even_once(self):
        """새벽에 처음 보는 critical 앞에서 검색을 시작하게 두지 않는다."""
        assert verdict(row(count=1, severity="critical")) == "urgent"
        assert verdict(row(count=1, severity="error")) == "urgent"

    def test_repetition_alone_is_enough(self):
        assert verdict(row(count=noise.WORTH_A_RUNBOOK)) == "frequent"

    def test_rare_and_light_is_not_a_gap(self):
        """드물고 가벼운 것까지 절차를 쓰라고 하면 목록이 의미 없어진다."""
        assert verdict(row(count=noise.WORTH_A_RUNBOOK - 1)) is None

    def test_threshold_is_inclusive(self):
        assert verdict(row(count=noise.WORTH_A_RUNBOOK)) == "frequent"
        assert verdict(row(count=noise.WORTH_A_RUNBOOK - 1)) is None

    def test_runbook_wins_over_everything(self):
        """절차가 있으면 장애 이력이 있어도 빈 자리가 아니다."""
        assert verdict(row(has_runbook=True, severity="critical"),
                       link(incidents=5)) == "covered"

    def test_threshold_differs_from_the_suppression_one(self):
        """문턱을 억제와 같이 두면 20회 미만은 영영 절차가 안 생긴다."""
        assert noise.WORTH_A_RUNBOOK < noise.NOISY_ENOUGH


class TestNotEverythingNeedsARunbook:
    """정규화 못 한 페이로드에 대응 절차를 쓰라고 하면 안 된다.

    그건 어댑터로 고칠 일이다. 여기에 섞이면 진짜 빈 자리가 묻힌다.
    """

    @pytest.mark.parametrize("event_type", noise.NOT_A_RUNBOOK_TARGET)
    def test_excluded(self, event_type):
        assert verdict(row(count=500, severity="critical",
                           event_type=event_type)) is None

    def test_unparsed_is_among_them(self):
        from api.normalize_handler import UNPARSED_TYPE
        assert UNPARSED_TYPE in noise.NOT_A_RUNBOOK_TARGET

    def test_counted_separately_not_as_no_reason(self):
        """'절차 대상 아님' 과 '아직 쓸 이유 없음' 은 다르다."""
        rows = noise.advise([row("u", 500, event_type="unparsed"),
                             row("q", 1)], {})
        counts = noise.coverage_summary(rows)
        assert counts["skipped"] == 1
        assert counts["gap"] == 0


class TestOrdering:
    def test_proven_beats_urgent_beats_frequent(self):
        rows = [row("freq", 50), row("urg", 1, severity="critical"),
                row("prov", 1)]
        got = noise.uncovered(rows, {"prov": link()})
        assert [r["fingerprint"] for r in got] == ["prov", "urg", "freq"]

    def test_a_single_critical_beats_fifty_infos(self):
        """순위표와 순서가 다르다. 그래서 목록을 따로 낸다."""
        rows = [row("noisy", 50, severity="info"),
                row("bad", 1, severity="critical")]
        assert noise.uncovered(rows, {})[0]["fingerprint"] == "bad"

    def test_count_breaks_ties(self):
        rows = [row("few", 5), row("many", 40)]
        assert [r["fingerprint"] for r in noise.uncovered(rows, {})] == \
            ["many", "few"]

    def test_covered_and_quiet_are_left_out(self):
        rows = [row("ok", 500, has_runbook=True), row("quiet", 1),
                row("gap", 50)]
        assert [r["fingerprint"] for r in noise.uncovered(rows, {})] == ["gap"]

    def test_limit_keeps_the_list_short(self):
        """목록이 길면 '나중에' 가 된다."""
        rows = [row(f"f{i}", 50) for i in range(20)]
        assert len(noise.uncovered(rows, {}, limit=5)) == 5

    def test_no_limit_returns_everything(self):
        rows = [row(f"f{i}", 50) for i in range(20)]
        assert len(noise.uncovered(rows, {}, limit=0)) == 20


class TestTwoAxesStaySeparate:
    """억제 판정과 절차 판정은 서로를 건드리지 않는다."""

    def test_suppression_order_is_unchanged(self):
        """절차 축을 붙였다고 순위표 순서가 바뀌면 안 된다.

        순위표는 억제 축 목록이다. 절차 순서는 uncovered() 가 낸다.
        """
        rows = [row("keepme", 500), row("suppressme", 500)]
        got = noise.advise(rows, {"keepme": link()})
        assert [r["fingerprint"] for r in got] == ["suppressme", "keepme"]

    def test_same_alarm_can_be_suppressible_and_still_need_a_runbook(self):
        """한 값으로 뭉개면 이 경우를 표현할 수 없다."""
        got = noise.advise([row(count=500, severity="critical")], {})[0]
        assert got["verdict"] == "suppress"
        assert got["coverage"] == "urgent"

    def test_advise_attaches_both(self):
        got = noise.advise([row()], {})[0]
        assert "verdict" in got and "coverage" in got and "coverage_why" in got

    def test_missing_keys_do_not_explode(self):
        """ranking() 이 아닌 곳에서 온 줄이라도 판정이 터지지 않아야 한다."""
        assert noise.coverage({}, None) == (None, "")


class TestCoverageSummary:
    def test_counts_each_state(self):
        rows = noise.advise([
            row("ok", 500, has_runbook=True),
            row("prov", 1),
            row("urg", 1, severity="error"),
            row("freq", 50),
            row("quiet", 1),
        ], {"prov": link()})
        got = noise.coverage_summary(rows)
        assert got["covered"] == 1
        assert got["proven"] == 1
        assert got["urgent"] == 1
        assert got["frequent"] == 1
        assert got["gap"] == 3

    def test_gap_is_the_sum_of_the_three(self):
        rows = noise.advise([row("a", 50), row("b", 50)], {})
        got = noise.coverage_summary(rows)
        assert got["gap"] == got["proven"] + got["urgent"] + got["frequent"]

    def test_empty(self):
        assert noise.coverage_summary([])["gap"] == 0


@pytest.mark.db
class TestOnScreen:
    @pytest.fixture
    def page(self, db_app, db_uri):
        db_app.config["WTF_CSRF_ENABLED"] = False
        client = db_app.test_client()
        client.post("/auth/login", data={"username": "admin", "password": "1234"})
        return client.get("/noise/").get_data(as_text=True)

    def test_the_block_is_there(self, page):
        assert "다음에 쓸 절차" in page

    def test_coverage_percentage_is_shown(self, page):
        assert "절차 있는 알람 종" in page

    def test_two_coverage_numbers_not_one(self, page):
        """종 기준과 건수 기준이 다르다. 한 숫자로 합치면 그 사실이 사라진다."""
        assert "절차 있는 알람 종" in page
        assert "절차로 덮인 알람 건수" in page

    def test_suppression_section_is_still_named(self, page):
        """두 판단이 한 제목 아래 섞이지 않는다."""
        assert "억제 판단" in page

    def test_write_link_carries_the_grounds(self, db_app, db_uri):
        """빈 폼을 주면 아무도 안 쓴다. 초안을 만들 근거를 함께 넘긴다."""
        from app import noise as n

        with db_app.app_context():
            rows = n.advise(n.ranking(720), {})
            gaps = n.uncovered(rows, {}, limit=5)
        if not gaps:
            pytest.skip("절차가 비어 있는 알람이 없습니다")
        db_app.config["WTF_CSRF_ENABLED"] = False
        client = db_app.test_client()
        client.post("/auth/login", data={"username": "admin", "password": "1234"})
        body = client.get("/noise/").get_data(as_text=True)
        assert f"fingerprint={gaps[0]['fingerprint']}" in body
        assert "severity=" in body and "source=" in body


@pytest.mark.db
class TestRankingCarriesEventType:
    def test_event_type_is_selected(self, db_app, db_uri):
        """커버리지 판정이 unparsed 를 가려내려면 이 값이 있어야 한다."""
        with db_app.app_context():
            rows = noise.ranking(720, limit=1)
        if not rows:
            pytest.skip("이벤트가 없습니다")
        assert "event_type" in rows[0]

    def test_summary_reports_coverage(self, db_app, db_uri):
        with db_app.app_context():
            got = noise.summary(720)
        for key in ("covered_kinds", "covered_events",
                    "covered_kinds_pct", "covered_events_pct"):
            assert key in got
        assert 0 <= got["covered_kinds_pct"] <= 100
        assert 0 <= got["covered_events_pct"] <= 100


class TestSeverityBeatsCountWithinAVerdict:
    """실제 데이터로 돌려보고 찾은 것.

    3번 난 error 셋이 3번 난 critical 위에 왔다. 절차를 하나만 쓸
    시간이 있다면 critical 부터 써야 한다.
    """

    def test_critical_outranks_error_at_the_same_count(self):
        rows = [row("e1", 3, severity="error"), row("e2", 3, severity="error"),
                row("c1", 3, severity="critical")]
        assert noise.uncovered(rows, {})[0]["fingerprint"] == "c1"

    def test_count_still_breaks_ties_within_a_severity(self):
        rows = [row("few", 3, severity="error"), row("many", 30, severity="error")]
        assert [r["fingerprint"] for r in noise.uncovered(rows, {})] == \
            ["many", "few"]

    def test_verdict_still_outranks_severity(self):
        """장애로 이어진 info 가 아직 안 겪은 critical 보다 먼저다."""
        rows = [row("crit", 1, severity="critical"),
                row("prov", 1, severity="info")]
        got = noise.uncovered(rows, {"prov": link()})
        assert [r["fingerprint"] for r in got] == ["prov", "crit"]
