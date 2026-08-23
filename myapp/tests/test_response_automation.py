# tests/test_response_automation.py
# 대응 흐름의 자동화 세 가지.
#
#   1. 에스컬레이션 메시지에 런북을 싣는다
#   2. 흐름이 멈춘 작업을 찾는다
#   3. 작업창이 열린 작업의 '작업 전' 스냅샷을 자동으로 찍는다
#
# 셋 다 사람의 판단을 대신하지 않는다. 승인과 증적 확정은 그대로 사람이
# 하고, 자동 재조치도 하지 않는다. 여기서 자동화하는 것은 기계가 할 일
# (수집기 돌리기, 목록 훑기)과 전달(절차를 메시지에 싣기)뿐이다.

from datetime import datetime, timedelta, timezone

import pytest

from app import escalation, work

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# 1) 에스컬레이션에 런북
# ----------------------------------------------------------------------

def breach(**over):
    base = {"customer": "가고객", "severity": "critical", "sample": "커넥션 풀 150%",
            "count": 12, "source": "pay-api", "account_id": "111122223333",
            "minutes": 30, "elapsed_minutes": 74, "fingerprint": "abc123"}
    base.update(over)
    return base


def book(**over):
    base = {"id": 7, "title": "커넥션 풀 고갈", "customer": "가고객",
            "body": "1. RDS 커넥션 수 확인\n2. 풀 설정 확인"}
    base.update(over)
    return base


class TestEscalationCarriesRunbook:
    def test_runbook_body_is_included(self, app):
        """새벽에 불려 나온 사람이 절차를 보려고 로그인해야 하면 안 된다."""
        with app.app_context():
            text = escalation.to_slack(breach(), 1, [], book())
        assert "RDS 커넥션 수 확인" in text
        assert "커넥션 풀 고갈" in text

    def test_scope_is_shown(self, app):
        with app.app_context():
            assert "가고객 전용" in escalation.to_slack(breach(), 1, [], book())
            assert "(공통)" in escalation.to_slack(
                breach(), 1, [], book(customer=""))

    def test_absence_is_stated_not_silent(self, app):
        """'어딘가에 있겠지' 하고 찾아 헤매는 것보다 없다고 알려주는 편이 낫다."""
        with app.app_context():
            text = escalation.to_slack(breach(), 1, [])
        assert "등록된 대응 절차가 없습니다" in text

    def test_long_runbook_is_clipped(self, app):
        """런북 전문을 보내면 채널이 글로 막히고 누가 불려 나왔는지가 밀려 올라간다."""
        with app.app_context():
            text = escalation.to_slack(
                breach(), 1, [], book(body="가" * 3000))
        assert len(text) < 3000
        assert "이어집니다" in text

    def test_short_runbook_is_not_clipped(self, app):
        with app.app_context():
            text = escalation.to_slack(breach(), 1, [], book())
        assert "이어집니다" not in text

    def test_link_only_when_base_url_given(self, app):
        with app.app_context():
            assert "|절차 전체 보기>" in escalation.to_slack(
                breach(), 1, [], book(), "https://ops.example.com")
            assert "절차 전체 보기" not in escalation.to_slack(
                breach(), 1, [], book())

    def test_who_was_called_stays_at_the_top(self, app):
        """절차가 길어도 호출된 사람이 맨 위에 있어야 한다."""
        with app.app_context():
            text = escalation.to_slack(
                breach(), 1, [{"name": "김당직", "slack_id": "U01", "level": 1}],
                book(body="가" * 500))
        assert text.index("U01") < text.index("대응 절차")

    def test_jira_keeps_the_whole_procedure(self, app):
        """Jira 는 길이 제한이 빡빡하지 않고 나중에 열어볼 수도 있다."""
        with app.app_context():
            _, description = escalation.to_jira(breach(), 2, book(body="가" * 3000))
        assert "가" * 3000 in description

    def test_jira_without_runbook(self, app):
        with app.app_context():
            _, description = escalation.to_jira(breach(), 2)
        assert "등록된 대응 절차가 없습니다" in description


# ----------------------------------------------------------------------
# 2) 멈춘 작업
# ----------------------------------------------------------------------

def order(status="open", hours_ago=100, **over):
    """stalled() 가 돌려주는 것과 같은 모양. 어긋나면 화면과 알림이 터진다."""
    base = {"id": 1, "title": "작업", "customer": "가고객", "operator": "나",
            "ticket": "", "status": status,
            "last_move": NOW - timedelta(hours=hours_ago),
            "stale_hours": hours_ago,
            "limit_hours": work.STALE_HOURS[status],
            "why": work.STALE_WHY[status]}
    base.update(over)
    return base


