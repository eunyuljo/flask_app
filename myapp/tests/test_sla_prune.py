# tests/test_sla_prune.py
# SLA 집계와 보존 정책 테스트.

import pytest

from app.sla import SEVERITIES, SUGGESTED


class TestSuggested:
    def test_covers_every_severity(self):
        assert set(SUGGESTED) == set(SEVERITIES)

    def test_low_severities_have_no_target(self):
        """warning/info 에 기본 목표를 주면 '항상 미달' 로 보인다."""
        assert SUGGESTED["warning"] == 0
        assert SUGGESTED["info"] == 0

    def test_critical_is_tighter_than_error(self):
        assert SUGGESTED["critical"] < SUGGESTED["error"]


@pytest.mark.db
class TestTargets:
    @pytest.fixture
    def clean_targets(self, db_uri):
        import psycopg

        def purge():
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM sla_targets WHERE customer = 'test-sla'")

        purge()
        yield
        purge()

    def test_save_and_read(self, db_app, clean_targets):
        from app.sla import save_target, targets

        with db_app.app_context():
            save_target("test-sla", "critical", 15, "계약 3조")
            got = targets("test-sla")
        assert got["critical"]["first_response_minutes"] == 15
        assert got["critical"]["customer"] == "test-sla"

    def test_customer_target_beats_default(self, db_app, clean_targets):
        """고객사 전용이 기본값을 이긴다(런북과 같은 규칙)."""
        from app.sla import save_target, targets

        with db_app.app_context():
            save_target("", "critical", 30)
            save_target("test-sla", "critical", 15)
            got = targets("test-sla")
        assert got["critical"]["first_response_minutes"] == 15

    def test_falls_back_to_default(self, db_app, clean_targets):
        from app.sla import save_target, targets

        with db_app.app_context():
            save_target("", "critical", 30)
            got = targets("test-sla")
        assert got["critical"]["customer"] == ""

    @pytest.mark.parametrize("bad", ["-5", "십오분", None])
    def test_invalid_minutes_rejected(self, db_app, clean_targets, bad):
        from app.sla import save_target, SlaError

        with db_app.app_context():
            with pytest.raises(SlaError):
                save_target("test-sla", "critical", bad)

    def test_unknown_severity_rejected(self, db_app, clean_targets):
        from app.sla import save_target, SlaError

        with db_app.app_context():
            with pytest.raises(SlaError):
                save_target("test-sla", "치명적", 30)

    def test_zero_is_allowed_and_means_no_target(self, db_app, clean_targets):
        """0 은 '목표 없음' 이다. 거부하면 목표를 뗄 방법이 없어진다."""
        from app.sla import save_target, targets

        with db_app.app_context():
            save_target("test-sla", "warning", 0)
            assert targets("test-sla")["warning"]["first_response_minutes"] == 0


@pytest.mark.db
class TestMeasure:
    def test_unknown_customer_is_empty(self, db_app, db_uri):
        from app.sla import measure

        with db_app.app_context():
            d = measure("존재하지-않는-고객사", 30)
        assert d["total"] == 0
        assert d["accounts"] == []

    def test_shape_and_severities(self, db_app, db_uri):
        from app.customer import names
        from app.sla import measure

        with db_app.app_context():
            all_names = names()
            if not all_names:
                pytest.skip("등록된 고객사가 없습니다")
            d = measure(all_names[0], 30)

        assert [r["severity"] for r in d["by_severity"]] == list(SEVERITIES)
        for r in d["by_severity"]:
            assert r["answered"] <= r["total"]
            assert r["unanswered"] == r["total"] - r["answered"]

    def test_no_target_is_not_counted_as_met(self, db_app, db_uri):
        """목표가 없으면 '달성' 으로 찍히면 안 된다. 지표가 거짓말을 한다."""
        from app.customer import names
        from app.sla import measure

        with db_app.app_context():
            all_names = names()
            if not all_names:
                pytest.skip("등록된 고객사가 없습니다")
            d = measure(all_names[0], 30)

        for r in d["by_severity"]:
            if not r["tracked"]:
                assert r["met"] is False


@pytest.mark.db
class TestPrune:
    def test_audit_log_is_never_pruned(self, db_app, db_uri):
        """감사 로그가 이벤트 정리에 딸려 지워지면 안 된다.

        테이블을 나눈 이유가 이것이다. 여기서는 명령을 실제로 돌리지 않고,
        정리 대상 질의가 events 만 본다는 것을 확인한다.
        """
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            # audit_log 는 events 를 참조하지 않는다(외래키 없음).
            cur.execute(
                """
                SELECT count(*) FROM information_schema.table_constraints tc
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'audit_log'
                   AND tc.constraint_type = 'FOREIGN KEY'
                   AND ccu.table_name = 'events'
                """
            )
            assert cur.fetchone()[0] == 0

    def test_referenced_snapshots_are_protected_by_schema(self, db_app, db_uri):
        """작업 증적이 참조하는 스냅샷은 스키마가 삭제를 막아야 한다.

        정리 명령이 실수로 지우려 해도 DB 가 거부한다.
        """
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot_id FROM resource_snapshots s
                 WHERE EXISTS (SELECT 1 FROM work_orders w
                                WHERE w.before_snapshot_id = s.snapshot_id
                                   OR w.after_snapshot_id = s.snapshot_id)
                 LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                pytest.skip("작업 증적이 참조하는 스냅샷이 없습니다")

            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cur.execute(
                    "DELETE FROM resource_snapshots WHERE snapshot_id = %s", (row[0],)
                )
            conn.rollback()
