# tests/test_rca_draft.py
# 사후 보고서 AI 초안과 Jira 등록.
#
# 모델 호출은 가짜로 바꾼다. 개발 환경에 API 키가 없기도 하지만,
# 그보다 확인하고 싶은 게 모델의 글솜씨가 아니라 그 주변이기 때문이다.
#   - 근거를 빠짐없이 실어 보내는가
#   - 초안이 사람 확인 없이 저장되지는 않는가
#   - 이미 쓴 칸을 덮어쓰지는 않는가

import json
import types

import pytest

from app import agent_core


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, payload, stop_reason="end_turn", stop_details=None):
        self.content = [FakeBlock(payload)]
        self.stop_reason = stop_reason
        self.stop_details = stop_details


DRAFT = {
    "impact": "결제 API 가 28분간 지연되었습니다.",
    "cause": "보안그룹에 5432/tcp 를 0.0.0.0/0 으로 열면서 커넥션 풀이 고갈됐습니다.",
    "action": "규칙을 되돌렸습니다.",
    "prevention": "DB 포트 개방은 승인 단계를 거칩니다.",
    "uncertain": ["규칙 회수 시각이 기록에 없습니다."],
}


@pytest.fixture
def fake_model(monkeypatch):
    """모델 호출만 가짜로. 넘어간 파라미터를 들여다볼 수 있게 남긴다."""
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            return FakeResponse(json.dumps(seen.get("_payload", DRAFT), ensure_ascii=False))

    client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages()))
    monkeypatch.setattr(agent_core, "_build_client", lambda p, c: client)
    monkeypatch.setattr(agent_core, "check_config", lambda: None)
    return seen


def item(**over):
    from datetime import datetime, timezone

    base = {
        "id": 7, "title": "결제 API 지연", "severity": "critical",
        "started_at": datetime(2026, 8, 21, 15, 58, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 8, 21, 16, 28, tzinfo=timezone.utc),
        "customer": "가고객", "account_id": "123456789012", "region": "ap-northeast-2",
        "author": "admin", "status": "draft", "summary": "",
        "published_at": None, "jira_key": "",
        "impact": "", "cause": "", "action": "", "prevention": "",
    }
    base.update(over)
    return base


def data(**over):
    from datetime import datetime, timezone

    at = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
    base = {
        "window": {"start": at, "end": at, "lead": 30, "tail": 30},
        "account_scoped": True, "event_count": 3,
        "timeline": [{"at": at, "kind": "alarm", "severity": "critical",
                      "text": "결제 API 응답 지연 2100ms", "detail": "pay-api · http"}],
        "by_kind": [{"fingerprint": "abc", "c": 8, "sample": "결제 API 응답 지연",
                     "severity": "critical", "source": "pay-api",
                     "first_seen": at, "last_seen": at}],
        "works": [], "rdiff": None, "diff_note": "",
    }
    base.update(over)
    return base


