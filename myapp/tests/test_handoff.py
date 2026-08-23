# tests/test_handoff.py
# 알람에서 작업·장애 만들기.
#
# 예전에는 알람을 보다가 작업이나 보고서를 만들려면 화면을 떠나 빈 폼을
# 처음부터 채워야 했다. 새벽에 12자리 계정 번호를 눈으로 찾아 고르고
# 발생 시각을 datetime 칸에 손으로 옮겨 적는 구조였다.
#
# 여기서 지키는 것:
#   1. 넘기는 값은 전부 이벤트에 이미 있는 것이다 (새 사실을 만들지 않는다)
#   2. 주소창에 아무 값이나 넣어도 폼에 들어가지 않는다
#   3. 장애 쪽 지문 연결만은 새 사실이다 - 사람이 지목했다는 것

from datetime import datetime, timezone

import pytest

from app import handoff


def record(**over):
    base = {
        "event_id": "e1",
        "message": "커넥션 풀 150% 점유",
        "severity": "critical",
        "source": "pay-api",
        "account_id": "111122223333",
        "fingerprint": "abc123",
        "occurred_at": datetime(2026, 8, 21, 3, 46, 9, tzinfo=timezone.utc),
    }
    base.update(over)
    return base


class TestWork:
    def test_carries_what_the_form_needs(self):
        got = handoff.for_work(record())
        assert set(got) == set(handoff.WORK_KEYS)
        assert got["title"] == "커넥션 풀 150% 점유"
        assert got["account_id"] == "111122223333"

    def test_request_says_it_came_from_an_alarm(self):
        """고객 요청이 아니라 알람에서 시작된 작업이라는 것이 증적에 남아야 한다."""
        got = handoff.for_work(record())
        assert "알람에서 시작된 작업" in got["request"]
        assert "critical" in got["request"]
        assert "커넥션 풀 150% 점유" in got["request"]

    def test_region_is_left_to_the_account(self):
        """이벤트에는 리전이 없다. 없는 값을 지어내지 않는다."""
        assert handoff.for_work(record())["region"] == ""


class TestIncident:
    def test_carries_what_the_form_needs(self):
        got = handoff.for_incident(record())
        assert set(got) == set(handoff.INCIDENT_KEYS)
        assert got["severity"] == "critical"
        assert got["sources"] == "pay-api"
        assert got["fingerprint"] == "abc123"

    def test_started_at_matches_the_datetime_local_format(self):
        """형식이 어긋나면 브라우저가 조용히 빈 칸으로 만든다."""
        assert handoff.for_incident(record())["started_at"] == "2026-08-21T03:46"

    def test_no_time_zone_shifting(self):
        """이벤트도 폼도 UTC 다. 여기서 바꾸면 그 약속이 깨진다."""
        got = handoff.for_incident(record())
        assert got["started_at"].startswith("2026-08-21T03:46")

    def test_missing_time_is_blank_not_an_error(self):
        assert handoff.for_incident(record(occurred_at=None))["started_at"] == ""


class TestTitle:
    def test_long_message_is_cut(self):
        """제목 칸에 로그 한 줄이 통째로 들어가면 목록에서 아무것도 못 읽는다."""
        got = handoff.for_work(record(message="가" * 200))
        assert len(got["title"]) == handoff.TITLE_MAX + 1     # 말줄임표 한 자
        assert got["title"].endswith("…")

    def test_only_the_first_line(self):
        got = handoff.for_work(record(message="첫 줄\n둘째 줄\n셋째 줄"))
        assert got["title"] == "첫 줄"

    def test_empty_message(self):
        assert handoff.for_work(record(message=""))["title"] == "(내용 없음)"


class TestTake:
    def test_only_known_keys_pass(self):
        """주소창에 아무 값이나 넣어도 폼에 들어가면 안 된다."""
        args = {"title": "제목", "정체불명": "값", "operator": "관리자"}
        got = handoff.take(args, handoff.WORK_KEYS)
        assert set(got) == set(handoff.WORK_KEYS)
        assert "정체불명" not in got
        assert "operator" not in got

    def test_missing_keys_become_empty(self):
        got = handoff.take({}, handoff.INCIDENT_KEYS)
        assert all(v == "" for v in got.values())


