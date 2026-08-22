# tests/test_escalation.py
# 에스컬레이션 단계 판정과 Jira 넘기기 테스트.
#
# 에스컬레이션은 '사람을 부르는' 기능이라 잘못되면 두 방향으로 사고가 난다.
#   너무 안 부름 : 아무도 모르는 채 장애가 이어진다
#   너무 자주 부름 : 다들 무시하게 된다
# 그래서 단계 판정과 중복 억제를 특히 확인한다.

import http.server
import json
import threading

import pytest

from app.escalation import due_level, mention, to_jira


class TestDueLevel:
    STEPS = [0, 30, 120]

    @pytest.mark.parametrize("elapsed,expected", [
        (5, 0),      # 목표(15) 전
        (14, 0),
        (15, 1),     # 목표 도달 = 1단계
        (44, 1),
        (45, 2),     # 목표 + 30
        (134, 2),
        (135, 3),    # 목표 + 120
        (999, 3),    # 더 가도 마지막 단계에서 멈춘다
    ])
    def test_levels(self, elapsed, expected):
        assert due_level(elapsed, 15, self.STEPS) == expected

    def test_never_below_target(self):
        """목표 전에는 절대 부르지 않는다."""
        for elapsed in range(0, 15):
            assert due_level(elapsed, 15, self.STEPS) == 0

    def test_single_step_policy(self):
        assert due_level(100, 15, [0]) == 1

    def test_empty_steps_never_escalates(self):
        assert due_level(9999, 15, []) == 0

    def test_target_zero_escalates_immediately(self):
        """목표 0분은 SLA 집계에서 빠지지만, 판정 함수 자체는 방어적이어야 한다."""
        assert due_level(1, 0, self.STEPS) == 1


class TestMention:
    def test_uses_slack_id(self):
        """@이름 은 멘션이 걸리지 않는다. Slack 은 <@ID> 만 알아본다."""
        assert mention({"slack_id": "U01ABC", "name": "김운영"}) == "<@U01ABC>"

    def test_falls_back_to_name(self):
        assert mention({"slack_id": "", "name": "김운영"}) == "김운영"


class TestJiraPayload:
    ITEM = {
        "customer": "A커머스", "account_id": "123456789012", "source": "pay-api",
        "severity": "critical", "sample": "결제 API 응답 지연 2400ms 감지",
        "count": 8, "fingerprint": "abc123", "minutes": 15, "elapsed_minutes": 200,
    }

    def test_summary_has_level_and_customer(self):
        summary, _ = to_jira(self.ITEM, 2)
        assert "[SLA 2단계]" in summary
        assert "A커머스" in summary

    def test_summary_is_bounded(self):
        item = dict(self.ITEM, sample="가" * 500)
        summary, _ = to_jira(item, 2)
        assert len(summary) < 200

    def test_description_states_the_limitation(self):
        """대응 여부가 감사 로그 기준이라는 사실이 티켓에도 남아야 한다."""
        _, description = to_jira(self.ITEM, 2)
        assert "감사 로그" in description
        assert self.ITEM["fingerprint"] in description


