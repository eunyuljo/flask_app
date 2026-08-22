# tests/test_incident_archive.py
# 사후 보고서를 진단 재료로 쓰는 부분 테스트.
#
# 핵심은 '무엇이 재료가 되지 않는가' 다. 원인이 비어 있거나 확정되지 않은
# 보고서가 재료로 나오면, 진단이 근거 없는 이야기를 물고 온다.

import pytest


@pytest.mark.db
class TestPastIncidents:
    @pytest.fixture
    def sample(self, db_app, db_uri):
        """확정/미확정/원인없음 세 가지 보고서를 만들어 둔다."""
        import psycopg

        made = []
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            for title, status, cause in [
                ("test-확정-원인있음", "published", "테스트 원인"),
                ("test-확정-원인없음", "published", ""),
                ("test-초안-원인있음", "draft", "테스트 원인"),
            ]:
                cur.execute(
                    """
                    INSERT INTO incidents (title, started_at, severity, author,
                                           status, cause, customer)
                    VALUES (%s, now() - interval '1 day', 'critical', 'tester',
                            %s, %s, 'test-고객사')
                    RETURNING id
                    """,
                    (title, status, cause),
                )
                incident_id = cur.fetchone()[0]
                made.append(incident_id)
                cur.execute(
                    "INSERT INTO incident_fingerprints "
                    "(incident_id, fingerprint, event_count, sample) "
                    "VALUES (%s, 'test-fp', 5, 'test 샘플')",
                    (incident_id,),
                )
        yield made
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM incidents WHERE title LIKE 'test-%'")

    def test_only_published_with_cause(self, db_app, sample):
        """확정됐고 원인이 적힌 것만 재료가 된다."""
        from app.incident import past_incidents

        with db_app.app_context():
            found = past_incidents("test-fp", limit=10)
        titles = {f["title"] for f in found}
        assert "test-확정-원인있음" in titles
        assert "test-확정-원인없음" not in titles, "원인이 없으면 재료가 아니다"
        assert "test-초안-원인있음" not in titles, "확정 전에는 재료가 아니다"

    def test_exclude_self(self, db_app, sample):
        """자기 자신을 '비슷한 지난 장애' 로 보여주면 안 된다."""
        from app.incident import past_incidents

        with db_app.app_context():
            first = past_incidents("test-fp", limit=10)[0]
            found = past_incidents("test-fp", limit=10, exclude_id=first["id"])
        assert first["id"] not in {f["id"] for f in found}

    def test_unknown_fingerprint_is_empty(self, db_app, sample):
        from app.incident import past_incidents

        with db_app.app_context():
            assert past_incidents("존재하지-않는-지문") == []

    def test_limit_is_respected(self, db_app, sample):
        from app.incident import past_incidents

        with db_app.app_context():
            assert len(past_incidents("test-fp", limit=1)) <= 1

    def test_past_for_many_batches(self, db_app, sample):
        from app.incident import past_for_many

        with db_app.app_context():
            got = past_for_many(["test-fp", "없는지문"])
        assert "test-fp" in got
        assert "없는지문" not in got

    def test_past_for_many_empty_input(self, db_app):
        from app.incident import past_for_many

        with db_app.app_context():
            assert past_for_many([]) == {}


@pytest.mark.db
class TestLinking:
    def test_link_is_idempotent(self, db_app, db_uri):
        """같은 보고서를 두 번 이어도 행이 늘지 않는다."""
        import psycopg
        from app.incident import link_fingerprints, recent

        with db_app.app_context():
            published = [i for i in recent(50) if i["status"] == "published"]
            if not published:
                pytest.skip("확정된 사후 보고서가 없습니다")
            incident_id = published[0]["id"]
            link_fingerprints(incident_id)

            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM incident_fingerprints WHERE incident_id = %s",
                    (incident_id,),
                )
                first = cur.fetchone()[0]

            link_fingerprints(incident_id)

            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM incident_fingerprints WHERE incident_id = %s",
                    (incident_id,),
                )
                second = cur.fetchone()[0]
        assert first == second

    def test_link_survives_event_deletion(self, db_app, db_uri):
        """이벤트를 지워도 연결은 남아야 한다.

        장애는 이벤트를 시간 범위로 조회하므로, 이 표가 없으면 보존 정책이
        돌 때 '어떤 알람의 장애였는지' 가 통째로 사라진다.
        """
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM information_schema.table_constraints tc
                  JOIN information_schema.constraint_column_usage ccu
                    ON tc.constraint_name = ccu.constraint_name
                 WHERE tc.table_name = 'incident_fingerprints'
                   AND tc.constraint_type = 'FOREIGN KEY'
                   AND ccu.table_name = 'events'
                """
            )
            assert cur.fetchone()[0] == 0, "events 를 참조하면 정리 때 함께 지워진다"


@pytest.mark.db
class TestPromptMaterial:
    def test_prompt_includes_past_cause(self, db_app, db_uri):
        from app.agent_core import _format_event
        from app.incident import past_incidents, recent

        with db_app.app_context():
            published = [i for i in recent(50)
                         if i["status"] == "published" and i["cause"].strip()]
            if not published:
                pytest.skip("원인이 적힌 확정 보고서가 없습니다")
            import psycopg
            from app.incident import psycopg_uri
            with psycopg.connect(psycopg_uri()) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT fingerprint FROM incident_fingerprints "
                    "WHERE incident_id = %s LIMIT 1", (published[0]["id"],)
                )
                row = cur.fetchone()
            if row is None:
                pytest.skip("연결된 지문이 없습니다")

            past = past_incidents(row[0], limit=2)
            event = {"severity": "critical", "source": "s", "event_type": "t",
                     "occurred_at": "2026-01-01T00:00:00Z", "message": "m",
                     "fingerprint": row[0], "meta": {}}
            text = _format_event(event, None, None, past)

        assert "같은 알람이 관련됐던 지난 장애" in text
        assert past[0]["cause"][:20] in text

    def test_prompt_omits_section_when_no_past(self, db_app):
        from app.agent_core import _format_event

        event = {"severity": "info", "source": "s", "event_type": "t",
                 "occurred_at": "2026-01-01T00:00:00Z", "message": "m",
                 "fingerprint": "x", "meta": {}}
        with db_app.app_context():
            text = _format_event(event, None, None, [])
        assert "지난 장애" not in text
