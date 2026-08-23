# tests/test_demo.py
# 샘플 데이터. 넣을 수 있고, 지울 수 있고, 진짜와 안 섞이는가.
#
# ── 왜 만들었나 ────────────────────────────────────────────────────
# 표 25개 중 18개가 비어 있었고, 비어 있는 쪽은 전부 '사람이 채우는 것'
# 이었다. 기계가 채우는 쪽(events, resources)에는 명령이 있었는데 사람이
# 채우는 쪽에는 하나도 없었다. 그래서 로컬에서 화면을 열면 절반이
# "아직 없습니다" 였고, 그 화면들이 제대로 도는지 볼 수 없었다.
#
# ── 여기서 제일 조심하는 것 ─────────────────────────────────────────
# 샘플과 진짜가 섞이는 것. 섞이면 나중에 못 가른다. 그리고 이 도구의
# 산출물은 고객사에 나가는 문서다 - 가짜가 섞이면 그게 나간다.

import pytest

from app import demo


class TestEverythingIsMarked:
    """표시가 없으면 지울 수 없고, 지울 수 없으면 섞인다."""

    def test_incidents_are_marked(self):
        for spec in demo.INCIDENTS:
            assert demo.MARK in spec["title"]

    def test_work_orders_are_marked(self):
        for spec in demo.WORK_ORDERS:
            assert demo.MARK in spec["title"]

    def test_oncall_is_marked(self):
        for name, _, _ in demo.ONCALL:
            assert demo.MARK in name

    def test_standards_are_marked_in_the_note(self):
        """구성 표준은 값이 진짜 설정이라 이름에 표시를 못 넣는다.

        넣으면 컴플라이언스가 '(샘플)' 이 붙은 태그를 요구하게 된다.
        """
        for rule, value, note in demo.STANDARDS:
            assert demo.MARK in note
            assert demo.MARK not in rule
            assert demo.MARK not in value

    def test_mark_is_visible_to_a_human(self):
        """화면에서 보고 '이건 샘플이구나' 를 알 수 있어야 한다."""
        assert demo.MARK.strip()
        assert not demo.MARK.isspace()


class TestCleanupCoversWhatSeedingCreates:
    """넣는 표와 지우는 표가 어긋나면 찌꺼기가 남는다."""

    SEEDS = {
        "oncall_members", "customer_contacts", "customer_routines",
        "customer_standards", "runbooks", "alarm_rules", "incidents",
        "work_orders", "deliveries",
    }

    def test_every_seeded_table_has_a_cleanup_rule(self):
        cleaned = {table for table, _ in demo.CLEAN}
        missing = self.SEEDS - cleaned
        assert not missing, f"지우는 규칙이 없습니다: {sorted(missing)}"

    def test_children_are_cleaned_before_parents(self):
        """자식을 나중에 지우면 외래키에 걸려 부모가 안 지워진다."""
        order = [table for table, _ in demo.CLEAN]
        for child, parent in (("runbook_runs", "runbooks"),
                              ("routine_runs", "customer_routines"),
                              ("incident_fingerprints", "incidents")):
            assert order.index(child) < order.index(parent), \
                f"{child} 를 {parent} 보다 먼저 지워야 합니다"

    def test_cleanup_is_always_scoped_by_the_mark(self):
        """조건 없는 DELETE 가 하나라도 있으면 사람이 쓴 것을 지운다."""
        for table, where in demo.CLEAN:
            assert "%s" in where, f"{table} 의 조건에 표시가 안 들어갔습니다"