@pytest.mark.db
class TestRoutes:
    def test_alarm_page_offers_both_links(self, db_client):
        body = db_client.get("/alarm/").get_data(as_text=True)
        assert "작업 만들기" in body
        assert "사후 보고서 만들기" in body

    def test_work_form_is_prefilled(self, db_client):
        body = db_client.get(
            "/work/?title=끌어온제목&account_id=111122223333&event_id=e1"
        ).get_data(as_text=True)
        assert 'value="끌어온제목"' in body
        assert "알람에서 넘어왔습니다" in body

    def test_incident_form_is_prefilled(self, db_client):
        body = db_client.get(
            "/incident/?title=끌어온제목&fingerprint=abc123&event_id=e1"
            "&started_at=2026-08-21T03:46&sources=pay-api"
        ).get_data(as_text=True)
        assert 'value="끌어온제목"' in body
        assert 'name="fingerprint" value="abc123"' in body
        assert 'value="2026-08-21T03:46"' in body

    def test_no_banner_without_an_event(self, db_client):
        """직접 들어왔을 때 '알람에서 넘어왔습니다' 가 뜨면 거짓말이다."""
        body = db_client.get("/incident/").get_data(as_text=True)
        assert "알람에서 넘어왔습니다" not in body


@pytest.mark.db
class TestOriginLink:
    """사람이 지목한 연결과 시간 겹침 추론을 구분한다."""

    @pytest.fixture
    def made(self, db_app, db_uri):
        import psycopg
        from app import incident

        with db_app.app_context():
            iid = incident.create(
                title="시험 장애",
                started_at=datetime.now(timezone.utc),
                author="시험",
            )
        yield iid
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM incidents WHERE id = %s", (iid,))

    def test_link_origin_marks_it(self, db_app, made):
        from app import incident

        with db_app.app_context():
            incident.link_origin(made, "abc123", "예시 메시지")
            assert incident.origin_fingerprints(made) == {"abc123"}

    def test_blank_fingerprint_is_ignored(self, db_app, made):
        from app import incident

        with db_app.app_context():
            assert incident.link_origin(made, "") is False
            assert incident.origin_fingerprints(made) == set()

    def test_batch_does_not_clear_the_mark(self, db_app, db_uri, made):
        """사람이 지목한 사실이 배치가 한 번 더 돌았다고 사라지면 안 된다."""
        import psycopg
        from app import incident

        with db_app.app_context():
            incident.link_origin(made, "abc123")
        # link_fingerprints 가 같은 지문을 다시 이어붙이는 상황
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incident_fingerprints
                    (incident_id, fingerprint, event_count, sample)
                VALUES (%s, 'abc123', 42, '나중에 센 것')
                ON CONFLICT (incident_id, fingerprint) DO UPDATE
                    SET event_count = EXCLUDED.event_count,
                        sample = EXCLUDED.sample
                """,
                (made,),
            )
        with db_app.app_context():
            assert incident.origin_fingerprints(made) == {"abc123"}
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("SELECT event_count FROM incident_fingerprints "
                        " WHERE incident_id = %s", (made,))
            assert cur.fetchone()[0] == 42      # 건수는 배치가 채운다

    def test_inferred_links_are_not_origins(self, db_app, db_uri, made):
        import psycopg
        from app import incident

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO incident_fingerprints "
                " (incident_id, fingerprint, event_count) VALUES (%s, 'zzz', 3)",
                (made,),
            )
        with db_app.app_context():
            assert incident.origin_fingerprints(made) == set()

    def test_create_from_alarm_links_immediately(self, db_client, db_app, db_uri):
        import psycopg

        r = db_client.post("/incident/new", data={
            "title": "알람에서 만든 보고서",
            "started_at": "2026-08-21T03:00",
            "severity": "error",
            "fingerprint": "fromalarm",
        })
        iid = int(r.headers["Location"].rstrip("/").split("/")[-1])
        try:
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT fingerprint, origin FROM incident_fingerprints "
                    " WHERE incident_id = %s", (iid,))
                assert cur.fetchall() == [("fromalarm", True)]
        finally:
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM incidents WHERE id = %s", (iid,))
