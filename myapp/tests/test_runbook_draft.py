# tests/test_runbook_draft.py
# 런북 초안 자동 생성.
#
# 런북 화면이 오래 비어 있는 이유는 하나다. 빈 칸에서 시작하는 것이 어렵다.
# 잦은 알람 목록은 이미 있으니 거기서 초안을 만들어 사람이 고치게 한다.
#
# 지키는 것:
#   1. 저장하지 않는다 (모델이 쓴 절차가 확인 없이 행동 지침이 되면 안 된다)
#   2. 새로고침이 모델을 다시 부르지 않는다 (부를 때마다 비용이다)
#   3. 근거는 화면이 쓰는 것과 같은 함수에서 온다
#   4. 설정이 없으면 버튼을 숨긴다

from unittest.mock import patch

import pytest

from app import agent_core


DRAFT = {
    # 폼 placeholder 와 겹치지 않는 제목을 쓴다. 겹치면
    # "초안이 채워졌나" 를 문자열로 확인할 수 없다.
    "title": "시험용 초안 제목 ZZQ",
    "body": "1. 인스턴스 상태를 확인한다\n2. 최근 배포 이력을 본다",
    "uncertain": ["이 환경의 정상 CPU 기준선을 모릅니다"],
}


class TestSchema:
    def test_schema_is_strict(self):
        """칸이 비어서 오면 초안이 반쪽이 된다."""
        schema = agent_core.RUNBOOK_SCHEMA
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {"title", "steps", "escalate_when",
                                           "uncertain"}

    def test_prompt_demands_escalation(self):
        """언제 사람을 부르는지가 없으면 당직자가 혼자 붙들고 있는다."""
        assert "에스컬레이션" in agent_core.RUNBOOK_SYSTEM_PROMPT

    def test_prompt_forbids_making_things_up(self):
        assert "지어내지" in agent_core.RUNBOOK_SYSTEM_PROMPT
        assert "uncertain" in agent_core.RUNBOOK_SYSTEM_PROMPT

    def test_prompt_puts_checks_before_changes(self):
        assert "확인부터" in agent_core.RUNBOOK_SYSTEM_PROMPT


class TestBodyFormatting:
    def test_steps_become_numbered_lines(self):
        body = agent_core._steps_to_body(
            {"steps": ["상태 확인", "배포 이력 확인"], "escalate_when": ""})
        assert body == "1. 상태 확인\n2. 배포 이력 확인"

    def test_escalation_is_appended(self):
        body = agent_core._steps_to_body(
            {"steps": ["확인"], "escalate_when": "30분 지속되면 2단계"})
        assert "사람을 부를 때: 30분 지속되면 2단계" in body

    def test_blank_steps_are_dropped(self):
        body = agent_core._steps_to_body(
            {"steps": ["확인", "  ", ""], "escalate_when": ""})
        assert body == "1. 확인"

    def test_empty_draft(self):
        assert agent_core._steps_to_body({}) == ""


class TestEvidence:
    def test_no_past_incidents_is_stated(self):
        """근거가 없다는 것을 모델에게 알려야 지어내지 않는다."""
        text = agent_core._format_alarm_kind("CPU 92%", "warning", "web-01",
                                             history=None, past=[])
        assert "지난 장애 기록이 없습니다" in text

    def test_past_incidents_are_included(self):
        from datetime import datetime, timezone

        text = agent_core._format_alarm_kind(
            "CPU 92%", "warning", "web-01", history=None,
            past=[{"title": "결제 지연", "started_at": datetime(2026, 8, 1,
                                                              tzinfo=timezone.utc),
                   "cause": "커넥션 풀 고갈", "action": "풀 크기 상향"}])
        assert "결제 지연" in text
        assert "커넥션 풀 고갈" in text

    def test_history_is_included(self):
        text = agent_core._format_alarm_kind(
            "CPU 92%", "warning", "", history={"total": 47, "samples": []})
        assert "47회" in text


