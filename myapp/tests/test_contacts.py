# tests/test_contacts.py
# 고객사 연락처.
#
# oncall_members 와 방향이 반대다. 저쪽은 우리 쪽 당직자(호출받는 사람),
# 여기는 고객사 담당자(우리가 연락하는 사람).
#
# 지키는 것:
#   1. 연락 수단 없는 연락처는 만들 수 없다
#   2. 급할 때 먼저 찾는 역할이 위로 온다
#   3. 지우지 않고 내린다 (지난 기록의 이름을 확인할 수 있어야 한다)
#   4. 개인정보라 리포트에 실리지 않는다

import pytest

from app import contacts
from app.contacts import ContactError


class TestValidation:
    def test_customer_required(self):
        with pytest.raises(ContactError):
            contacts.add("", "김운영", "primary", phone="010")

    def test_name_required(self):
        with pytest.raises(ContactError):
            contacts.add("가고객", "  ", "primary", phone="010")

    def test_unknown_kind(self):
        with pytest.raises(ContactError) as e:
            contacts.add("가고객", "김운영", "사장님", phone="010")
        assert "알 수 없는 역할" in str(e.value)

    def test_needs_a_way_to_reach_them(self):
        """연락 수단이 없는 연락처는 연락처가 아니다."""
        with pytest.raises(ContactError) as e:
            contacts.add("가고객", "김운영", "primary")
        assert "하나는 있어야" in str(e.value)

    def test_email_alone_is_enough(self, db_app, db_uri):
        import psycopg

        with db_app.app_context():
            contacts.add("시험연락", "김메일", "report", email="a@example.com")
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM customer_contacts WHERE customer = %s",
                        ("시험연락",))


@pytest.mark.db
class TestStore:
    @pytest.fixture
    def some(self, db_app, db_uri):
        import psycopg

        with db_app.app_context():
            contacts.add("시험연락", "박보고", "report", email="r@example.com")
            contacts.add("시험연락", "김긴급", "emergency", phone="010-0000-0000")
            contacts.add("시험연락", "이기술", "primary", email="t@example.com")
        yield "시험연락"
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM customer_contacts WHERE customer = %s",
                        ("시험연락",))

    def test_emergency_comes_first(self, db_app, some):
        """새벽에 먼저 찾는 것이 맨 위에 있어야 한다."""
        with db_app.app_context():
            kinds = [c["kind"] for c in contacts.listing(some)]
        assert kinds[0] == "emergency"

    def test_for_kind(self, db_app, some):
        with db_app.app_context():
            got = contacts.for_kind(some, "report")
        assert [c["name"] for c in got] == ["박보고"]

    def test_add_twice_updates(self, db_app, some):
        with db_app.app_context():
            contacts.add(some, "김긴급", "emergency", phone="010-9999-9999")
            got = contacts.for_kind(some, "emergency")
        assert len(got) == 1
        assert got[0]["phone"] == "010-9999-9999"

    def test_same_person_can_hold_two_roles(self, db_app, some):
        with db_app.app_context():
            contacts.add(some, "김긴급", "approver", phone="010-0000-0000")
            names = [c["name"] for c in contacts.listing(some)]
        assert names.count("김긴급") == 2

    def test_deactivate_hides_but_keeps(self, db_app, db_uri, some):
        """지우지 않는다. 지난 기록에 남은 이름이 누구였는지 봐야 한다."""
        import psycopg

        with db_app.app_context():
            target = contacts.for_kind(some, "report")[0]
            contacts.set_active(target["id"], False)
            assert contacts.for_kind(some, "report") == []
            assert any(c["name"] == "박보고"
                       for c in contacts.listing(some, include_inactive=True))
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM customer_contacts WHERE id = %s",
                        (target["id"],))
            assert cur.fetchone()[0] == 1

    def test_counts_in_one_query(self, db_app, some):
        with db_app.app_context():
            got = contacts.counts([some])
        assert got[some] == {"report": 1, "emergency": 1, "primary": 1}

    def test_counts_of_nothing(self, db_app, db_uri):
        with db_app.app_context():
            assert contacts.counts([]) == {}

    def test_unknown_contact(self, db_app, db_uri):
        with db_app.app_context():
            with pytest.raises(ContactError):
                contacts.set_active(-1, False)


@pytest.mark.db
class TestRoutes:
    @pytest.fixture
    def cleanup(self, db_uri):
        yield
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM customer_contacts WHERE name = %s", ("시험담당",))

    def test_hub_shows_the_panel(self, db_client):
        body = db_client.get("/customer/").get_data(as_text=True)
        assert "연락처" in body

    def test_add_and_show(self, db_client, db_app, cleanup):
        from app.customer import names

        with db_app.app_context():
            who = names()
        if not who:
            pytest.skip("등록된 고객사가 없습니다")

        r = db_client.post("/customer/contacts/add", data={
            "customer": who[0], "name": "시험담당", "kind": "emergency",
            "phone": "010-1111-2222",
        }, follow_redirects=True)
        body = r.get_data(as_text=True)
        assert "시험담당" in body
        assert "010-1111-2222" in body

    def test_bad_input_does_not_break_the_page(self, db_client):
        r = db_client.post("/customer/contacts/add",
                           data={"customer": "가고객", "name": "누구",
                                 "kind": "primary"},
                           follow_redirects=True)
        assert r.status_code == 200
        assert "하나는 있어야" in r.get_data(as_text=True)


@pytest.mark.db
class TestNotInReports:
    """개인정보는 고객사에 나가는 산출물에 실리지 않는다."""

    def test_contacts_are_not_in_the_inventory_export(self, db_app, db_uri):
        import inspect

        from app import inventory_xlsx, compliance_xlsx, msr_pptx, report_pptx

        for module in (inventory_xlsx, compliance_xlsx, msr_pptx, report_pptx):
            source = inspect.getsource(module)
            assert "customer_contacts" not in source, module.__name__
            assert "contacts" not in source, module.__name__
