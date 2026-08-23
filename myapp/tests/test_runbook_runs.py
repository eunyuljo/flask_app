# tests/test_runbook_runs.py
# 런북 실행 기록.
#
# runbooks 는 "이럴 땐 이렇게 한다" 는 규칙이고, runbook_runs 는 "실제로
# 그렇게 했고 결과가 이랬다" 는 실행 상태다. 여기서 지키려는 것은 두 가지다.
#   1. 런북을 지워도 "따랐다" 는 사실은 남는다
#   2. 실패·불일치는 이유 없이 저장되지 않는다 (그래야 절차를 고칠 수 있다)

import pytest

from app import runbook
from app.runbook import RunbookError


class TestValidation:
    """DB 없이 걸러지는 것들. 연결하기 전에 판정한다."""

    def test_unknown_outcome(self):
        with pytest.raises(RunbookError) as e:
            runbook.record_run(1, "무언가", "나")
        assert "알 수 없는 결과" in str(e.value)

    @pytest.mark.parametrize("outcome", ["failed", "stale"])
    def test_note_required_for_trouble(self, outcome):
        """절차가 안 통했다는 기록은 이유가 없으면 쓸모가 없다."""
        with pytest.raises(RunbookError) as e:
            runbook.record_run(1, outcome, "나", note="   ")
        assert "적어야 저장됩니다" in str(e.value)

    def test_minutes_must_be_number(self):
        with pytest.raises(RunbookError) as e:
            runbook.record_run(1, "resolved", "나", minutes="열다섯")
        assert "숫자" in str(e.value)

    def test_outcomes_cover_needs_note(self):
        """메모 필수 목록이 결과 목록 밖으로 벗어나지 않게."""
        assert set(runbook.NEEDS_NOTE) <= set(runbook.OUTCOMES)


@pytest.mark.db
class TestRecord:
    @pytest.fixture
    def book(self, db_app, db_uri):
        """시험용 런북 하나. 끝나면 실행 기록까지 지운다."""
        import psycopg

        fingerprint = "test-run-fp"
        with db_app.app_context():
            book_id = runbook.save(
                fingerprint, "시험용 절차", "1. 아무것도 하지 않는다", "테스트"
            )
        yield {"id": book_id, "fingerprint": fingerprint}
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM runbook_runs WHERE fingerprint = %s",
                        (fingerprint,))
            cur.execute("DELETE FROM runbooks WHERE fingerprint = %s", (fingerprint,))

    def test_record_and_read(self, db_app, book):
        with db_app.app_context():
            runbook.record_run(book["id"], "resolved", "김당직",
                               minutes=15, account_id="111122223333")
            rows = runbook.runs(book["id"], book["fingerprint"])

        assert len(rows) == 1
        assert rows[0]["outcome"] == "resolved"
        assert rows[0]["ran_by"] == "김당직"
        assert rows[0]["minutes"] == 15
        # 지문과 제목은 호출한 쪽이 아니라 런북에서 복사해 온다.
        assert rows[0]["fingerprint"] == book["fingerprint"]
        assert rows[0]["title"] == "시험용 절차"

    def test_survives_runbook_delete(self, db_app, book):
        """절차가 사라졌다고 '그때 이 절차를 따랐다' 가 없던 일이 되지 않는다."""
        with db_app.app_context():
            runbook.record_run(book["id"], "resolved", "김당직")
            runbook.delete(book["id"])
            rows = runbook.runs(fingerprint=book["fingerprint"])

        assert len(rows) == 1
        assert rows[0]["runbook_id"] is None   # ON DELETE SET NULL
        assert rows[0]["title"] == "시험용 절차"   # 그래도 무엇이었는지는 안다

    def test_unknown_runbook(self, db_app, db_uri):
        with db_app.app_context():
            with pytest.raises(RunbookError) as e:
                runbook.record_run(-1, "resolved", "나")
        assert "찾지 못했습니다" in str(e.value)

    def test_usage_omits_unused(self, db_app, book):
        """한 번도 안 쓰인 런북은 키가 없다. 0 건과 구분되어야 한다."""
        with db_app.app_context():
            assert book["id"] not in runbook.usage([book["id"]])

            runbook.record_run(book["id"], "resolved", "나")
            runbook.record_run(book["id"], "stale", "나", note="명령어가 바뀌었다")
            used = runbook.usage([book["id"]])[book["id"]]

        assert used["total"] == 2
        assert used["resolved"] == 1
        assert used["trouble"] == 1
        assert used["last_at"] is not None

    def test_summary_lists_broken_runbook(self, db_app, book):
        with db_app.app_context():
            runbook.record_run(book["id"], "failed", "나", note="재시작해도 그대로")
            stats = runbook.summary()

        mine = [f for f in stats["needs_fix"]
                if f["fingerprint"] == book["fingerprint"]]
        assert mine, "실패한 절차가 '손봐야 할 절차' 에 없습니다"
        assert mine[0]["last_note"] == "재시작해도 그대로"


@pytest.mark.db
class TestRoutes:
    def test_runs_page(self, db_client):
        assert db_client.get("/runbook/runs").status_code == 200

    def test_index_still_renders(self, db_client):
        """목록 화면이 사용 현황을 함께 읽는다."""
        assert db_client.get("/runbook/").status_code == 200

    def test_run_rejects_bad_outcome(self, db_client):
        """저장되지 않아도 화면은 돌아가야 한다."""
        r = db_client.post("/runbook/1/run", data={"outcome": ""},
                           follow_redirects=True)
        assert r.status_code == 200

    @pytest.mark.parametrize("back", [
        "http://evil.example/",
        "//evil.example/",
        "https://evil.example/pwn",
    ])
    def test_back_stays_inside_app(self, db_client, back):
        """back 은 폼에 실려 오는 값이다. 바깥 주소로 튕기면 안 된다.

        결과를 일부러 비워 둔다. 저장은 어차피 거부되지만 돌아가는 곳은
        똑같이 정해지고, 그래야 이 테스트가 개발 DB 에 줄을 남기지 않는다.
        """
        r = db_client.post("/runbook/1/run",
                           data={"outcome": "", "back": back})
        assert r.status_code == 302
        assert "evil.example" not in r.headers["Location"]

    def test_back_keeps_internal_path(self, db_client):
        r = db_client.post("/runbook/1/run",
                           data={"outcome": "", "back": "/alarm/?unacked=1"})
        assert r.headers["Location"].endswith("/alarm/?unacked=1")
