# tests/test_approval.py
# 변경 승인과 작업창.
#
# work_orders 는 open 에서 시작했다 - 만들어지자마자 작업해도 되는
# 상태다. 고객 승인 없이 프로덕션을 건드리는 MSP 는 없는데 그 앞 단계가
# 통째로 비어 있었고, 그래서 "승인 없이 실행된 작업" 을 찾을 수 없었다.

from datetime import datetime, timedelta, timezone

import pytest

from app import work

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


class TestWindow:
    """작업창. 벗어나도 막지 않고 남기기만 한다."""

    def _item(self, start=None, end=None):
        return {"window_start": start, "window_end": end}

    def test_no_window(self):
        state = work.window_state(self._item(), now=NOW)
        assert state["has_window"] is False

    def test_inside(self):
        state = work.window_state(
            self._item(NOW - timedelta(hours=1), NOW + timedelta(hours=1)), now=NOW
        )
        assert state["inside"] is True

    def test_too_early(self):
        state = work.window_state(
            self._item(NOW + timedelta(hours=1), NOW + timedelta(hours=2)), now=NOW
        )
        assert state["inside"] is False
        assert "아직 이릅니다" in state["note"]

    def test_too_late(self):
        state = work.window_state(
            self._item(NOW - timedelta(hours=3), NOW - timedelta(hours=1)), now=NOW
        )
        assert state["inside"] is False
        assert "끝났습니다" in state["note"]


class TestLabels:
    def test_every_flow_state_has_a_label(self):
        for state in work.FLOW:
            assert state in work.STATUS_LABEL

    def test_rejected_has_a_label(self):
        assert "rejected" in work.STATUS_LABEL

    def test_open_keeps_its_meaning(self):
        """이미 쌓인 기록이 전부 open 이다. 뜻을 바꾸면 지난 작업이
        미승인으로 보인다."""
        assert "open" in work.FLOW
        assert work.FLOW.index("requested") < work.FLOW.index("open")


@pytest.mark.db
class TestApproval:
    @pytest.fixture
    def wid(self, db_app, db_uri):
        """테스트용 작업 하나. 끝나면 지운다."""
        import psycopg

        with db_app.app_context():
            work_id = work.create(
                "테스트 작업", "가고객", "123456789012", "ap-northeast-2",
                operator="요청자", requested_by="요청자", rollback="되돌리는 법",
            )
        yield work_id
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM work_orders WHERE id = %s", (work_id,))

    def test_starts_as_requested(self, db_app, wid):
        with db_app.app_context():
            assert work.get(wid)["status"] == "requested"

    def test_self_approval_is_blocked(self, db_app, wid):
        """승인이 형식만 남으면 없는 것과 같다. 이 규칙 하나가 나머지를 지탱한다."""
        with db_app.app_context():
            with pytest.raises(work.WorkError) as e:
                work.approve(wid, "요청자")
            assert "자기가 낸 요청" in str(e.value)
            assert work.get(wid)["status"] == "requested"

    def test_another_person_can_approve(self, db_app, wid):
        with db_app.app_context():
            work.approve(wid, "승인자", note="확인함")
            item = work.get(wid)
            assert item["status"] == "open"
            assert item["approved_by"] == "승인자"
            assert item["approved_at"] is not None

    def test_double_approval_is_blocked(self, db_app, wid):
        with db_app.app_context():
            work.approve(wid, "승인자")
            with pytest.raises(work.WorkError) as e:
                work.approve(wid, "다른승인자")
            assert "상태가 아닙니다" in str(e.value)

    def test_reject_needs_a_reason(self, db_app, wid):
        """사유가 없으면 요청자가 무엇을 고쳐야 할지 모른다."""
        with db_app.app_context():
            with pytest.raises(work.WorkError):
                work.reject(wid, "승인자", note="  ")

    def test_reject(self, db_app, wid):
        with db_app.app_context():
            work.reject(wid, "승인자", note="되돌리는 방법이 부실합니다")
            item = work.get(wid)
            assert item["status"] == "rejected"
            assert "부실" in item["decided_note"]

    def test_cannot_snapshot_before_approval(self, db_app, wid):
        """승인 전에 스냅샷을 찍을 수 있으면 승인 단계가 장식이 된다."""
        with db_app.app_context():
            with pytest.raises(work.WorkError):
                work.check_transition(work.get(wid), "before")

    def test_can_snapshot_after_approval(self, db_app, wid):
        with db_app.app_context():
            work.approve(wid, "승인자")
            work.check_transition(work.get(wid), "before")   # 예외가 나면 안 된다

    def test_rejected_work_cannot_proceed(self, db_app, wid):
        with db_app.app_context():
            work.reject(wid, "승인자", note="안 됨")
            with pytest.raises(work.WorkError):
                work.check_transition(work.get(wid), "before")

    def test_pending_lists_only_requested(self, db_app, wid):
        with db_app.app_context():
            assert wid in [w["id"] for w in work.pending()]
            work.approve(wid, "승인자")
            assert wid not in [w["id"] for w in work.pending()]

    def test_backwards_window_is_rejected(self, db_app, db_uri):
        with db_app.app_context():
            with pytest.raises(work.WorkError):
                work.create("거꾸로", "가고객", "123456789012", "ap-northeast-2",
                            operator="누구",
                            window_start=NOW, window_end=NOW - timedelta(hours=1))

    def test_out_of_window_marks_but_does_not_block(self, db_app, wid):
        """막으면 급할 때 이 도구를 통째로 우회하고, 그러면 증적이 아예 안 남는다."""
        with db_app.app_context():
            work.approve(wid, "승인자")
            work.mark_out_of_window(wid)
            item = work.get(wid)
            assert item["out_of_window"] is True
            work.check_transition(item, "before")   # 그래도 진행은 된다


@pytest.mark.db
class TestRoutes:
    @pytest.fixture
    def wid(self, db_app, db_uri):
        import psycopg

        with db_app.app_context():
            work_id = work.create("라우트 테스트", "가고객", "123456789012",
                                  "ap-northeast-2", operator="admin",
                                  requested_by="admin")
        yield work_id
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM work_orders WHERE id = %s", (work_id,))

    def _as(self, db_app, username, role):
        c = db_app.test_client()
        with c.session_transaction() as s:
            s["username"] = username
            s["role"] = role
        return c

    def test_operator_cannot_approve(self, db_app, wid):
        """메뉴를 숨기는 것이 아니라 라우트에서 막는다."""
        c = self._as(db_app, "운영자", "operator")
        body = c.post(f"/work/{wid}/approve", follow_redirects=True).get_data(as_text=True)
        assert "관리자만" in body
        with db_app.app_context():
            assert work.get(wid)["status"] == "requested"

    def test_admin_can_approve_someone_elses(self, db_app, wid):
        c = self._as(db_app, "다른관리자", "admin")
        c.post(f"/work/{wid}/approve", data={"note": "ok"})
        with db_app.app_context():
            assert work.get(wid)["status"] == "open"

    def test_admin_cannot_approve_own(self, db_app, wid):
        c = self._as(db_app, "admin", "admin")     # wid 의 requested_by 가 admin
        body = c.post(f"/work/{wid}/approve", follow_redirects=True).get_data(as_text=True)
        assert "자기가 낸 요청" in body

    def test_approval_is_audited(self, db_app, wid):
        from app import audit

        c = self._as(db_app, "감사용관리자", "admin")
        c.post(f"/work/{wid}/approve", data={"note": "확인"})
        with db_app.app_context():
            rows = [a for a in audit.recent(20)
                    if a["action"] == "work_approval" and str(wid) in a["summary"]]
        assert rows
