# tests/test_access.py
# 고객사 계정 접속 위생.
#
# MSP 가 고객사에 대해 가진 것 중 가장 민감한 것이 계정 접점인데,
# 그동안 아무도 안 보고 있었다. 여기서 지키는 것:
#   1. ExternalId 없는 실계정은 위험으로 잡힌다 (혼동된 대리인)
#   2. 데모 계정을 위험으로 만들지 않는다 (그러면 아무도 안 본다)
#   3. 확인한 적 없음 / 확인 실패 / 오래됨을 구분한다
#   4. 비밀 값은 화면에 찍지 않는다

from datetime import datetime, timedelta, timezone

import pytest

from app import access


def account(**kw):
    base = {"account_id": "111122223333", "customer": "가고객",
            "role_arn": "", "external_id": "", "regions": ["ap-northeast-2"],
            "enabled": True}
    base.update(kw)
    return base


def probe(ok=True, days_ago=0, detail=""):
    return {"ok": ok, "detail": detail,
            "at": datetime.now(timezone.utc) - timedelta(days=days_ago)}


def ids(items):
    return [f["id"] for f in items]


class TestFindings:
    def test_real_account_without_external_id_is_dangerous(self):
        """이 항목 하나가 이 화면을 만든 이유다."""
        items = access.findings(account(role_arn="arn:...:role/R"), probe())
        assert "no-external-id" in ids(items)
        assert [f for f in items if f["id"] == "no-external-id"][0]["level"] == "danger"

    def test_external_id_present_is_clean(self):
        items = access.findings(
            account(role_arn="arn:...:role/R", external_id="비밀"), probe())
        assert ids(items) == []

    def test_demo_account_is_information_not_danger(self):
        """데모 계정을 위험으로 칠하면 진짜 위험이 그 속에 묻힌다."""
        items = access.findings(account(), None)
        assert ids(items) == ["demo-account"]
        assert items[0]["level"] == "info"

    def test_demo_account_is_not_asked_to_be_probed(self):
        """확인할 역할이 없는데 '확인한 적 없음' 이라고 하면 고칠 방법이 없다."""
        assert "never-probed" not in ids(access.findings(account(), None))

    def test_never_probed(self):
        items = access.findings(account(role_arn="arn:...", external_id="x"), None)
        assert ids(items) == ["never-probed"]

    def test_failed_probe_shows_the_reason(self):
        items = access.findings(
            account(role_arn="arn:...", external_id="x"),
            probe(ok=False, detail="AssumeRole 에 실패했습니다"))
        found = [f for f in items if f["id"] == "probe-failed"][0]
        assert found["level"] == "danger"
        assert "AssumeRole" in found["detail"]

    def test_stale_probe(self):
        items = access.findings(
            account(role_arn="arn:...", external_id="x"),
            probe(days_ago=access.STALE_DAYS + 1))
        assert "stale-probe" in ids(items)

    def test_fresh_probe_is_not_stale(self):
        items = access.findings(
            account(role_arn="arn:...", external_id="x"),
            probe(days_ago=access.STALE_DAYS - 1))
        assert ids(items) == []

    def test_no_regions(self):
        items = access.findings(account(regions=[]), None)
        assert "no-regions" in ids(items)

    def test_disabled_account(self):
        items = access.findings(account(enabled=False), None)
        assert "disabled" in ids(items)

    def test_danger_sorts_first(self):
        items = access.findings(
            account(role_arn="arn:...", regions=[]), None)
        assert items[0]["level"] == "danger"

    def test_worst(self):
        assert access.worst([]) is None
        assert access.worst([{"level": "warn"}, {"level": "danger"}]) == "danger"
        assert access.worst([{"level": "info"}, {"level": "warn"}]) == "warn"


class TestOverview:
    def test_worst_first(self, app):
        """가장 나쁜 계정이 맨 위에 온다. 아래로 스크롤해야 보이면 안 본다."""
        with app.app_context():
            rows = access.overview([
                account(account_id="111111111111", customer="깨끗",
                        role_arn="arn:...", external_id="x"),
                account(account_id="222222222222", customer="위험",
                        role_arn="arn:...", external_id=""),
            ])
        assert rows[0]["account"]["customer"] == "위험"

    def test_missing_table_does_not_hide_config_problems(self, app):
        """확인 기록을 못 읽어도 ExternalId 누락은 보여야 한다.

        app fixture 는 sqlite:// 를 보므로 account_probes 를 읽지 못한다.
        """
        with app.app_context():
            rows = access.overview([
                account(role_arn="arn:...", external_id=""),
            ])
        assert "no-external-id" in ids(rows[0]["findings"])


@pytest.mark.db
class TestProbeRecord:
    @pytest.fixture
    def clean(self, db_app, db_uri):
        import psycopg

        yield
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM account_probes WHERE account_id = %s",
                        ("999988887777",))

    def test_latest_wins(self, db_app, clean):
        with db_app.app_context():
            access.record_probe("999988887777", "ap-northeast-2", False, "끊김", "나")
            access.record_probe("999988887777", "ap-northeast-2", True, "됨", "나")
            got = access.last_probes()[("999988887777", "ap-northeast-2")]
        assert got["ok"] is True
        assert got["detail"] == "됨"

    def test_regions_are_recorded_separately(self, db_app, clean):
        """어떤 리전만 막히는 경우가 실제로 있다."""
        with db_app.app_context():
            access.record_probe("999988887777", "ap-northeast-2", True, "됨")
            access.record_probe("999988887777", "us-east-1", False, "안 됨")
            got = access.last_probes()
        assert got[("999988887777", "ap-northeast-2")]["ok"] is True
        assert got[("999988887777", "us-east-1")]["ok"] is False


@pytest.mark.db
class TestRoutes:
    def test_page(self, db_client):
        assert db_client.get("/customer/access").status_code == 200

    def test_secret_is_never_rendered(self, db_client, db_app, db_uri):
        """ExternalId 는 비밀이다. 있는지 없는지만 말한다."""
        import psycopg
        from app.accounts import upsert_account

        secret = "절대화면에나오면안되는값"
        with db_app.app_context():
            upsert_account("시험비밀", "988877776666", regions=["ap-northeast-2"],
                           role_arn="arn:aws:iam::988877776666:role/R",
                           external_id=secret)
        try:
            body = db_client.get("/customer/access").get_data(as_text=True)
            assert secret not in body
            assert "설정됨" in body
        finally:
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM aws_accounts WHERE account_id = %s",
                            ("988877776666",))

    def test_operator_cannot_probe(self, db_app, db_uri):
        c = db_app.test_client()
        with c.session_transaction() as s:
            s["username"] = "운영자"
            s["role"] = "operator"
        r = c.post("/customer/access/probe",
                   data={"account_id": "111111111111", "region": "ap-northeast-2"},
                   follow_redirects=True)
        assert "관리자만" in r.get_data(as_text=True)

    def test_unknown_account(self, db_client):
        r = db_client.post("/customer/access/probe",
                           data={"account_id": "000000000000", "region": "x"},
                           follow_redirects=True)
        assert "등록되지 않은 계정" in r.get_data(as_text=True)