@pytest.mark.db
class TestRoutes:
    def test_draft_fills_the_form_without_saving(self, db_client, db_app):
        from app import runbook

        with patch("app.views.runbook.draft_runbook", return_value=DRAFT), \
             patch("app.views.runbook.check_config", return_value=None):
            r = db_client.post("/runbook/draft",
                               data={"fingerprint": "시험지문", "sample": "CPU 92%",
                                     "severity": "warning"})
            assert r.status_code == 302        # PRG

            body = db_client.get(
                "/runbook/new?fingerprint=시험지문").get_data(as_text=True)

        assert "아직 저장되지 않았습니다" in body
        assert DRAFT["title"] in body
        assert "인스턴스 상태를 확인한다" in body

        with db_app.app_context():
            assert runbook.find("시험지문") is None

    def test_refresh_does_not_call_the_model_again(self, db_client):
        """새로고침마다 모델을 부르면 비용이 그만큼 나간다."""
        calls = []

        def spy(**kwargs):
            calls.append(kwargs)
            return DRAFT

        with patch("app.views.runbook.draft_runbook", side_effect=spy), \
             patch("app.views.runbook.check_config", return_value=None):
            db_client.post("/runbook/draft",
                           data={"fingerprint": "시험지문2", "sample": "x"})
            first = db_client.get(
                "/runbook/new?fingerprint=시험지문2").get_data(as_text=True)
            second = db_client.get(
                "/runbook/new?fingerprint=시험지문2").get_data(as_text=True)

        assert len(calls) == 1
        assert "아직 저장되지 않았습니다" in first
        assert "아직 저장되지 않았습니다" not in second

    def test_draft_for_another_alarm_is_not_shown(self, db_client):
        """다른 알람의 초안이 엉뚱한 폼에 채워지면 잘못된 절차가 저장된다."""
        with patch("app.views.runbook.draft_runbook", return_value=DRAFT), \
             patch("app.views.runbook.check_config", return_value=None):
            db_client.post("/runbook/draft", data={"fingerprint": "지문A",
                                                   "sample": "x"})
            body = db_client.get(
                "/runbook/new?fingerprint=지문B").get_data(as_text=True)

        assert DRAFT["title"] not in body
        assert "인스턴스 상태를 확인한다" not in body

    def test_button_hidden_without_config(self, db_client):
        with patch("app.views.runbook.check_config", return_value="키가 없습니다"):
            body = db_client.get(
                "/runbook/new?fingerprint=x").get_data(as_text=True)
        assert "AI 로 초안 만들기" not in body

    def test_button_shown_with_config(self, db_client):
        with patch("app.views.runbook.check_config", return_value=None):
            body = db_client.get(
                "/runbook/new?fingerprint=x").get_data(as_text=True)
        assert "AI 로 초안 만들기" in body

    def test_no_fingerprint_is_refused(self, db_client):
        r = db_client.post("/runbook/draft", data={"sample": "x"},
                           follow_redirects=True)
        assert "지문이 없습니다" in r.get_data(as_text=True)

    def test_model_failure_does_not_break_the_page(self, db_client):
        from app.agent_core import AgentNotConfigured

        with patch("app.views.runbook.draft_runbook",
                   side_effect=AgentNotConfigured("키가 없습니다")), \
             patch("app.views.runbook.check_config", return_value=None):
            r = db_client.post("/runbook/draft",
                               data={"fingerprint": "지문C", "sample": "x"},
                               follow_redirects=True)
        assert r.status_code == 200
        assert "초안을 만들지 못했습니다" in r.get_data(as_text=True)


class TestModelCall:
    """실제로 부르지는 않고, 요청 모양만 본다."""

    def test_request_shape(self, app):
        captured = {}

        class FakeResponse:
            stop_reason = "end_turn"
            content = [type("B", (), {"type": "text", "text":
                '{"title":"t","steps":["a"],"escalate_when":"e","uncertain":[]}'})()]

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                return FakeResponse()

        class FakeClient:
            beta = type("B", (), {"messages": FakeMessages()})()

        with app.app_context():
            with patch("app.agent_core.check_config", return_value=None), \
                 patch("app.agent_core._build_client", return_value=FakeClient()):
                out = agent_core.draft_runbook("CPU 92%", "warning")

        assert captured["thinking"] == {"type": "adaptive"}
        assert captured["output_config"]["format"]["type"] == "json_schema"
        assert out["title"] == "t"
        assert out["body"].startswith("1. a")

    def test_refusal_is_handled_before_parsing(self, app):
        """refusal 이면 content 가 비어 있다. 먼저 안 보면 엉뚱한 에러가 난다."""
        from app.agent_core import AgentNotConfigured

        class FakeResponse:
            stop_reason = "refusal"
            stop_details = type("D", (), {"category": "cyber"})()
            content = []

        class FakeClient:
            beta = type("B", (), {"messages": type(
                "M", (), {"create": lambda self, **kw: FakeResponse()})()})()

        with app.app_context():
            with patch("app.agent_core.check_config", return_value=None), \
                 patch("app.agent_core._build_client", return_value=FakeClient()):
                with pytest.raises(AgentNotConfigured) as e:
                    agent_core.draft_runbook("x", "warning")
        assert "cyber" in str(e.value)
