# tests/test_ack.py
# 알람 확인(ack).
#
# 이 값 하나가 SLA·MSR·리포트의 근거가 된다. 예전에는 감사 로그에서
# 추론했는데, 그건 "그 계정을 들여다봤다" 이지 "이 알람을 처리했다" 가
# 아니었다. 두 근거를 함께 쓰되 어느 쪽인지 세는 것이 핵심이다.

import pytest

from app import event_store


@pytest.mark.db
class TestAcknowledge:
    @pytest.fixture
    def one(self, db_app, db_uri):
        """확인 상태를 만졌다가 되돌려 놓는다. 개발 DB 를 더럽히지 않는다."""
        import psycopg

        with db_app.app_context():
            row = event_store.recent(1)
            if not row:
                pytest.skip("이벤트가 없습니다")
            event_id = row[0]["record"]["event_id"]

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT acknowledged_at, acknowledged_by FROM events WHERE event_id = %s",
                (event_id,),
            )
            before = cur.fetchone()
        yield event_id
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE events SET acknowledged_at = %s, acknowledged_by = %s "
                " WHERE event_id = %s",
                (before[0], before[1], event_id),
            )

    def test_round_trip(self, db_app, one):
        with db_app.app_context():
            event_store.unacknowledge(one)
            event_store.acknowledge(one, "김당직")
            record = event_store.get(one)["record"]
            assert record["acknowledged_by"] == "김당직"
            assert record["acknowledged_at"] is not None

    def test_first_person_wins(self, db_app, one):
        """나중 사람이 덮어쓰면 최초 대응 시각이 뒤로 밀린다."""
        with db_app.app_context():
            event_store.unacknowledge(one)
            event_store.acknowledge(one, "먼저본사람")
            with pytest.raises(event_store.EventStoreError) as e:
                event_store.acknowledge(one, "나중사람")
            assert "먼저본사람" in str(e.value)
            assert event_store.get(one)["record"]["acknowledged_by"] == "먼저본사람"

    def test_unacknowledge(self, db_app, one):
        with db_app.app_context():
            event_store.acknowledge(one, "누구")
            event_store.unacknowledge(one)
            assert event_store.get(one)["record"]["acknowledged_at"] is None

    def test_unknown_event(self, db_app, db_uri):
        with db_app.app_context():
            with pytest.raises(event_store.EventStoreError):
                event_store.acknowledge("없는이벤트", "누구")

    def test_unacked_filter_excludes_acked(self, db_app, one):
        with db_app.app_context():
            event_store.unacknowledge(one)
            assert one in [e["record"]["event_id"]
                           for e in event_store.recent(50, unacked_only=True)]
            event_store.acknowledge(one, "누구")
            assert one not in [e["record"]["event_id"]
                               for e in event_store.recent(50, unacked_only=True)]

    def test_severity_filter(self, db_app, db_uri):
        with db_app.app_context():
            rows = event_store.recent(20, severity="critical")
            assert all(e["record"]["severity"] == "critical" for e in rows)

    def test_unacked_count_splits_by_severity(self, db_app, db_uri):
        """info 100건보다 critical 1건이 급하다."""
        with db_app.app_context():
            counts = event_store.unacked_count()
            assert counts["total"] == sum(counts["by_severity"].values())


@pytest.mark.db
class TestSlaUsesAck:
    """SLA 가 확인 기록을 우선 쓰고, 추론과 구분해서 센다."""

    def test_measure_reports_both_sources(self, db_app, db_uri):
        from app import sla
        from app.customer import names

        with db_app.app_context():
            customers = names()
            if not customers:
                pytest.skip("고객사가 없습니다")
            m = sla.measure(customers[0], days=365)
            assert m["acked"] + m["inferred"] == m["measured"]

    def test_ack_moves_a_row_from_inferred_to_acked(self, db_app, db_uri):
        """확인 버튼을 누르면 그만큼 추론이 줄어야 한다."""
        import psycopg

        from app import sla
        from app.customer import names

        with db_app.app_context():
            customers = names()
            if not customers:
                pytest.skip("고객사가 없습니다")
            customer = customers[0]
            before = sla.measure(customer, days=365)

            # 이 고객사 계정의 미확인 이벤트 하나를 고른다.
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT e.event_id FROM events e "
                    "  JOIN aws_accounts a ON a.account_id = e.account_id "
                    " WHERE a.customer = %s AND e.acknowledged_at IS NULL LIMIT 1",
                    (customer,),
                )
                row = cur.fetchone()
            if row is None:
                pytest.skip("미확인 이벤트가 없습니다")

            try:
                event_store.acknowledge(row[0], "테스터")
                after = sla.measure(customer, days=365)
                assert after["acked"] == before["acked"] + 1
            finally:
                event_store.unacknowledge(row[0])


@pytest.mark.db
class TestRoutes:
    def test_ack_button_appears(self, db_client):
        body = db_client.get("/alarm/").get_data(as_text=True)
        assert "/ack" in body

    def test_unacked_view(self, db_client):
        assert db_client.get("/alarm/?unacked=1").status_code == 200

    def test_bad_severity_is_ignored_not_injected(self, db_client):
        """허용 목록 밖의 값은 조용히 무시한다. SQL 로는 어차피 %s 로 나가지만,
        알 수 없는 값으로 빈 화면을 내는 것보다 무시가 낫다."""
        r = db_client.get("/alarm/?severity=' OR 1=1 --")
        assert r.status_code == 200

    def test_ack_requires_login(self, db_app, db_uri):
        r = db_app.test_client().post("/alarm/없는것/ack")
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]
