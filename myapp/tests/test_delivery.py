# tests/test_delivery.py
# 보고서 발송 기록.
#
# 지금까지 "고객사에 냈다" 는 incidents.customer_status 한 칸이 전부였다.
# 누구에게 어떤 경로로 보냈는지는 어디에도 없었다.
#
# 지키는 것:
#   1. 다운로드는 발송이 아니다 (사람이 눌러야 한 줄이 생긴다)
#   2. 받는 사람 없는 발송 기록은 만들 수 없다
#   3. 그때의 수신자 값을 복사해 둔다 (담당자가 바뀌어도 지난 기록은 그대로)

import pytest

from app import delivery
from app.delivery import DeliveryError


class TestValidation:
    def test_customer_required(self):
        with pytest.raises(DeliveryError):
            delivery.record("", "msr", "email", "나", recipients="누구")

    def test_unknown_kind(self):
        with pytest.raises(DeliveryError) as e:
            delivery.record("가고객", "카톡", "email", "나", recipients="누구")
        assert "알 수 없는 산출물" in str(e.value)

    def test_unknown_channel(self):
        with pytest.raises(DeliveryError) as e:
            delivery.record("가고객", "msr", "비둘기", "나", recipients="누구")
        assert "알 수 없는 경로" in str(e.value)

    def test_recipients_required(self):
        """누구에게 보냈는지 없는 기록은 나중에 아무 답도 못 해준다."""
        with pytest.raises(DeliveryError) as e:
            delivery.record("가고객", "msr", "email", "나", recipients="  ")
        assert "받는 사람" in str(e.value)


@pytest.mark.db
class TestStore:
    @pytest.fixture
    def clean(self, db_uri):
        yield
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM deliveries WHERE customer = %s", ("시험발송",))

    def test_record_and_read(self, db_app, clean):
        with db_app.app_context():
            delivery.record("시험발송", "msr", "email", "김보고",
                            ref="2026-08", title="8월 월간 리뷰",
                            recipients="박담당 <a@example.com>")
            got = delivery.recent("시험발송")
        assert len(got) == 1
        assert got[0]["kind"] == "msr"
        assert got[0]["recipients"] == "박담당 <a@example.com>"
        assert got[0]["sent_by"] == "김보고"

    def test_for_ref_finds_it(self, db_app, clean):
        with db_app.app_context():
            delivery.record("시험발송", "incident", "email", "나",
                            ref="12345", recipients="누구")
            assert len(delivery.for_ref("incident", "12345")) == 1
            assert delivery.for_ref("incident", "99999") == []

    def test_for_ref_takes_numbers_too(self, db_app, clean):
        """장애 번호는 int 로 넘어온다. 문자열 칼럼과 맞춰야 한다."""
        with db_app.app_context():
            delivery.record("시험발송", "incident", "email", "나",
                            ref=777, recipients="누구")
            assert len(delivery.for_ref("incident", 777)) == 1

    def test_recipients_are_a_snapshot(self, db_app, clean):
        """담당자가 바뀌어도 지난 기록은 그대로여야 한다."""
        with db_app.app_context():
            delivery.record("시험발송", "msr", "email", "나",
                            ref="2026-07", recipients="옛담당 <old@example.com>")
            delivery.record("시험발송", "msr", "email", "나",
                            ref="2026-08", recipients="새담당 <new@example.com>")
            got = delivery.recent("시험발송")
        assert {g["recipients"] for g in got} == {
            "옛담당 <old@example.com>", "새담당 <new@example.com>"}

    def test_filter_by_kind(self, db_app, clean):
        with db_app.app_context():
            delivery.record("시험발송", "msr", "email", "나", recipients="a")
            delivery.record("시험발송", "incident", "slack", "나", recipients="b")
            assert len(delivery.recent("시험발송", kind="msr")) == 1

    def test_summary(self, db_app, clean):
        with db_app.app_context():
            delivery.record("시험발송", "msr", "email", "나", recipients="a")
            delivery.record("시험발송", "incident", "email", "나", recipients="b")
            rows = {r["customer"]: r for r in delivery.summary()["rows"]}
        assert rows["시험발송"]["total"] == 2
        assert rows["시험발송"]["msr"] == 1
        assert rows["시험발송"]["incident"] == 1


@pytest.mark.db
class TestSuggestRecipients:
    @pytest.fixture
    def people(self, db_app, db_uri):
        import psycopg
        from app import contacts

        with db_app.app_context():
            contacts.add("시험수신", "박보고", "report", email="r@example.com")
            contacts.add("시험수신", "이기술", "primary", email="t@example.com")
        yield "시험수신"
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM customer_contacts WHERE customer = %s",
                        ("시험수신",))

    def test_prefers_report_contacts(self, db_app, people):
        with db_app.app_context():
            got = delivery.suggest_recipients(people)
        assert got == "박보고 <r@example.com>"

    def test_falls_back_to_primary(self, db_app, db_uri):
        import psycopg
        from app import contacts

        with db_app.app_context():
            contacts.add("시험대체", "이기술", "primary", email="t@example.com")
            got = delivery.suggest_recipients("시험대체")
        assert "이기술" in got
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM customer_contacts WHERE customer = %s",
                        ("시험대체",))

    def test_no_contacts_is_blank_not_an_error(self, db_app, db_uri):
        """연락처가 없다고 발송 기록까지 못 남기면 안 된다. 손으로 적으면 된다."""
        with db_app.app_context():
            assert delivery.suggest_recipients("아무도없는고객") == ""


@pytest.mark.db
class TestRoutes:
    def test_page(self, db_client):
        assert db_client.get("/report/deliveries").status_code == 200

    def test_page_says_download_is_not_delivery(self, db_client):
        body = db_client.get("/report/deliveries").get_data(as_text=True)
        assert "다운로드 기록이 아닙니다" in body

    def test_record_needs_recipients(self, db_client):
        r = db_client.post("/report/deliveries/record",
                           data={"customer": "가고객", "kind": "msr",
                                 "channel": "email", "recipients": ""},
                           follow_redirects=True)
        assert "받는 사람을 적으세요" in r.get_data(as_text=True)

    def test_back_stays_inside_the_app(self, db_client):
        """back 은 폼에 실려 오는 값이다."""
        r = db_client.post("/report/deliveries/record",
                           data={"customer": "가고객", "kind": "msr",
                                 "channel": "email", "recipients": "",
                                 "back": "http://evil.example/"})
        assert "evil.example" not in r.headers["Location"]


class TestDownloadIsNotDelivery:
    """파일을 만드는 경로가 발송을 기록하지 않는지 본다.

    내려받은 것을 발송으로 세면 확인하려고 열어본 것까지 전부 발송이 되고,
    그러면 이 기능이 아무 뜻도 없어진다.
    """

    @pytest.mark.parametrize("name", [
        "download", "download_pptx", "msr_download", "inventory_download",
    ])
    def test_download_endpoints_do_not_record(self, app, name):
        import inspect

        view = app.view_functions.get(f"report.{name}") or \
               app.view_functions.get(f"resources.{name}")
        if view is None:
            pytest.skip(f"{name} 이 없습니다")
        source = inspect.getsource(view)
        assert "delivery.record" not in source, name