class TestEvidence:
    """모델에게 넘기는 근거."""

    def test_timeline_is_included(self, app):
        with app.app_context():
            text = agent_core._format_incident(item(), data())
        assert "결제 API 응답 지연 2100ms" in text

    def test_resource_change_field_names(self, app):
        """change/field 를 status/key 로 잘못 읽으면 '[?] None:' 만 나간다."""
        rdiff = {"summary": {"added": 0, "removed": 0, "modified": 1},
                 "changes": [{"resource_id": "sg-pay", "resource_type": "ec2:security_group",
                              "change": "modified",
                              "fields": [{"field": "ingress",
                                          "before": ["443/tcp:10.0.0.0/8"],
                                          "after": ["5432/tcp:0.0.0.0/0"]}]}]}
        with app.app_context():
            text = agent_core._format_incident(item(), data(rdiff=rdiff))
        assert "[modified]" in text and "ingress:" in text
        assert "[?]" not in text and "None:" not in text

    def test_past_incident_cause_is_included(self, app):
        """재발인지 아닌지가 원인 분석에서 가장 크게 갈리는 지점이다."""
        from datetime import datetime, timezone

        past = [{"id": 2, "title": "같은 장애", "event_count": 8,
                 "started_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                 "cause": "그때도 같은 보안그룹 규칙이었다", "action": "되돌림"}]
        with app.app_context():
            text = agent_core._format_incident(item(), data(), past=past)
        assert "그때도 같은 보안그룹 규칙이었다" in text

    def test_already_written_fields_are_passed(self, app):
        """이미 파악된 것을 다시 추측하게 두면 안 된다."""
        with app.app_context():
            text = agent_core._format_incident(item(cause="이미 쓴 원인"), data())
        assert "이미 쓴 원인" in text

    def test_unscoped_timeline_is_flagged(self, app):
        """계정으로 좁히지 못했으면 다른 계정 알람이 섞여 있다."""
        with app.app_context():
            text = agent_core._format_incident(item(), data(account_scoped=False))
        assert "섞여 있을 수 있습니다" in text

    def test_long_evidence_is_truncated_visibly(self, app):
        """말없이 자르면 모델이 '이게 전부' 로 읽고 없는 것을 근거로 삼는다."""
        from datetime import datetime, timezone

        at = datetime(2026, 8, 21, 16, 0, tzinfo=timezone.utc)
        many = [{"at": at, "kind": "alarm", "severity": "error",
                 "text": "x" * 400, "detail": "y"} for _ in range(300)]
        with app.app_context():
            text = agent_core._format_incident(item(), data(timeline=many))
        assert len(text) <= agent_core.RCA_MAX_INPUT + 100
        assert "잘렸습니다" in text


class TestRequestShape:
    def test_no_tools_are_attached(self, app, fake_model):
        """장애는 지난 일이고 근거는 DB 에 있다. AWS 를 조회할 이유가 없다."""
        with app.app_context():
            agent_core.draft_rca(item(), data())
        assert "tools" not in fake_model

    def test_asks_for_the_four_fields_plus_uncertain(self, app, fake_model):
        with app.app_context():
            agent_core.draft_rca(item(), data())
        schema = fake_model["output_config"]["format"]["schema"]
        assert set(schema["required"]) == {
            "impact", "cause", "action", "prevention", "uncertain"
        }

    def test_schema_fields_match_the_narrative_columns(self):
        """스키마와 DB 칸이 갈라지면 초안이 저장되지 않는다."""
        from app.incident import NARRATIVE_FIELDS

        props = set(agent_core.RCA_SCHEMA["properties"]) - {"uncertain"}
        assert props == set(NARRATIVE_FIELDS)

    def test_thinking_is_on(self, app, fake_model):
        """타임라인에서 인과를 골라내는 일이라 그냥 요약보다 어렵다."""
        with app.app_context():
            agent_core.draft_rca(item(), data())
        assert fake_model["thinking"]["type"] == "adaptive"


class TestResult:
    def test_returns_the_four_fields(self, app, fake_model):
        with app.app_context():
            out = agent_core.draft_rca(item(), data())
        assert out["cause"].startswith("보안그룹")
        assert out["uncertain"]

    def test_refusal_is_reported_not_parsed(self, app, monkeypatch):
        """거절당하면 content 가 비어 있다. 먼저 JSON 으로 파싱하려 들면
        엉뚱한 에러가 나서 원인을 못 찾는다."""
        class FakeMessages:
            def create(self, **kwargs):
                return FakeResponse("", stop_reason="refusal",
                                    stop_details=types.SimpleNamespace(category="cyber"))

        client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages()))
        monkeypatch.setattr(agent_core, "_build_client", lambda p, c: client)
        monkeypatch.setattr(agent_core, "check_config", lambda: None)
        with app.app_context():
            with pytest.raises(agent_core.AgentNotConfigured) as e:
                agent_core.draft_rca(item(), data())
        assert "cyber" in str(e.value)

    def test_broken_json_is_reported(self, app, monkeypatch):
        class FakeMessages:
            def create(self, **kwargs):
                return FakeResponse("JSON 이 아님")

        client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages()))
        monkeypatch.setattr(agent_core, "_build_client", lambda p, c: client)
        monkeypatch.setattr(agent_core, "check_config", lambda: None)
        with app.app_context():
            with pytest.raises(agent_core.AgentNotConfigured):
                agent_core.draft_rca(item(), data())

    def test_missing_config_is_reported_before_calling(self, app, monkeypatch):
        monkeypatch.setattr(agent_core, "check_config", lambda: "키가 없습니다")
        with app.app_context():
            with pytest.raises(agent_core.AgentNotConfigured):
                agent_core.draft_rca(item(), data())


class TestJiraRendering:
    def test_no_markdown_syntax(self, app):
        """_adf() 는 평문을 문단으로만 감싼다. '#' 과 '|' 가 글자 그대로 찍힌다."""
        from app.rca import to_jira

        with app.app_context():
            summary, body = to_jira(item(status="published", cause="원인"), data())
        assert not any(line.startswith("#") for line in body.split("\n"))
        assert "|" not in body

    def test_summary_is_prefixed(self, app):
        from app.rca import to_jira

        with app.app_context():
            summary, _ = to_jira(item(status="published"), data())
        assert summary.startswith("[장애]")

    def test_empty_field_says_so(self, app):
        """빈 칸을 그냥 비워두면 읽는 사람이 항목을 놓친다."""
        from app.rca import to_jira

        with app.app_context():
            _, body = to_jira(item(status="published"), data())
        assert "(작성되지 않음)" in body