@pytest.mark.db
class TestRoundTrip:
    @pytest.fixture
    def seeded(self, db_app, db_uri):
        """넣었다가 지운다. 개발 DB 에 찌꺼기를 남기지 않는다."""
        with db_app.app_context():
            demo.clear(echo=lambda *a: None)
            made = demo.seed(echo=lambda *a: None)
        yield made
        with db_app.app_context():
            demo.clear(echo=lambda *a: None)

    def test_something_gets_created(self, seeded):
        assert sum(seeded.values()) > 0

    def test_clear_removes_everything_it_made(self, db_app, db_uri, seeded):
        import psycopg

        with db_app.app_context():
            demo.clear(echo=lambda *a: None)

        pattern = f"%{demo.MARK}%"
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            for table, where in demo.CLEAN:
                cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                if cur.fetchone()[0] is None:
                    continue
                cur.execute(f"SELECT count(*) FROM {table} WHERE {where}",
                            (pattern,))
                assert cur.fetchone()[0] == 0, f"{table} 에 샘플이 남았습니다"

    def test_clear_leaves_unmarked_rows_alone(self, db_app, db_uri, seeded):
        """손으로 쓴 것을 지우면 안 된다. 이게 제일 위험한 실패다."""
        import psycopg

        from app import runbook

        with db_app.app_context():
            mine = runbook.save(fingerprint="zzq_사람이쓴것", title="손으로 쓴 절차",
                                body="지워지면 안 됩니다", author="사람")
            demo.clear(echo=lambda *a: None)
            survived = runbook.find("zzq_사람이쓴것")

        assert survived is not None, "사람이 쓴 절차가 지워졌습니다"
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM runbooks WHERE id = %s", (mine,))

    def test_seeding_twice_does_not_explode(self, db_app, db_uri, seeded):
        with db_app.app_context():
            demo.seed(echo=lambda *a: None)


@pytest.mark.db
class TestItAnchorsToRealData:
    """지어낸 값으로 채우면 화면은 차지만 연결이 끊겨 있다."""

    @pytest.fixture
    def seeded(self, db_app, db_uri):
        with db_app.app_context():
            demo.clear(echo=lambda *a: None)
            demo.seed(echo=lambda *a: None)
        yield
        with db_app.app_context():
            demo.clear(echo=lambda *a: None)

    def test_runbooks_attach_to_fingerprints_that_exist(self, db_app, db_uri, seeded):
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM runbooks r "
                " WHERE r.author LIKE %s "
                "   AND NOT EXISTS (SELECT 1 FROM events e "
                "                    WHERE e.fingerprint = r.fingerprint)",
                (f"%{demo.MARK}%",),
            )
            assert cur.fetchone()[0] == 0, "이벤트가 없는 지문에 절차를 붙였습니다"

    def test_work_orders_use_registered_accounts(self, db_app, db_uri, seeded):
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM work_orders w "
                " WHERE w.title LIKE %s "
                "   AND w.account_id NOT IN (SELECT account_id FROM aws_accounts)",
                (f"%{demo.MARK}%",),
            )
            assert cur.fetchone()[0] == 0

    def test_coverage_is_left_incomplete_on_purpose(self, db_app, db_uri, seeded):
        """전부 채우면 '아직 비어 있는 것' 을 보여주는 화면이 무슨 일을
        하는지 안 보인다."""
        from app import noise

        with db_app.app_context():
            rows = noise.advise(noise.ranking(24 * 60), {})
            counts = noise.coverage_summary(rows)
        assert counts["covered"] > 0, "절차가 하나도 안 붙었습니다"
        assert counts["gap"] > 0, "빈 자리가 하나도 없으면 커버리지 화면이 죽습니다"


@pytest.mark.db
class TestScreensSurviveIt:
    @pytest.fixture
    def seeded(self, db_app, db_uri):
        with db_app.app_context():
            demo.clear(echo=lambda *a: None)
            demo.seed(echo=lambda *a: None)
        yield
        with db_app.app_context():
            demo.clear(echo=lambda *a: None)

    def test_every_menu_page_still_renders(self, db_app, db_uri, seeded):
        """샘플을 넣고 나서 500 이 나면 로컬 테스트가 거기서 멈춘다."""
        from flask import url_for

        from app import nav

        db_app.config["WTF_CSRF_ENABLED"] = False
        client = db_app.test_client()
        client.post("/auth/login", data={"username": "admin", "password": "1234"})

        known = {r.endpoint for r in db_app.url_map.iter_rules()}
        broken = []
        for group in nav.menu("admin", known):
            for item in group["items"]:
                with db_app.test_request_context():
                    url = url_for(item["endpoint"])
                if client.get(url).status_code != 200:
                    broken.append((item["label"], url))
        assert not broken, f"열리지 않는 화면: {broken}"