class TestStaleThresholds:
    def test_evidence_at_risk_has_the_tightest_limit(self):
        """작업 전만 찍힌 상태가 가장 위험하다. 가장 짧게 잡는다."""
        assert work.STALE_HOURS["before_taken"] == min(work.STALE_HOURS.values())

    def test_riskiest_sorts_first(self):
        assert work.STALE_ORDER[0] == "before_taken"

    def test_every_state_explains_itself(self):
        for state in work.STALE_HOURS:
            assert state in work.STALE_WHY
            assert state in work.STALE_ORDER

    def test_closed_and_rejected_are_not_watched(self):
        """끝난 것을 멈췄다고 하면 목록이 영원히 안 비워진다."""
        assert "closed" not in work.STALE_HOURS
        assert "rejected" not in work.STALE_HOURS

    def test_every_watched_state_is_a_real_state(self):
        assert set(work.STALE_HOURS) <= set(work.FLOW) | {"requested"}


class TestStalledSummary:
    def test_counts(self):
        rows = [order("before_taken"), order("open"), order("open")]
        got = work.stalled_summary(rows)
        assert got["total"] == 3
        assert got["by_status"]["open"] == 2
        assert got["evidence_at_risk"] == 1

    def test_empty(self):
        got = work.stalled_summary([])
        assert got["total"] == 0
        assert got["evidence_at_risk"] == 0


class TestStalledSlack:
    def test_evidence_warning_is_up_front(self):
        text = work.to_slack_stalled([order("before_taken", 20)])
        assert "증적이 반쪽" in text

    def test_no_warning_when_nothing_is_at_risk(self):
        text = work.to_slack_stalled([order("requested", 30)])
        assert "증적이 반쪽" not in text

    def test_long_list_is_capped(self):
        """채널에 스무 줄을 쏟으면 아무도 안 읽는다."""
        text = work.to_slack_stalled([order("open", 100) for _ in range(25)])
        assert "외 15건" in text

    def test_empty_is_empty_string(self):
        assert work.to_slack_stalled([]) == ""


@pytest.mark.db
class TestStalledQuery:
    @pytest.fixture
    def seeded(self, db_app, db_uri):
        """네 상태를 각각 오래된 시각으로 심는다."""
        import psycopg

        now = datetime.now(timezone.utc)
        rows = [
            ("requested", "승인 대기", now - timedelta(hours=30), None),
            ("open", "시작 안 함", now - timedelta(days=5), None),
            ("before_taken", "전만 찍힘", now - timedelta(hours=20), "before"),
            ("after_taken", "확정 안 함", now - timedelta(days=3), "after"),
            ("open", "방금 승인", now, None),
        ]
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            for status, title, when, phase in rows:
                snap = None
                if phase:
                    cur.execute(
                        "INSERT INTO resource_snapshots (account_id, region, source,"
                        " complete, collected_at) VALUES ('999000999000','ap-northeast-2',"
                        " 'demo', true, %s) RETURNING snapshot_id", (when,))
                    snap = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO work_orders (title, customer, account_id, region,"
                    " operator, status, created_at, approved_at, before_snapshot_id,"
                    " after_snapshot_id) VALUES (%s,'시험멈춤','999000999000',"
                    " 'ap-northeast-2','시험',%s,%s,%s,%s,%s)",
                    (title, status, when,
                     when if status != "requested" else None,
                     snap, snap if phase == "after" else None))
        yield "시험멈춤"
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM work_orders WHERE customer = %s", ("시험멈춤",))
            cur.execute("DELETE FROM resource_snapshots WHERE account_id = %s",
                        ("999000999000",))

    def mine(self, db_app, customer):
        with db_app.app_context():
            return [r for r in work.stalled() if r["customer"] == customer]

    def test_finds_the_stalled_not_the_fresh(self, db_app, seeded):
        got = self.mine(db_app, seeded)
        assert {r["title"] for r in got} == {"승인 대기", "시작 안 함",
                                             "전만 찍힘", "확정 안 함"}

    def test_riskiest_first(self, db_app, seeded):
        assert self.mine(db_app, seeded)[0]["status"] == "before_taken"

    def test_uses_snapshot_time_not_creation_time(self, db_app, seeded):
        """상태가 바뀐 시각 칼럼은 없다. 스냅샷의 collected_at 을 쓴다."""
        got = {r["title"]: r for r in self.mine(db_app, seeded)}
        # 20시간 전에 찍힌 스냅샷 기준이어야 한다
        assert 19 <= got["전만 찍힘"]["stale_hours"] <= 21

    def test_each_row_carries_its_limit(self, db_app, seeded):
        for row in self.mine(db_app, seeded):
            assert row["limit_hours"] == work.STALE_HOURS[row["status"]]
            assert row["why"]


