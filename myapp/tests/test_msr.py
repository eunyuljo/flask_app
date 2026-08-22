# tests/test_msr.py
# 월간 서비스 리뷰.
#
# 자료 모으기(collect)는 DB 를 보지만, 제안(suggest)과 슬라이드 배치는
# 자료만 있으면 된다. 그래서 그쪽은 손으로 만들어 먹인다.
#
# 슬라이드는 "예외 없이 만들어졌다" 만 보면 안 된다. 표가 슬라이드 밖으로
# 나가도 예외는 안 나고, 인쇄물에서 조용히 사라진다. 좌표를 직접 잰다.

from datetime import datetime, timedelta, timezone

import pytest

from app import msr

pptx = pytest.importorskip("pptx")

from app.msr_pptx import build  # noqa: E402


START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def data(**over):
    base = {
        "customer": "가고객", "year": 2026, "month": 8,
        "start": START, "end": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "label": "2026년 8월", "days": 31,
        "accounts": [{"account_id": "123456789012", "alias": "prod",
                      "regions": ["ap-northeast-2"]}],
        "alarms": {"total": 120,
                   "by_severity": {"critical": 5, "error": 15, "warning": 40, "info": 60},
                   "prev_total": 100, "change": 20},
        "top_alarms": [],
        "incidents": [], "works": [], "compliance": None,
        "sla": None, "notes": [], "actions": [],
    }
    base.update(over)
    base.setdefault("actions", msr.suggest(base))
    return base


def incident(n, sent=False, ended=True):
    return {
        "id": n, "title": f"결제 API 지연 {n}", "severity": "critical",
        "started_at": START + timedelta(days=n),
        "ended_at": START + timedelta(days=n, hours=2) if ended else None,
        "status": "published",
        "customer_status": "sent" if sent else "draft",
    }


def work(n, closed=True):
    return {
        "id": n, "ticket": f"OPS-{n}", "title": f"보안그룹 수정 {n}",
        "account_id": "123456789012", "region": "ap-northeast-2",
        "operator": "김운영", "status": "closed" if closed else "open",
        "created_at": START + timedelta(days=n),
        "closed_at": START + timedelta(days=n, hours=1) if closed else None,
    }


class TestMonthBounds:
    def test_normal_month(self):
        start, end = msr.month_bounds(2026, 8)
        assert (start.month, end.month) == (8, 9)

    def test_december_rolls_over(self):
        start, end = msr.month_bounds(2026, 12)
        assert (end.year, end.month) == (2027, 1)

    def test_previous_month_of_january(self):
        assert msr.previous_month(2026, 1) == (2025, 12)

    def test_bounds_are_utc(self):
        """서버 시간대에 따라 '8월' 의 범위가 달라지면 안 된다."""
        start, end = msr.month_bounds(2026, 8)
        assert start.tzinfo == timezone.utc and end.tzinfo == timezone.utc

    def test_end_is_exclusive(self):
        """9월 1일 0시를 포함하면 다음 달 첫 알람이 8월에 섞인다."""
        _start, end = msr.month_bounds(2026, 8)
        assert (end.day, end.hour) == (1, 0)


class TestPctChange:
    def test_growth(self):
        assert msr._pct_change(120, 100) == 20

    def test_decline(self):
        assert msr._pct_change(80, 100) == -20

    def test_no_baseline_gives_none(self):
        """0 에서 5 로 늘어난 것을 '500% 증가' 라고 쓰면 뜻이 없다."""
        assert msr._pct_change(5, 0) is None


class TestSuggest:
    def test_unsent_incident_is_raised(self):
        out = msr.suggest(data(incidents=[incident(1, sent=False)]))
        assert any("제출본" in a["text"] for a in out)

    def test_sent_incident_is_not_raised(self):
        out = msr.suggest(data(incidents=[incident(1, sent=True)]))
        assert not any("제출본" in a["text"] for a in out)

    def test_open_work_is_raised(self):
        out = msr.suggest(data(works=[work(1, closed=False)]))
        assert any("증적" in a["text"] for a in out)

    def test_alarm_without_runbook_is_raised(self):
        top = [{"fingerprint": "a", "times": 9, "sample": "디스크 95%",
                "severity": "error", "has_runbook": False}]
        out = msr.suggest(data(top_alarms=top))
        assert any("절차" in a["text"] for a in out)

    def test_alarm_with_runbook_is_not_raised(self):
        top = [{"fingerprint": "a", "times": 9, "sample": "디스크 95%",
                "severity": "error", "has_runbook": True}]
        out = msr.suggest(data(top_alarms=top))
        assert not any("절차" in a["text"] for a in out)

    def test_critical_compliance_is_raised(self):
        comp = {"by_severity": {"critical": 2, "high": 0, "medium": 0, "low": 0},
                "total": 2, "excused": 0, "worst": [],
                "checked_at": START}
        out = msr.suggest(data(compliance=comp))
        assert any(a["kind"] == "보안" for a in out)

    def test_missed_sla_is_raised(self):
        sla = {"by_severity": [
            {"severity": "critical", "tracked": True, "met": False, "unanswered": 0},
        ]}
        out = msr.suggest(data(sla=sla))
        assert any("목표 미달" in a["text"] for a in out)

    def test_untracked_severity_is_not_a_miss(self):
        """목표 없음을 미달로 세면 지표가 거짓말을 한다."""
        sla = {"by_severity": [
            {"severity": "info", "tracked": False, "met": False, "unanswered": 0},
        ]}
        out = msr.suggest(data(sla=sla))
        assert not any("목표 미달" in a["text"] for a in out)

    def test_empty_month_still_says_something(self):
        """빈 칸을 앞에 두면 결국 아무도 안 쓴다."""
        out = msr.suggest(data())
        assert out and out[0]["text"]


