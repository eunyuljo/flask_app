# tests/test_jobs_health.py
# 배치 실행 상태와 연동 점검.
#
# 판정(_judge)은 순수 함수라 시각을 손으로 만들어 먹인다. 실제로 6시간을
# 기다릴 수는 없고, 기다린다 해도 그건 시간을 테스트하는 것이지 판정을
# 테스트하는 게 아니다.

from datetime import datetime, timedelta, timezone

import pytest

from app import health, jobs

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def run(hours_ago, outcome="ok"):
    return {"started_at": NOW - timedelta(hours=hours_ago),
            "ended_at": NOW - timedelta(hours=hours_ago),
            "outcome": outcome, "summary": "", "detail": ""}


class TestJudge:
    def test_recent_success_is_ok(self):
        state, _ = jobs._judge(run(1), {"hours": 26, "why": "매일"}, NOW, 0)
        assert state == "ok"

    def test_overdue_is_stale(self):
        """cron 이 조용히 죽은 상황. 실패보다 이게 더 무섭다."""
        state, note = jobs._judge(run(72), {"hours": 26, "why": "매일"}, NOW, 0)
        assert state == "stale"
        assert "72시간 전" in note

    def test_never_run(self):
        state, note = jobs._judge(None, {"hours": 26, "why": "매일 한 번"}, NOW, 0)
        assert state == "never"
        assert "매일 한 번" in note

    def test_one_failure(self):
        state, _ = jobs._judge(run(1, "failed"), {"hours": 26, "why": "매일"}, NOW, 1)
        assert state == "failed"

    def test_consecutive_failures_are_different(self):
        """한 번 실패는 흔하다. 연속 실패는 다른 문제다."""
        state, note = jobs._judge(run(1, "failed"), {"hours": 26, "why": "매일"}, NOW, 3)
        assert state == "failing"
        assert "3회" in note

    def test_running_recently_is_fine(self):
        state, _ = jobs._judge(run(1, "running"), None, NOW, 0)
        assert state == "running"

    def test_running_too_long_is_stuck(self):
        """프로세스가 사라지면 outcome 이 running 인 채로 남는다."""
        state, note = jobs._judge(run(jobs.STUCK_HOURS + 2, "running"), None, NOW, 0)
        assert state == "stuck"
        assert "죽었을 수 있습니다" in note

    def test_manual_command_is_never_stale(self):
        """add-account 처럼 필요할 때만 부르는 것은 '안 돌았다' 가 문제가 아니다."""
        state, _ = jobs._judge(run(500), None, NOW, 0)
        assert state == "ok"

    def test_every_state_has_a_label(self):
        for state in jobs.STATE_LABEL:
            assert jobs.STATE_LABEL[state]

    def test_alert_states_exclude_healthy_ones(self):
        assert "ok" not in jobs.ALERT_STATES
        assert "running" not in jobs.ALERT_STATES
        assert set(jobs.ALERT_STATES) <= set(jobs.STATE_LABEL)


class TestExpected:
    def test_expected_jobs_are_real_commands(self, app):
        """없는 명령을 기다리면 영원히 '기록 없음' 으로 남는다."""
        commands = set(app.cli.commands)
        missing = set(jobs.EXPECTED) - commands
        assert not missing, f"EXPECTED 에 있는데 CLI 에 없는 명령: {missing}"

    def test_expected_jobs_have_a_reason(self):
        for job, spec in jobs.EXPECTED.items():
            assert spec["hours"] > 0, job
            assert spec["why"], job


class TestHealthChecks:
    def test_every_check_declares_whether_it_sends(self, app):
        """바깥으로 보내는 점검은 화면에서 미리 알려야 한다."""
        for c in health.CHECKS:
            assert "sends" in c, c["id"]
            assert c["why"], c["id"]

    def test_unknown_check(self):
        assert health.run_check("없는것") is None

    def test_result_shape(self, app):
        """되든 안 되든 같은 모양이어야 화면이 한 갈래로 그린다."""
        with app.app_context():
            r = health.run_check("agent")["result"]
        assert set(r) == {"ok", "detail", "ms"}
        assert isinstance(r["ok"], bool)

    def test_lambda_check_runs_the_real_normalizer(self, app):
        with app.app_context():
            r = health.run_check("lambda")["result"]
        assert r["ok"] and "지문" in r["detail"]

    def test_db_check_fails_without_db(self, app):
        """testing 설정은 sqlite:// 라 붙지 못한다. 그걸 정상으로 보고하면 안 된다."""
        with app.app_context():
            r = health.run_check("db")["result"]
        assert r["ok"] is False

    def test_a_check_that_raises_is_reported_not_propagated(self, app, monkeypatch):
        """점검 하나가 터졌다고 화면 전체가 500 이 되면 안 된다."""
        monkeypatch.setitem(
            health.CHECKS_BY_ID["agent"], "fn",
            lambda: (_ for _ in ()).throw(RuntimeError("일부러")),
        )
        with app.app_context():
            r = health.run_check("agent")["result"]
        assert r["ok"] is False and "일부러" in r["detail"]

    def test_jira_check_does_not_create_an_issue(self, app, monkeypatch):
        """점검 때문에 티켓이 쌓이면 아무도 이 버튼을 안 누른다."""
        from app import jira

        called = []
        monkeypatch.setattr(jira, "create_issue",
                            lambda *a, **k: called.append(1))
        with app.app_context():
            health.run_check("jira")
        assert not called


