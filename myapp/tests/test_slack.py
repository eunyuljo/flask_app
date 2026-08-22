# tests/test_slack.py
# Slack 전송 계층 테스트.
#
# 실제 Slack 에 붙지 않고, 같은 규약을 흉내내는 서버를 세워서 검증한다.
# (성공은 본문이 정확히 "ok", 실패는 400 + 평문 이유)

import http.server
import json
import threading

import pytest

from app.slack import (
    post, webhook_url, is_configured, escape,
    SlackError, SlackNotConfigured, MAX_TEXT,
)


class _Handler(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/bad":
            self.send_response(400); self.end_headers()
            self.wfile.write(b"invalid_payload"); return
        if self.path == "/weird":
            self.send_response(200); self.end_headers()
            self.wfile.write(b"not ok"); return
        _Handler.received.append(json.loads(body))
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_slack():
    """Slack 웹훅을 흉내내는 서버. 포트는 OS 가 고르게 한다."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.received = []
    yield f"http://127.0.0.1:{server.server_port}", _Handler
    server.shutdown()


class TestEscape:
    def test_escapes_slack_specials(self):
        assert escape("a & b < c > d") == "a &amp; b &lt; c &gt; d"

    def test_does_not_touch_asterisks(self):
        """알람 메시지에 흔한 문자까지 막으면 오히려 읽기 나빠진다."""
        assert escape("CPU 92% *high*") == "CPU 92% *high*"

    def test_handles_non_string(self):
        assert escape(42) == "42"


class TestConfiguration:
    def test_not_configured_raises_its_own_error(self, app):
        """설정 안 한 것은 '실패' 와 다르다. 호출하는 쪽이 구분해야 한다."""
        with app.app_context():
            with pytest.raises(SlackNotConfigured):
                post("x")

    def test_not_configured_is_a_slack_error(self, app):
        """다만 SlackError 로 한꺼번에 잡을 수도 있어야 한다."""
        assert issubclass(SlackNotConfigured, SlackError)

    def test_purpose_webhook_wins(self, app):
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = "https://example.com/common"
            app.config["SLACK_SLA_WEBHOOK"] = "https://example.com/sla"
            assert webhook_url("sla") == "https://example.com/sla"

    def test_falls_back_to_common(self, app):
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = "https://example.com/common"
            app.config["SLACK_HANDOVER_WEBHOOK"] = ""
            assert webhook_url("handover") == "https://example.com/common"

    def test_is_configured(self, app):
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = ""
            assert is_configured() is False
            app.config["SLACK_WEBHOOK_URL"] = "https://example.com/x"
            assert is_configured() is True


class TestPost:
    def test_success(self, app, fake_slack):
        base, handler = fake_slack
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = base + "/ok"
            assert post("안녕하세요") is True
        assert handler.received[0]["text"] == "안녕하세요"

    def test_blocks_are_sent_with_text(self, app, fake_slack):
        base, handler = fake_slack
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = base + "/ok"
            post("폴백", blocks=[{"type": "divider"}])
        payload = handler.received[0]
        assert payload["text"] == "폴백"
        assert payload["blocks"] == [{"type": "divider"}]

    def test_long_text_is_truncated_with_notice(self, app, fake_slack):
        """조용히 자르면 받는 쪽은 내용이 원래 그만큼인 줄 안다."""
        base, handler = fake_slack
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = base + "/ok"
            post("가" * (MAX_TEXT + 5000))
        sent = handler.received[0]["text"]
        assert len(sent) < MAX_TEXT + 200
        assert "잘렸습니다" in sent

    def test_http_error_is_reported_with_reason(self, app, fake_slack):
        base, _ = fake_slack
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = base + "/bad"
            with pytest.raises(SlackError) as e:
                post("x")
        assert "invalid_payload" in str(e.value)

    def test_non_ok_body_is_an_error(self, app, fake_slack):
        """Slack 은 성공하면 본문이 정확히 'ok' 다."""
        base, _ = fake_slack
        with app.app_context():
            app.config["SLACK_WEBHOOK_URL"] = base + "/weird"
            with pytest.raises(SlackError):
                post("x")

    def test_connection_failure(self, app):
        with app.app_context():
            # 닫힌 포트
            app.config["SLACK_WEBHOOK_URL"] = "http://127.0.0.1:1/none"
            with pytest.raises(SlackError) as e:
                post("x")
        assert "연결하지 못했습니다" in str(e.value)


@pytest.mark.db
class TestRenderers:
    def test_handover_slack_has_no_markdown_tables(self, db_app, db_uri):
        """Slack mrkdwn 은 표를 렌더링하지 못한다.

        to_markdown 을 그대로 보내면 파이프가 잔뜩 찍힌 글덩어리가 된다.
        """
        from app.handover import collect, to_slack, to_markdown

        with db_app.app_context():
            data = collect(24)
            slack_text = to_slack(data)
            md = to_markdown(data)

        assert "|---" in md, "Markdown 쪽은 표를 쓴다"
        assert "|---" not in slack_text
        assert "**" not in slack_text, "Slack 은 *굵게* 다"

    def test_handover_slack_links_only_with_base_url(self, db_app, db_uri):
        from app.handover import collect, to_slack

        with db_app.app_context():
            data = collect(24)
            without = to_slack(data)
            with_url = to_slack(data, base_url="https://msp.example.com/")

        assert "https://msp.example.com" not in without
        assert "<https://msp.example.com/handover/" in with_url

    def test_sla_slack_states_its_limitation(self, db_app, db_uri):
        """근사치라는 사실이 메시지에 남아야 한다."""
        from app import sla

        with db_app.app_context():
            found = sla.breaches(24)
            if not found:
                pytest.skip("목표를 넘긴 알람이 없습니다")
            text = sla.to_slack(found[:2])
        assert "감사 로그" in text