class TestDeck:
    def _prs(self, d):
        from io import BytesIO
        return pptx.Presentation(BytesIO(build(d).getvalue()))

    def test_builds(self):
        assert self._prs(data()).slides

    def test_slide_count_is_stable(self):
        """자료가 비어도 장 수는 같아야 한다. 달마다 목차가 달라지면
        지난달 것과 나란히 놓고 보기 어렵다."""
        empty = len(self._prs(data()).slides)
        full = len(self._prs(data(
            incidents=[incident(1)], works=[work(1)],
            top_alarms=[{"fingerprint": "a", "times": 3, "sample": "x",
                         "severity": "error", "has_runbook": False}],
        )).slides)
        assert empty == full

    def test_customer_name_on_the_cover(self):
        texts = " ".join(
            sh.text_frame.text for sh in self._prs(data()).slides[0].shapes
            if sh.has_text_frame
        )
        assert "가고객" in texts and "2026년 8월" in texts

    def test_nothing_runs_off_the_slide(self):
        """표가 슬라이드 밖으로 나가도 예외는 안 난다. 인쇄물에서만 사라진다.

        장애 30건 같은 나쁜 달을 넣어서, 줄 수가 늘어도 안 넘치는지 본다.
        """
        comp = {
            "by_severity": {"critical": 9, "high": 9, "medium": 3, "low": 1},
            "total": 22, "excused": 2, "checked_at": START,
            "worst": [{"severity": "critical", "title": "관리 포트가 인터넷에 열려 있음",
                       "resource_id": f"sg-{i}", "standard": "CIS AWS 5.2"}
                      for i in range(20)],
        }
        top = [{"fingerprint": f"f{i}", "times": 30 - i,
                "sample": "결제 API 응답 지연 3000ms 감지", "severity": "error",
                "has_runbook": False} for i in range(20)]
        prs = self._prs(data(
            incidents=[incident(i) for i in range(1, 31)],
            works=[work(i, closed=False) for i in range(1, 31)],
            compliance=comp, top_alarms=top,
        ))
        W, H = prs.slide_width, prs.slide_height
        over = []
        for n, s in enumerate(prs.slides, 1):
            for sh in s.shapes:
                if sh.left + sh.width > W or sh.top + sh.height > H:
                    over.append((n, sh.shape_type))
        assert not over, f"슬라이드 밖으로 나간 도형: {over}"

    def test_truncation_is_disclosed(self):
        """잘라냈으면 잘랐다고 적어야 한다. 말없이 빠지면 보고서가 거짓말이 된다."""
        prs = self._prs(data(incidents=[incident(i) for i in range(1, 31)]))
        texts = " ".join(
            sh.text_frame.text for s in prs.slides for sh in s.shapes
            if sh.has_text_frame
        )
        assert "외 " in texts and "건만 실었습니다" in texts

    def test_caveats_slide_is_always_there(self):
        """이 숫자가 무엇을 말하지 않는지 적지 않으면
        읽는 사람이 실제보다 넓게 해석한다."""
        prs = self._prs(data())
        last = " ".join(
            sh.text_frame.text for sh in prs.slides[-1].shapes if sh.has_text_frame
        )
        assert "말하지 않는 것" in last
        assert "UTC" in last

    def test_notes_reach_the_deck(self):
        d = data(notes=["등록된 계정이 없어 알람을 집계하지 못했습니다."])
        texts = " ".join(
            sh.text_frame.text for s in self._prs(d).slides for sh in s.shapes
            if sh.has_text_frame
        )
        assert "집계하지 못했습니다" in texts


@pytest.mark.db
class TestCollect:
    def test_runs_for_every_customer(self, db_app, db_uri):
        from app.customer import names

        with db_app.app_context():
            months = msr.available_months()
            if not months:
                pytest.skip("이벤트가 없습니다")
            m = months[0]
            for name in names():
                d = msr.collect(name, m["year"], m["month"])
                assert d["label"] == f"{m['year']}년 {m['month']}월"
                assert d["actions"]
                build(d)          # 슬라이드까지 만들어져야 끝난 것이다

    def test_unknown_customer_does_not_crash(self, db_app, db_uri):
        with db_app.app_context():
            d = msr.collect("있을리 없는 고객사", 2026, 8)
            assert d["accounts"] == []
            assert d["alarms"]["total"] == 0
            assert any("계정이 없어" in n for n in d["notes"])