@pytest.mark.db
class TestRoutes:
    """화면 흐름. 초안이 사람 확인 없이 저장되지 않는 것이 핵심이다."""

    @pytest.fixture
    def logged(self, db_client, monkeypatch):
        seen = {}

        class FakeMessages:
            def create(self, **kwargs):
                seen.update(kwargs)
                return FakeResponse(json.dumps(DRAFT, ensure_ascii=False))

        client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=FakeMessages()))
        monkeypatch.setattr(agent_core, "_build_client", lambda p, c: client)
        monkeypatch.setattr(agent_core, "check_config", lambda: None)

        return db_client

    @pytest.fixture
    def fresh(self, db_app, db_uri):
        """테스트용 장애 하나. 끝나면 지운다."""
        from datetime import datetime, timedelta, timezone

        import psycopg

        from app import incident

        with db_app.app_context():
            iid = incident.create(
                "테스트 장애", datetime.now(timezone.utc) - timedelta(hours=1),
                "tester", ended_at=datetime.now(timezone.utc), severity="critical",
            )
        yield iid
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM incidents WHERE id = %s", (iid,))

    def test_draft_is_not_saved(self, logged, fresh, db_app):
        from app import incident

        logged.post(f"/incident/{fresh}/rca/draft")
        with db_app.app_context():
            assert not incident.get(fresh)["cause"].strip()

    def test_draft_appears_in_the_form(self, logged, fresh):
        logged.post(f"/incident/{fresh}/rca/draft")
        body = logged.get(f"/incident/{fresh}").get_data(as_text=True)
        assert "AI 초안 (미저장)" in body
        assert "커넥션 풀이 고갈됐습니다" in body

    def test_uncertainties_are_shown(self, logged, fresh):
        """무엇을 확신 못 하는지 안 보여주면 초안을 결론으로 읽는다."""
        logged.post(f"/incident/{fresh}/rca/draft")
        body = logged.get(f"/incident/{fresh}").get_data(as_text=True)
        assert "규칙 회수 시각이 기록에 없습니다" in body

    def test_written_fields_are_not_overwritten(self, logged, fresh, db_app):
        from app import incident

        with db_app.app_context():
            incident.update_narrative(fresh, {"cause": "사람이 쓴 원인"})
        logged.post(f"/incident/{fresh}/rca/draft")
        body = logged.get(f"/incident/{fresh}").get_data(as_text=True)
        assert "사람이 쓴 원인" in body
        assert "커넥션 풀이 고갈됐습니다" not in body

    def test_discard(self, logged, fresh):
        logged.post(f"/incident/{fresh}/rca/draft")
        logged.post(f"/incident/{fresh}/rca/discard")
        body = logged.get(f"/incident/{fresh}").get_data(as_text=True)
        assert "AI 초안 (미저장)" not in body

    def test_saving_clears_the_draft(self, logged, fresh):
        logged.post(f"/incident/{fresh}/rca/draft")
        logged.post(f"/incident/{fresh}/save",
                    data={"impact": "a", "cause": "b", "action": "c", "prevention": "d"})
        body = logged.get(f"/incident/{fresh}").get_data(as_text=True)
        assert "AI 초안 (미저장)" not in body

    def test_jira_needs_a_published_report(self, logged, fresh):
        """초안 상태로 넘기면 나중에 내용이 갈린다."""
        body = logged.post(f"/incident/{fresh}/jira",
                           follow_redirects=True).get_data(as_text=True)
        assert "제출된 보고서만" in body

    def test_jira_is_created_once(self, logged, fresh, db_app, monkeypatch):
        from app import incident, jira as jira_mod

        calls = []
        monkeypatch.setattr(jira_mod, "is_configured", lambda: True)
        monkeypatch.setattr(jira_mod, "create_issue",
                            lambda s, d, labels=None: (calls.append(s), "OPS-1")[1])

        with db_app.app_context():
            incident.update_narrative(fresh, {"cause": "원인", "action": "조치"})
            incident.publish(fresh)

        logged.post(f"/incident/{fresh}/jira")
        body = logged.post(f"/incident/{fresh}/jira",
                           follow_redirects=True).get_data(as_text=True)
        assert len(calls) == 1
        assert "이미 Jira 이슈가 있습니다" in body
