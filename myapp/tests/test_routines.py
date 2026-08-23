# tests/test_routines.py
# 고객사에 약속한 주기 업무.
#
# jobs.py 와 헷갈리면 안 된다. 저쪽은 '우리 cron 이 살아 있나' 이고
# 이쪽은 '고객사에 약속한 일을 했나' 다.
#
# 판정을 순수 함수로 둔 덕분에 DB 없이 시간만 옮겨가며 확인할 수 있다.

from datetime import datetime, timedelta, timezone

import pytest

from app import routines
from app.routines import RoutineError, judge

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def routine(interval_days=30, active=True):
    return {"interval_days": interval_days, "active": active}


class TestJudge:
    def test_never_done(self):
        state, detail = judge(routine(), None, NOW)
        assert state == "never"
        assert "30일 주기" in detail

    def test_just_done_is_ok(self):
        state, _ = judge(routine(), NOW - timedelta(days=1), NOW)
        assert state == "ok"

    def test_overdue(self):
        state, detail = judge(routine(), NOW - timedelta(days=35), NOW)
        assert state == "overdue"
        assert "5일 지났습니다" in detail

    def test_exactly_due_is_overdue(self):
        """오늘이 기한이면 아직 정상이 아니다. 오늘 해야 한다."""
        state, detail = judge(routine(), NOW - timedelta(days=30), NOW)
        assert state == "overdue"
        assert "오늘이 기한" in detail

    def test_soon_scales_with_the_interval(self):
        """연간 점검을 7일 전에 알려주면 늦다. 주기에 비례시킨다."""
        # 월간(30일): 6일 전부터 '곧 도래'
        assert judge(routine(30), NOW - timedelta(days=25), NOW)[0] == "soon"
        assert judge(routine(30), NOW - timedelta(days=23), NOW)[0] == "ok"
        # 연간(365일): 73일 전부터 '곧 도래'
        assert judge(routine(365), NOW - timedelta(days=300), NOW)[0] == "soon"
        assert judge(routine(365), NOW - timedelta(days=200), NOW)[0] == "ok"

    def test_paused_never_alerts(self):
        """멈춘 항목이 계속 빨갛게 남으면 진짜 미이행이 묻힌다."""
        state, _ = judge(routine(active=False), None, NOW)
        assert state == "paused"
        state, _ = judge(routine(active=False), NOW - timedelta(days=999), NOW)
        assert state == "paused"

    def test_states_are_all_labelled(self):
        for state in ("never", "overdue", "soon", "ok", "paused"):
            assert state in routines.STATE_LABEL

    def test_alert_states_are_real_states(self):
        assert set(routines.ALERT_STATES) <= set(routines.STATE_LABEL)


class TestSummary:
    def test_counts(self):
        items = [{"state": "overdue"}, {"state": "overdue"},
                 {"state": "ok"}, {"state": "never"}]
        got = routines.summary(items)
        assert got["total"] == 4
        assert got["overdue"] == 2
        assert got["alert"] == 3       # overdue 2 + never 1
        assert got["soon"] == 0        # 없는 상태도 0 으로 채운다


class TestValidation:
    """DB 없이 걸러지는 것들."""

    def test_customer_required(self):
        with pytest.raises(RoutineError):
            routines.add("", "점검", 30)

    def test_name_required(self):
        with pytest.raises(RoutineError):
            routines.add("가고객", "  ", 30)

    def test_interval_must_be_a_number(self):
        with pytest.raises(RoutineError) as e:
            routines.add("가고객", "점검", "한달")
        assert "숫자" in str(e.value)

    def test_interval_must_be_positive(self):
        with pytest.raises(RoutineError):
            routines.add("가고객", "점검", 0)


@pytest.mark.db
class TestStore:
    @pytest.fixture
    def one(self, db_app, db_uri):
        import psycopg

        with db_app.app_context():
            rid = routines.add("시험고객", "월간 점검", 30, why="증설 판단")
        yield rid
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM routine_runs WHERE customer = %s", ("시험고객",))
            cur.execute("DELETE FROM customer_routines WHERE customer = %s",
                        ("시험고객",))

    def test_listing_starts_at_never(self, db_app, one):
        with db_app.app_context():
            items = [i for i in routines.listing("시험고객")]
        assert items[0]["state"] == "never"
        assert items[0]["why"] == "증설 판단"

    def test_mark_done_moves_to_ok(self, db_app, one):
        with db_app.app_context():
            routines.mark_done(one, "김당직", "여유 40%")
            item = routines.listing("시험고객")[0]
        assert item["state"] == "ok"
        assert item["last_done_by"] == "김당직"
        assert item["last_note"] == "여유 40%"

    def test_add_twice_updates(self, db_app, one):
        """같은 고객사에 같은 이름이면 항목이 둘이 되지 않는다."""
        with db_app.app_context():
            routines.add("시험고객", "월간 점검", 90)
            items = routines.listing("시험고객")
        assert len(items) == 1
        assert items[0]["interval_days"] == 90

    def test_pause_and_resume(self, db_app, one):
        with db_app.app_context():
            routines.set_active(one, False)
            assert routines.listing("시험고객")[0]["state"] == "paused"
            routines.set_active(one, True)
            assert routines.listing("시험고객")[0]["state"] == "never"

    def test_history_survives_the_promise(self, db_app, db_uri, one):
        """약속을 지워도 '그때 했다' 는 남는다."""
        import psycopg

        with db_app.app_context():
            routines.mark_done(one, "김당직")
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM customer_routines WHERE id = %s", (one,))
            cur.execute(
                "SELECT routine_id, customer, name FROM routine_runs "
                " WHERE customer = %s", ("시험고객",))
            rows = cur.fetchall()
        assert rows and rows[0][0] is None          # ON DELETE SET NULL
        assert rows[0][1:] == ("시험고객", "월간 점검")

    def test_unknown_routine(self, db_app, db_uri):
        with db_app.app_context():
            with pytest.raises(RoutineError):
                routines.mark_done(-1, "나")


@pytest.mark.db
class TestRoutes:
    def test_page(self, db_client):
        assert db_client.get("/customer/routines").status_code == 200

    def test_filter_by_customer(self, db_client):
        assert db_client.get("/customer/routines?customer=없는고객").status_code == 200

    def test_bad_input_does_not_break_the_page(self, db_client):
        r = db_client.post("/customer/routines/add",
                           data={"customer": "가고객", "name": "", "interval_days": "30"},
                           follow_redirects=True)
        assert r.status_code == 200
        assert "이름을 입력하세요" in r.get_data(as_text=True)