@pytest.mark.db
class TestRecording:
    @pytest.fixture
    def clean(self, db_app, db_uri):
        import psycopg

        def purge():
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM job_runs WHERE job LIKE 'test-%'")

        purge()
        yield
        purge()

    def test_start_and_finish(self, db_app, clean):
        with db_app.app_context():
            run_id = jobs.start("test-job")
            assert run_id
            jobs.finish(run_id, "ok", summary="끝남")
            rows = jobs.recent("test-job", limit=1)
            assert rows[0]["outcome"] == "ok"
            assert rows[0]["ended_at"] is not None

    def test_finish_with_none_is_a_noop(self, db_app, clean):
        """기록을 시작하지 못했어도 배치는 끝까지 돌아야 한다."""
        with db_app.app_context():
            jobs.finish(None, "ok")      # 예외가 나면 안 된다

    def test_failure_is_recorded(self, db_app, clean):
        with db_app.app_context():
            run_id = jobs.start("test-job")
            jobs.finish(run_id, "failed", summary="터짐")
            assert jobs.recent("test-job", limit=1)[0]["outcome"] == "failed"

    def test_runs_accumulate(self, db_app, clean):
        """마지막 것만 덮어쓰면 '어제도 실패했나' 를 알 수 없다."""
        with db_app.app_context():
            for _ in range(3):
                jobs.finish(jobs.start("test-job"), "ok")
            assert len(jobs.recent("test-job")) == 3

    def test_status_includes_never_run_commands(self, db_app, db_uri):
        """기록이 없는 것과 그런 명령이 없는 것은 다르다."""
        with db_app.app_context():
            names = {s["job"] for s in jobs.status()}
        assert set(jobs.EXPECTED) <= names


@pytest.mark.db
class TestCliRecords:
    def test_a_tracked_command_leaves_a_record(self, db_app, db_uri):
        """데코레이터가 실제로 붙어 있는지."""
        with db_app.app_context():
            before = jobs.recent("compliance-check", limit=1)
        runner = db_app.test_cli_runner()
        runner.invoke(args=["compliance-check", "--severity", "critical"])
        with db_app.app_context():
            after = jobs.recent("compliance-check", limit=1)
        assert after and (not before or after[0]["id"] != before[0]["id"])

    def test_failure_inside_a_command_is_recorded_and_reraised(self, db_app, db_uri,
                                                              monkeypatch):
        """기록 때문에 실패를 삼키면 cron 이 성공한 줄 안다."""
        from app import compliance

        def boom(*a, **k):
            raise RuntimeError("일부러 터뜨림")

        monkeypatch.setattr(compliance, "latest_snapshots", boom)
        result = db_app.test_cli_runner().invoke(args=["compliance-check"])
        assert result.exit_code != 0
        with db_app.app_context():
            assert jobs.recent("compliance-check", limit=1)[0]["outcome"] == "failed"


@pytest.mark.db
class TestRoutes:
    def test_page_opens(self, db_client):
        assert db_client.get("/admin/health").status_code == 200

    def test_check_runs_and_redirects(self, db_client):
        r = db_client.post("/admin/health/check/db")
        assert r.status_code == 302

    def test_result_shows_once_then_clears(self, db_client):
        """Slack 에 메시지를 보내는 일이라, 새로고침이 재전송처럼 보이면 안 된다."""
        db_client.post("/admin/health/check/lambda")
        first = db_client.get("/admin/health").get_data(as_text=True)
        second = db_client.get("/admin/health").get_data(as_text=True)
        assert "지문" in first
        assert "지문" not in second

    def test_operator_cannot_reach_it(self, db_app, db_uri):
        c = db_app.test_client()
        with c.session_transaction() as s:
            s["username"] = "운영자"
            s["role"] = "operator"
        assert c.get("/admin/health").status_code == 403
