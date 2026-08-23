# tests/test_view_tabs.py
# 같은 자료의 여러 보기를 한 메뉴로 합친 것.
#
# 화면이 28개까지 늘었을 때, 점검 항목이 같은 준비도/설정 현황이 메뉴
# 둘로 있으면 "어느 쪽을 봐야 하지" 를 매번 묻게 됐다. 같은 문제가
# 리소스(목록/변경)와 런북(절차/실행 기록)에도 있었다.
#
# 지키는 것:
#   1. 쌍마다 메뉴는 하나
#   2. 어느 보기에 있든 그 메뉴에 불이 들어온다
#   3. 두 보기 모두 탭이 보인다 (막다른 화면이 없다)
#   4. 옛 주소는 깨지지 않는다

import pytest

from app import nav

# (기본 보기, 다른 보기, 메뉴 endpoint)
PAIRS = [
    ("/resources/inventory", "/resources/", "resources.inventory_page"),
    ("/runbook/", "/runbook/runs", "runbook.index"),
    ("/customer/readiness", "/customer/readiness?view=all",
     "customer.readiness_page"),
]


class TestOneMenuPerPair:
    @pytest.mark.parametrize("primary, other, endpoint", PAIRS)
    def test_only_one_menu_entry(self, primary, other, endpoint):
        blueprint = endpoint.split(".")[0]
        entries = [i for i in nav.ITEMS
                   if i["endpoint"].split(".")[0] == blueprint
                   and i["endpoint"] in _pair_endpoints()]
        assert len(entries) == 1, f"{blueprint}: {entries}"

    @pytest.mark.parametrize("primary, other, endpoint", PAIRS)
    def test_menu_points_at_the_default_view(self, primary, other, endpoint):
        assert any(i["endpoint"] == endpoint for i in nav.ITEMS)

    @pytest.mark.parametrize("endpoint, expected", [
        ("resources.index", "resources.inventory_page"),
        ("resources.inventory_page", "resources.inventory_page"),
        ("runbook.runs", "runbook.index"),
        ("runbook.index", "runbook.index"),
        ("runbook.edit", "runbook.index"),
        ("customer.settings_page", "customer.readiness_page"),
        ("customer.readiness_page", "customer.readiness_page"),
    ])
    def test_every_view_lights_its_menu(self, endpoint, expected):
        """어느 보기에 있든 사이드바에서 자기 자리를 찾을 수 있어야 한다."""
        assert nav.active_endpoint(endpoint) == expected

    def test_impact_page_keeps_its_own_menu(self):
        """영향 범위는 같은 블루프린트지만 다른 물음이다. 합치지 않았다."""
        assert nav.active_endpoint("resources.impact_page") == \
            "resources.impact_page"


def _pair_endpoints():
    return {"resources.inventory_page", "resources.index",
            "runbook.index", "runbook.runs",
            "customer.readiness_page", "customer.settings_page"}


@pytest.mark.db
class TestBothViewsRender:
    @pytest.mark.parametrize("primary, other, endpoint", PAIRS)
    def test_both_show_tabs(self, db_client, primary, other, endpoint):
        """탭이 한쪽에만 있으면 다른 쪽이 막다른 화면이 된다."""
        for path in (primary, other):
            body = db_client.get(path).get_data(as_text=True)
            assert "view-tabs" in body, path

    @pytest.mark.parametrize("primary, other, endpoint", PAIRS)
    def test_each_view_marks_itself_active(self, db_client, primary, other,
                                           endpoint):
        """지금 어느 보기인지 알 수 없으면 탭이 아니라 그냥 링크 두 개다."""
        for path in (primary, other):
            body = db_client.get(path).get_data(as_text=True)
            tabs = body[body.index("view-tabs"):]
            tabs = tabs[:tabs.index("</p>")]
            assert "<strong>" in tabs, path

    def test_old_settings_url_still_lands(self, db_client):
        assert db_client.get("/customer/settings",
                             follow_redirects=True).status_code == 200


@pytest.mark.db
class TestNoDuplicateCopy:
    """탭이 하는 말을 본문이 또 하면 같은 것을 두 번 읽게 된다."""

    def test_inventory_does_not_link_to_its_sibling(self, db_client):
        body = db_client.get("/resources/inventory").get_data(as_text=True)
        # 탭 밖에서 형제 화면으로 가는 링크가 또 있으면 안 된다
        after_tabs = body[body.index("</p>", body.index("view-tabs")):]
        assert 'href="/resources/"' not in after_tabs

    def test_runbook_list_does_not_link_to_runs(self, db_client):
        body = db_client.get("/runbook/").get_data(as_text=True)
        after_tabs = body[body.index("</p>", body.index("view-tabs")):]
        assert 'href="/runbook/runs"' not in after_tabs


class TestCategorySizes:
    def test_merges_freed_room(self):
        """합치기 전에는 인프라 5, 대응 4였다."""
        counts = {}
        for item in nav.ITEMS:
            counts[item["category"]] = counts.get(item["category"], 0) + 1
        assert counts["infra"] == 4
        assert counts["respond"] == 3

    def test_still_under_the_cap(self):
        counts = {}
        for item in nav.ITEMS:
            counts[item["category"]] = counts.get(item["category"], 0) + 1
        assert max(counts.values()) <= 6
