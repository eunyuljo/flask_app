# tests/test_suppression.py
# 알람 억제 테스트.
#
# 억제는 "알람을 안 보내는" 기능이라 잘못 걸리면 조용히 사고가 난다.
# 규칙이 없을 때 반드시 통과시키는 것, DB 가 없을 때 막지 않는 것을
# 특히 확인한다. 억제 못 해서 알람이 더 가는 것보다 판정 실패로
# 알람을 막는 쪽이 훨씬 위험하다.

import os

import pytest

from api.normalize_handler import check_suppression, normalize, send_alarm


@pytest.fixture
def no_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture
def rules_table(db_uri, monkeypatch):
    """규칙 테이블을 비우고 DATABASE_URL 을 진짜 DB 로 맞춘다."""
    import psycopg

    monkeypatch.setenv("DATABASE_URL", db_uri)
    with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.alarm_rules')")
        if cur.fetchone()[0] is None:
            pytest.skip("alarm_rules 테이블이 없습니다 (init-db 필요)")
        cur.execute("DELETE FROM alarm_rules WHERE fingerprint LIKE 'test-%'")
        cur.execute("DELETE FROM alarm_state WHERE fingerprint LIKE 'test-%'")
    yield db_uri
    with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM alarm_rules WHERE fingerprint LIKE 'test-%'")
        cur.execute("DELETE FROM alarm_state WHERE fingerprint LIKE 'test-%'")


class TestWithoutDatabase:
    def test_passes_when_no_database(self, no_database):
        """DB 가 없으면 판정하지 않고 통과시킨다."""
        allowed, reason = check_suppression("아무지문")
        assert allowed is True

    def test_send_alarm_still_skips_low_severity(self, no_database):
        record = normalize({"msg": "x", "level": "info"})
        assert send_alarm(record)["alarmed"] is False


@pytest.mark.db
class TestWithDatabase:
    def test_passes_when_no_rule(self, rules_table):
        allowed, _ = check_suppression("test-no-rule")
        assert allowed is True

    def test_muted_blocks(self, rules_table):
        import psycopg

        with psycopg.connect(rules_table) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alarm_rules (fingerprint, muted) VALUES (%s, true)",
                ("test-muted",),
            )
        allowed, reason = check_suppression("test-muted")
        assert allowed is False
        assert "muted" in reason

    def test_window_blocks_second_alarm(self, rules_table):
        import psycopg

        with psycopg.connect(rules_table) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alarm_rules (fingerprint, window_minutes) VALUES (%s, 30)",
                ("test-window",),
            )
            # 방금 보낸 것으로 기록해 둔다.
            cur.execute(
                "INSERT INTO alarm_state (fingerprint, last_alarmed_at) VALUES (%s, now())",
                ("test-window",),
            )
        allowed, reason = check_suppression("test-window")
        assert allowed is False
        assert "30분" in reason

    def test_window_allows_after_expiry(self, rules_table):
        import psycopg

        with psycopg.connect(rules_table) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alarm_rules (fingerprint, window_minutes) VALUES (%s, 5)",
                ("test-expired",),
            )
            cur.execute(
                "INSERT INTO alarm_state (fingerprint, last_alarmed_at) "
                "VALUES (%s, now() - interval '10 minutes')",
                ("test-expired",),
            )
        allowed, _ = check_suppression("test-expired")
        assert allowed is True

    def test_zero_window_does_not_suppress(self, rules_table):
        import psycopg

        with psycopg.connect(rules_table) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alarm_rules (fingerprint, window_minutes) VALUES (%s, 0)",
                ("test-zero",),
            )
            cur.execute(
                "INSERT INTO alarm_state (fingerprint, last_alarmed_at) VALUES (%s, now())",
                ("test-zero",),
            )
        allowed, _ = check_suppression("test-zero")
        assert allowed is True

    def test_suppressed_count_increases(self, rules_table):
        import psycopg

        with psycopg.connect(rules_table) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alarm_rules (fingerprint, muted) VALUES (%s, true)",
                ("test-count",),
            )
        check_suppression("test-count")
        check_suppression("test-count")

        with psycopg.connect(rules_table) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT suppressed_count FROM alarm_state WHERE fingerprint = %s",
                ("test-count",),
            )
            assert cur.fetchone()[0] == 2