class _JiraHandler(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        payload = json.loads(body)
        # 프로젝트 키가 BAD 면 Jira 처럼 400 + 이유를 돌려준다.
        if payload["fields"]["project"]["key"] == "BAD":
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "errorMessages": [],
                "errors": {"project": "project is required"},
            }).encode())
            return
        _JiraHandler.received.append({
            "auth": self.headers.get("Authorization", ""),
            "payload": payload,
        })
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"id": "1", "key": "OPS-42"}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_jira(app):
    server = http.server.HTTPServer(("127.0.0.1", 0), _JiraHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _JiraHandler.received = []
    with app.app_context():
        app.config.update(
            JIRA_BASE_URL=f"http://127.0.0.1:{server.server_port}",
            JIRA_EMAIL="ops@example.com",
            JIRA_API_TOKEN="token",
            JIRA_PROJECT_KEY="OPS",
            JIRA_ISSUE_TYPE="Task",
        )
        yield app, _JiraHandler
    server.shutdown()


class TestJiraClient:
    def test_not_configured_names_what_is_missing(self, app):
        from app.jira import create_issue, JiraNotConfigured

        with app.app_context():
            app.config.update(JIRA_BASE_URL="", JIRA_EMAIL="", JIRA_API_TOKEN="",
                              JIRA_PROJECT_KEY="")
            with pytest.raises(JiraNotConfigured) as e:
                create_issue("x", "y")
        assert "JIRA_BASE_URL" in str(e.value)

    def test_not_configured_is_a_jira_error(self):
        from app.jira import JiraError, JiraNotConfigured
        assert issubclass(JiraNotConfigured, JiraError)

    def test_creates_issue_and_returns_key(self, fake_jira):
        from app.jira import create_issue

        application, handler = fake_jira
        with application.app_context():
            key = create_issue("제목", "본문\n둘째 줄", labels=["sla"])
        assert key == "OPS-42"
        sent = handler.received[0]
        assert sent["auth"].startswith("Basic ")
        assert sent["payload"]["fields"]["project"]["key"] == "OPS"
        assert sent["payload"]["fields"]["labels"] == ["sla"]

    def test_description_is_adf_not_plain_text(self, fake_jira):
        """Jira Cloud v3 는 description 을 평문으로 받지 않는다."""
        from app.jira import create_issue

        application, handler = fake_jira
        with application.app_context():
            create_issue("제목", "첫 줄\n둘째 줄")
        description = handler.received[0]["payload"]["fields"]["description"]
        assert description["type"] == "doc"
        assert description["version"] == 1
        assert len(description["content"]) == 2

    def test_error_body_is_parsed(self, fake_jira):
        """Jira 는 실패 이유를 errors/errorMessages 에 담아준다. 그걸 꺼내야 한다."""
        from app.jira import create_issue, JiraError

        application, _ = fake_jira
        with application.app_context():
            application.config["JIRA_PROJECT_KEY"] = "BAD"
            with pytest.raises(JiraError) as e:
                create_issue("x", "y")
        assert "project is required" in str(e.value)
        assert "400" in str(e.value)

    def test_connection_failure(self, app):
        from app.jira import create_issue, JiraError

        with app.app_context():
            app.config.update(JIRA_BASE_URL="http://127.0.0.1:1",
                              JIRA_EMAIL="a@b.c", JIRA_API_TOKEN="t",
                              JIRA_PROJECT_KEY="OPS")
            with pytest.raises(JiraError) as e:
                create_issue("x", "y")
        assert "연결하지 못했습니다" in str(e.value)


@pytest.mark.db
class TestMembersAndState:
    @pytest.fixture
    def clean(self, db_uri):
        import psycopg

        def purge():
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM oncall_members WHERE name LIKE 'test-%'")
                cur.execute("DELETE FROM escalations WHERE account_id = 'test-acct'")

        purge()
        yield
        purge()

    def test_scoped_member_beats_global(self, db_app, clean):
        """고객사 전담이 있으면 전체 담당보다 우선한다."""
        from app.escalation import add_member, members, _for_level

        with db_app.app_context():
            add_member("test-전체", 1, "U0G")
            add_member("test-전담", 1, "U0S", customer="test-고객사")
            people = _for_level(members("test-고객사"), 1)
        names = {m["name"] for m in people}
        # 전담이 있으면 전체 담당은 빠진다.
        assert names == {"test-전담"}, "전담이 있으면 그 사람만 불려야 한다"

    def test_global_used_when_no_scoped(self, db_app, clean):
        """전담이 없으면 전체 담당이 나온다.

        실제로 등록된 담당자가 함께 있을 수 있으므로 이 테스트가 만든
        것만 확인한다. 개발용 DB 를 비우는 테스트는 쓰지 않는다.
        """
        from app.escalation import add_member, members, _for_level

        with db_app.app_context():
            add_member("test-전체", 1, "U0G")
            people = _for_level(members("이-고객사는-전담이-없다"), 1)
        names = {m["name"] for m in people}
        assert "test-전체" in names
        # 전담이 없으므로 고객사가 붙은 사람은 아무도 없어야 한다.
        assert all(not m["customer"] for m in people)

    def test_invalid_level_rejected(self, db_app, clean):
        from app.escalation import add_member, EscalationError

        with db_app.app_context():
            with pytest.raises(EscalationError):
                add_member("test-x", 0)
            with pytest.raises(EscalationError):
                add_member("test-x", "일단계")
            with pytest.raises(EscalationError):
                add_member("   ", 1)

    def test_same_level_is_not_called_twice(self, db_app, clean):
        from app.escalation import record, already_notified, pending

        item = {
            "account_id": "test-acct", "fingerprint": "fp1", "minutes": 15,
            "elapsed_minutes": 200, "customer": "c", "severity": "critical",
            "sample": "s", "source": "src", "count": 1,
        }
        with db_app.app_context():
            first = pending([item], [0, 30, 120])
            assert first[0]["levels"] == [1, 2, 3]
            for level in first[0]["levels"]:
                record("test-acct", "fp1", level)
            assert already_notified("test-acct", "fp1") == {1, 2, 3}
            assert pending([item], [0, 30, 120]) == []

    def test_skipped_levels_are_filled_in(self, db_app, clean):
        """3단계까지 가야 하는데 1단계만 올렸으면 2, 3 을 한 번에 올린다.

        중간 단계를 건너뛰면 그 사람은 자기가 호출된 적 없다는 것도 모른다.
        """
        from app.escalation import record, pending

        item = {
            "account_id": "test-acct", "fingerprint": "fp2", "minutes": 15,
            "elapsed_minutes": 500, "customer": "c", "severity": "critical",
            "sample": "s", "source": "src", "count": 1,
        }
        with db_app.app_context():
            record("test-acct", "fp2", 1)
            todo = pending([item], [0, 30, 120])
        assert todo[0]["levels"] == [2, 3]

    def test_clear_lets_it_start_over(self, db_app, clean):
        """대응이 끝난 뒤 같은 알람이 다시 나면 1단계부터 다시 올라가야 한다."""
        from app.escalation import record, clear, already_notified

        with db_app.app_context():
            record("test-acct", "fp3", 1)
            assert clear("test-acct", "fp3") == 1
            assert already_notified("test-acct", "fp3") == set()
