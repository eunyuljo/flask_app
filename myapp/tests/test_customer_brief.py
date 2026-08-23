# tests/test_customer_brief.py
# 고객사 인수인계 문서.
#
# 담당자가 바뀔 때 넘겨야 할 것이 화면 여섯 개에 흩어져 있다. 자료는
# 이미 다 있는데 모으는 일만 사람이 한다.
#
# 지키는 것:
#   1. 비밀 값(ExternalId)은 절대 들어가지 않는다
#   2. 한 조각을 못 읽어도 나머지는 담고, 못 읽었다고 적는다
#   3. 저장하지 않는다 (고객사 정보가 바뀌면 문서도 바뀌어야 한다)

import pytest

from app import customer_brief


def data(**over):
    base = {
        "customer": "가고객",
        "generated_at": __import__("datetime").datetime(
            2026, 8, 23, tzinfo=__import__("datetime").timezone.utc),
        "accounts": [], "contacts": [], "standards": [], "routines": [],
        "runbooks": [], "incidents": [], "access": [], "deliveries": [],
        "readiness": None, "notes": [],
    }
    base.update(over)
    return base


class TestSecrets:
    def test_external_id_value_is_never_written(self):
        """이 문서는 메신저로 오가기 쉽고, 한 번 나가면 회수할 수 없다."""
        secret = "절대나오면안되는값"
        md = customer_brief.to_markdown(data(accounts=[{
            "account_id": "111122223333", "alias": "prod",
            "regions": ["ap-northeast-2"], "role_arn": "arn:...:role/R",
            "external_id": secret, "enabled": True,
        }]))
        assert secret not in md
        assert "설정됨" in md

    def test_missing_external_id_is_flagged(self):
        md = customer_brief.to_markdown(data(accounts=[{
            "account_id": "111122223333", "alias": "", "regions": [],
            "role_arn": "arn:...:role/R", "external_id": "", "enabled": True,
        }]))
        assert "**없음**" in md

    def test_the_document_says_secrets_are_excluded(self):
        """받는 사람이 '여기 다 있다' 고 믿으면 안 된다."""
        assert "비밀 값" in customer_brief.to_markdown(data())


class TestPartialFailure:
    def test_notes_are_surfaced_at_the_top(self):
        """인수인계에서는 '무엇을 못 읽었는지' 가 오히려 중요하다."""
        md = customer_brief.to_markdown(data(notes=["연락처를 읽지 못했습니다: X"]))
        assert "읽지 못한 것이 있습니다" in md
        assert "연락처를 읽지 못했습니다" in md
        # 문서 앞쪽에 있어야 한다
        assert md.index("읽지 못한 것") < md.index("## 연락처")

    def test_one_broken_part_does_not_empty_the_rest(self, app, monkeypatch):
        from app import contacts

        def boom(_):
            raise contacts.ContactError("표가 없습니다")

        monkeypatch.setattr(contacts, "listing", boom)
        with app.app_context():
            got = customer_brief.collect("아무고객")

        assert any("연락처" in n for n in got["notes"])
        assert got["customer"] == "아무고객"


class TestContent:
    def test_missing_contacts_is_called_out_not_silent(self):
        """연락처가 없는 채로 인수인계가 끝나면 새벽에 곤란해진다."""
        md = customer_brief.to_markdown(data())
        assert "인수인계 전에 채워야 합니다" in md

    def test_contacts_table(self):
        md = customer_brief.to_markdown(data(contacts=[{
            "kind": "emergency", "name": "김운영", "email": "a@example.com",
            "phone": "010-0000-0000", "note": "야간 가능",
        }]))
        assert "김운영" in md
        assert "긴급 연락" in md
        assert "야간 가능" in md

    def test_dangerous_access_is_raised(self):
        md = customer_brief.to_markdown(data(access=[{
            "account": {"account_id": "111122223333"},
            "worst": "danger",
            "findings": [{"level": "danger", "title": "ExternalId 가 없습니다"}],
        }]))
        assert "계정 접속에 위험 항목이 있습니다" in md
        assert "ExternalId 가 없습니다" in md

    def test_only_customer_runbooks(self):
        """공통 런북까지 넣으면 문서가 통째로 런북 목록이 된다."""
        import inspect

        source = inspect.getsource(customer_brief._customer_runbooks)
        assert 'r["customer"] == customer' in source

    def test_blocking_readiness_is_stated(self):
        md = customer_brief.to_markdown(data(readiness={
            "summary": {"ready": False, "blocking": 3},
            "results": [{"status": "missing", "title": "연락처를 안다",
                         "detail": "없습니다"}],
        }))
        assert "필수 항목 3건이 비어 있습니다" in md

    def test_says_it_is_not_saved(self):
        assert "저장되지 않습니다" in customer_brief.to_markdown(data())


@pytest.mark.db
class TestRoutes:
    def test_page(self, db_client):
        assert db_client.get("/customer/brief").status_code == 200

    def test_download(self, db_client, db_app):
        from app.customer import names

        with db_app.app_context():
            who = names()
        if not who:
            pytest.skip("등록된 고객사가 없습니다")

        r = db_client.get(f"/customer/brief.md?customer={who[0]}")
        assert r.status_code == 200
        assert r.data.decode("utf-8").startswith(f"# {who[0]} 인수인계")
        assert "filename*=UTF-8" in r.headers["Content-Disposition"]

    def test_screen_and_file_are_the_same_text(self, db_client, db_app):
        """화면용으로 다시 그리면 화면과 파일이 언젠가 갈라진다."""
        from app.customer import names

        with db_app.app_context():
            who = names()
        if not who:
            pytest.skip("등록된 고객사가 없습니다")

        page = db_client.get(f"/customer/brief?customer={who[0]}").get_data(as_text=True)
        file = db_client.get(f"/customer/brief.md?customer={who[0]}").get_data(as_text=True)
        # 문서 첫 줄이 화면에도 그대로 있어야 한다
        assert file.splitlines()[0] in page


@pytest.mark.db
class TestReadinessMerge:
    """준비도와 설정 현황을 한 화면의 두 보기로 합쳤다."""

    def test_deep_view_is_default(self, db_client):
        body = db_client.get("/customer/readiness").get_data(as_text=True)
        assert "고객사 하나 깊게" in body

    def test_wide_view_is_the_matrix(self, db_client):
        body = db_client.get("/customer/readiness?view=all").get_data(as_text=True)
        assert "항목별로 몇 곳이 비었나" in body

    def test_old_settings_url_redirects(self, db_client):
        """북마크와 문서에 남은 주소가 깨지면 안 된다."""
        r = db_client.get("/customer/settings")
        assert r.status_code == 302
        assert "view=all" in r.headers["Location"]

    def test_one_menu_entry_covers_both(self):
        from app import nav

        entries = [i for i in nav.ITEMS
                   if i["endpoint"] == "customer.readiness_page"]
        assert len(entries) == 1
        # 옛 주소로 들어와도 이 메뉴에 불이 들어와야 한다
        assert nav.active_endpoint("customer.settings_page") == \
            "customer.readiness_page"