# ----------------------------------------------------------------------
# 3) 작업창 자동 스냅샷
# ----------------------------------------------------------------------

@pytest.mark.db
class TestAutoSnapshot:
    @pytest.fixture
    def windows(self, db_app, db_uri):
        import psycopg

        now = datetime.now(timezone.utc)
        rows = [
            ("창 안", now - timedelta(hours=1), now + timedelta(hours=1)),
            ("방금 시작", now - timedelta(minutes=5), now + timedelta(hours=1)),
            ("아직 이름", now + timedelta(hours=2), now + timedelta(hours=3)),
            ("이미 끝남", now - timedelta(days=1), now - timedelta(hours=20)),
        ]
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            for title, ws, we in rows:
                cur.execute(
                    "INSERT INTO work_orders (title, customer, account_id, region,"
                    " operator, status, approved_at, window_start, window_end)"
                    " VALUES (%s,'시험자동','999000999000','ap-northeast-2','시험',"
                    " 'open', now(), %s, %s)", (title, ws, we))
        yield "시험자동"
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM work_orders WHERE customer = %s", ("시험자동",))

    def due(self, db_app, customer):
        with db_app.app_context():
            return [r["title"] for r in work.due_for_auto_snapshot()
                    if r["customer"] == customer]

    def test_only_the_open_window_past_grace(self, db_app, windows):
        assert self.due(db_app, windows) == ["창 안"]

    def test_grace_waits_for_the_person(self, db_app, windows):
        """사람이 직접 찍으러 오는 중일 수 있다. 사람이 찍은 것이 더 정확하다."""
        with db_app.app_context():
            got = [r["title"] for r in work.due_for_auto_snapshot(grace_minutes=1)
                   if r["customer"] == windows]
        assert set(got) == {"창 안", "방금 시작"}

    def test_finished_window_is_left_alone(self, db_app, windows):
        """창이 끝난 뒤 찍은 것은 '작업 전' 이 아니라 '한참 뒤' 다."""
        assert "이미 끝남" not in self.due(db_app, windows)

    def test_work_without_a_window_is_never_automatic(self, db_app, db_uri):
        """창을 안 정한 작업까지 자동으로 찍으면 사람이 모르는 새 상태가 바뀐다."""
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_orders (title, customer, account_id, region,"
                " operator, status, approved_at) VALUES ('창없음','시험창없음',"
                " '999000999000','ap-northeast-2','시험','open', now())")
        try:
            assert self.due(db_app, "시험창없음") == []
        finally:
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM work_orders WHERE customer = %s",
                            ("시험창없음",))


class TestWhatStaysManual:
    """자동화하지 않은 것. 이 목록이 자동 재조치로 넘어갈 때의 출발선이다."""

    def test_after_snapshot_is_never_automatic(self):
        """작업이 끝났는지 기계는 알 수 없다. 도중 상태를 '작업 후' 로
        남기면 그건 증적이 아니라 잘못된 증적이다.

        질의가 status='open' 인 것만 고르므로 after 는 구조적으로 불가능하다.
        (before_taken 을 고르지 않는 한 '작업 후' 로 넘어갈 대상이 없다.)
        """
        import inspect

        source = inspect.getsource(work.due_for_auto_snapshot)
        assert "status = 'open'" in source
        assert "before_snapshot_id IS NULL" in source
        assert "before_taken" not in source

    def test_approval_is_never_automatic(self):
        """자기 승인 금지가 이 흐름의 핵심이다."""
        import inspect

        from app import cli

        source = inspect.getsource(cli)
        assert "work.approve(" not in source

    def test_no_remediation_command_exists(self, app):
        """자동 재조치는 판단 재료가 쌓이고 합의된 뒤의 일이다."""
        names = set(app.cli.commands)
        for banned in ("restart", "remediate", "auto-fix", "reboot"):
            assert not any(banned in n for n in names), banned
