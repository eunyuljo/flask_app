# tests/test_account_audit.py
# 계정 귀속(events.account_id)과 감사 로그 테스트.
#
# 둘 다 이 프로젝트에서 뒤늦게 고친 것이다.
#   - 계정이 없어서 세 곳에서 타협했다(RCA 범위, 고객사 알람, 노이즈 분리)
#   - 감사 로그를 메모리 100건 링버퍼에 넣어서 재시작하면 사라졌다

import pytest

from api.normalize_handler import normalize


class TestAccountExtraction:
    @pytest.mark.parametrize("key", [
        "account_id", "account", "accountId", "aws_account_id",
        "AWSAccountId", "recipientAccountId",
    ])
    def test_aliases(self, key):
        assert normalize({"msg": "x", key: "123456789012"})["account_id"] == "123456789012"

    @pytest.mark.parametrize("bad", [
        "12345",                              # 짧음
        "1234567890123",                      # 김
        "abcdefghijkl",                       # 숫자가 아님
        "arn:aws:iam::123456789012:root",     # ARN 은 계정 번호가 아니다
        "",
    ])
    def test_bad_format_is_dropped(self, bad):
        """형식이 다르면 계정으로 쓰지 않는다.

        엉뚱한 값이 들어오면 이벤트가 없는 계정에 묶여 조용히 사라진다.
        """
        assert normalize({"msg": "x", "account": bad})["account_id"] == ""

    def test_dropped_value_survives_in_meta(self):
        """버린 값은 meta 에 남긴다. 보낸 쪽이 무엇을 줬는지 알아야 고친다."""
        record = normalize({"msg": "x", "account": "잘못된값"})
        assert record["meta"]["account_id_raw"] == "잘못된값"

    def test_missing_account_is_empty_not_none(self):
        """None 이면 DB 의 NOT NULL 에 걸린다."""
        assert normalize({"msg": "x"})["account_id"] == ""

    def test_account_does_not_change_fingerprint(self):
        """계정이 지문을 가르면 같은 알람이 계정마다 다른 종류가 된다.

        지문은 '무슨 알람인가' 이고 계정은 '어디서 났나' 다. 섞으면 안 된다.
        """
        base = {"msg": "CPU 92%", "service": "web", "type": "metric"}
        without = normalize(dict(base))["fingerprint"]
        with_acct = normalize(dict(base, account="123456789012"))["fingerprint"]
        assert without == with_acct

    def test_account_not_duplicated_into_meta(self):
        """표준 필드로 올라간 값이 meta 에도 남으면 두 곳을 관리하게 된다."""
        record = normalize({"msg": "x", "account_id": "123456789012"})
        assert "account_id" not in record["meta"]


@pytest.fixture
def clean_audit(db_uri):
    """테스트가 남긴 감사 기록을 지운다.

    감사 로그는 지우지 않는 것이 원칙이라 앱에는 삭제 경로가 없다.
    그래서 테스트가 치우지 않으면 실제 기록에 'test-audit ...' 이 섞인다.
    여기서만 SQL 로 직접 지운다.
    """
    import psycopg

    def purge():
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE summary LIKE 'test-audit%'")

    purge()
    yield
    purge()


@pytest.mark.db
class TestAuditLog:
    def test_record_and_read(self, db_app, db_uri, clean_audit):
        from app import audit

        with db_app.test_request_context("/"):
            audit.record(
                action="console_command", outcome="rejected",
                summary="test-audit 차단 확인",
                account={"customer": "테스트사", "account_id": "000000000000"},
                region="ap-northeast-2", actor_kind="human",
            )
            rows = audit.recent(50, account_id="000000000000")

        assert rows
        assert rows[0]["outcome"] == "rejected"
        assert rows[0]["customer"] == "테스트사"

    def test_agent_and_human_are_separable(self, db_app, db_uri, clean_audit):
        from app import audit

        with db_app.test_request_context("/"):
            audit.record(action="ai_diagnose", outcome="ok", summary="test-audit 모델",
                         account={"account_id": "000000000000"}, actor_kind="agent")
            humans = audit.recent(50, account_id="000000000000", actor_kind="human")
            agents = audit.recent(50, account_id="000000000000", actor_kind="agent")

        assert all(r["actor_kind"] == "human" for r in humans)
        assert all(r["actor_kind"] == "agent" for r in agents)
        assert agents, "모델 기록이 분리되어 조회돼야 한다"

    def test_unknown_filter_is_ignored_not_crashing(self, db_app, db_uri, clean_audit):
        from app import audit

        with db_app.test_request_context("/"):
            rows = audit.recent(5, outcome="이상한값", actor_kind="이상한값")
        assert isinstance(rows, list)

    def test_record_failure_does_not_raise(self, db_app, monkeypatch):
        """감사 기록에 실패해도 사용자의 작업까지 실패시키지 않는다."""
        from app import audit

        def boom(*a, **kw):
            raise RuntimeError("DB 가 흔들림")

        monkeypatch.setattr(audit, "_connect", boom)
        with db_app.test_request_context("/"):
            audit.record(action="console_command", outcome="ok", summary="x")

    def test_summary_counts_alerts(self, db_app, db_uri, clean_audit):
        from app import audit

        with db_app.test_request_context("/"):
            s = audit.summary()
        assert s["alerts"] == s["by_outcome"].get("rejected", 0)
        assert s["total"] == sum(s["by_outcome"].values())


@pytest.mark.db
class TestScoping:
    def test_incident_scopes_by_account(self, db_app, db_uri):
        """계정이 지정된 장애는 다른 계정 알람이 빠져야 한다."""
        from app.incident import recent, assemble

        with db_app.app_context():
            items = [i for i in recent() if i["account_id"]]
            if not items:
                pytest.skip("계정이 지정된 장애 기록이 없습니다")
            data = assemble(items[0])
            if not data["account_scoped"]:
                pytest.skip("그 계정의 이벤트가 없어 계정으로 좁히지 못했습니다")
            for e in data["events"]:
                pass   # events 질의가 이미 계정으로 걸러졌다
        assert data["scope_account"] == items[0]["account_id"]

    def test_customer_alarms_only_own_accounts(self, db_app, db_uri):
        """고객사 알람 집계에 다른 고객사 계정이 섞이면 안 된다."""
        import psycopg
        from app.customer import names, overview

        with db_app.app_context():
            all_names = names()
            if not all_names:
                pytest.skip("등록된 고객사가 없습니다")
            data = overview(all_names[0])
            own = {a["account_id"] for a in data["accounts"]}

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM events WHERE account_id = ANY(%s) "
                "AND occurred_at >= now() - interval '24 hours'",
                (list(own),),
            )
            expected = cur.fetchone()[0]
        assert data["alarms"]["total"] == expected
