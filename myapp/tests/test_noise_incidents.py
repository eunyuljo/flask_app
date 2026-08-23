# tests/test_noise_incidents.py
# 알람이 실제 장애의 신호였던 적이 있는가.
#
# 순위표는 '얼마나 시끄러운가' 까지만 말한다. 억제할지 정하려면 다른
# 물음이 필요하다. 500번 났어도 한 번도 장애로 이어지지 않았다면 억제를
# 검토할 만하고, 20번 났는데 그중 한 번이 결제 장애였다면 절대 억제하면
# 안 된다.
#
# 가장 중요한 것: 장애로 이어졌던 알람이 muted 되어 있는 경우.
# 다음 장애의 첫 신호를 우리가 스스로 껐다는 뜻이다.

import pytest

from app import noise


def row(fingerprint="a", count=100, muted=False, window=None):
    return {"fingerprint": fingerprint, "c": count, "muted": muted,
            "window_minutes": window, "sample": "예시"}


def link(incidents=1, origin=0):
    return {"incidents": incidents, "published": incidents, "origin": origin,
            "last_at": None, "last_title": "결제 장애", "last_id": 7}


def verdict(rows, links, fingerprint):
    return next(r["verdict"] for r in noise.advise(rows, links)
                if r["fingerprint"] == fingerprint)


class TestAdvise:
    def test_muted_alarm_that_caused_an_incident_is_risky(self):
        """이 한 줄이 이 기능을 만든 이유다."""
        got = noise.advise([row(muted=True)], {"a": link(incidents=2)})[0]
        assert got["verdict"] == "risky"
        assert "껐을 수 있습니다" in got["why"]

    def test_incident_linked_alarm_is_kept(self):
        got = noise.advise([row()], {"a": link()})[0]
        assert got["verdict"] == "keep"
        assert "억제 대상이 아닙니다" in got["why"]

    def test_origin_links_are_called_out(self):
        """사람이 지목한 것과 시간이 겹쳐 딸려온 것은 근거의 무게가 다르다."""
        got = noise.advise([row()], {"a": link(incidents=3, origin=2)})[0]
        assert "사람이 지목한 것 2건" in got["why"]

    def test_noisy_without_incidents_is_a_suppression_candidate(self):
        got = noise.advise([row(count=500)], {})[0]
        assert got["verdict"] == "suppress"

    def test_quiet_alarm_is_not_a_candidate(self):
        """3번 난 것을 시끄럽다고 하면 목록이 의미 없어진다."""
        assert verdict([row(count=3)], {}, "a") is None

    def test_threshold_is_inclusive(self):
        assert verdict([row(count=noise.NOISY_ENOUGH)], {}, "a") == "suppress"
        assert verdict([row(count=noise.NOISY_ENOUGH - 1)], {}, "a") is None

    def test_already_suppressed_is_not_suggested_again(self):
        assert verdict([row(count=500, window=10)], {}, "a") is None

    def test_risky_sorts_first(self):
        """아래로 스크롤해야 보이면 안 본다."""
        rows = [row("quiet", 3), row("noisy", 900), row("danger", 30, muted=True)]
        got = noise.advise(rows, {"danger": link()})
        assert got[0]["fingerprint"] == "danger"

    def test_suppress_before_keep(self):
        rows = [row("keepme", 500), row("suppressme", 500)]
        got = noise.advise(rows, {"keepme": link()})
        assert [r["fingerprint"] for r in got] == ["suppressme", "keepme"]

    def test_link_is_attached_for_the_screen(self):
        got = noise.advise([row()], {"a": link()})[0]
        assert got["link"]["last_title"] == "결제 장애"

    def test_no_links_at_all(self):
        """장애 이력을 못 읽어도 순위표는 나와야 한다."""
        got = noise.advise([row(count=3)], {})
        assert len(got) == 1
        assert got[0]["link"] is None


class TestSummary:
    def test_counts(self):
        rows = [row("a", 500), row("b", 500, muted=True), row("c", 3)]
        got = noise.advice_summary(noise.advise(rows, {"b": link()}))
        assert got == {"risky": 1, "suppress": 1, "keep": 0, "linked": 1}

    def test_linked_distinguishes_unknown_from_zero(self):
        """이력이 없다는 것과 못 읽었다는 것은 다르다."""
        assert noise.advice_summary(noise.advise([row()], {}))["linked"] == 0


@pytest.mark.db
class TestIncidentLinks:
    @pytest.fixture
    def linked(self, db_app, db_uri):
        import psycopg
        from datetime import datetime, timezone

        from app import incident

        with db_app.app_context():
            iid = incident.create(title="시험 장애",
                                  started_at=datetime.now(timezone.utc),
                                  author="시험")
            incident.link_origin(iid, "시험지문", "예시 메시지")
        yield "시험지문"
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM incidents WHERE title = %s", ("시험 장애",))

    def test_finds_the_link(self, db_app, linked):
        with db_app.app_context():
            got = noise.incident_links([linked])
        assert got[linked]["incidents"] == 1
        assert got[linked]["origin"] == 1
        assert got[linked]["last_title"] == "시험 장애"

    def test_unlinked_fingerprint_is_absent(self, db_app, linked):
        """0 건짜리 행을 만들지 않는다. 없는 것과 0 인 것을 구분할 수 있어야 한다."""
        with db_app.app_context():
            got = noise.incident_links(["아무것도아닌지문"])
        assert got == {}

    def test_no_filter_returns_everything(self, db_app, linked):
        with db_app.app_context():
            assert linked in noise.incident_links()

    def test_not_windowed_by_time(self, db_app, db_uri):
        """반년 전에 장애를 냈던 알람도 여전히 장애를 낼 수 있는 알람이다."""
        import inspect

        source = inspect.getsource(noise.incident_links)
        assert "make_interval" not in source
        assert "occurred_at >=" not in source


@pytest.mark.db
class TestRoutes:
    def test_page_still_renders(self, db_client):
        assert db_client.get("/noise/").status_code == 200

    def test_page_says_what_no_history_means(self, db_client):
        body = db_client.get("/noise/").get_data(as_text=True)
        # 이력 없음과 '아직 보고서를 안 씀' 을 구분해 준다.
        assert "사후 보고서를 안 썼다는 것은 다릅니다" in body
