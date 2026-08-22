# tests/test_nav.py
# 왼쪽 메뉴.
#
# 메뉴가 템플릿에 박혀 있던 동안에는 이런 테스트를 쓸 수 없었다.
# 데이터로 꺼내 놓으니 "적어둔 화면이 실제로 있는가", "화면을 만들고
# 메뉴에 안 넣었는가" 를 기계가 본다.

import pytest

from app import nav


class TestRegistry:
    def test_every_endpoint_exists(self, app):
        """메뉴에 적어둔 화면이 실제로 있어야 한다.

        없으면 url_for 가 BuildError 를 내고, 사이드바 하나 때문에
        모든 페이지가 500 이 된다.
        """
        known = {r.endpoint for r in app.url_map.iter_rules()}
        missing = [i["endpoint"] for i in nav.ITEMS if i["endpoint"] not in known]
        assert not missing, f"메뉴에 있는데 앱에 없는 화면: {missing}"

    def test_every_endpoint_is_a_get_route(self, app):
        """메뉴는 눌러서 가는 곳이다. POST 전용을 걸어두면 405 가 난다."""
        methods = {r.endpoint: r.methods for r in app.url_map.iter_rules()}
        bad = [i["endpoint"] for i in nav.ITEMS
               if "GET" not in methods.get(i["endpoint"], set())]
        assert not bad, f"GET 으로 못 여는 메뉴: {bad}"

    def test_categories_are_known(self):
        bad = [i["endpoint"] for i in nav.ITEMS
               if i["category"] not in nav.CATEGORY_IDS]
        assert not bad, f"없는 카테고리를 가리키는 메뉴: {bad}"

    def test_no_duplicate_endpoints(self):
        eps = [i["endpoint"] for i in nav.ITEMS]
        assert len(eps) == len(set(eps))

    def test_no_duplicate_icons(self):
        """아이콘이 겹치면 접힌 사이드바(모바일)에서 구분되지 않는다."""
        icons = [i["icon"] for i in nav.ITEMS]
        dupes = {i for i in icons if icons.count(i) > 1}
        assert not dupes, f"겹치는 아이콘: {dupes}"

    def test_every_item_has_a_hint(self):
        """메뉴 이름만으로는 무엇을 하는 화면인지 모르는 것들이 있다."""
        for item in nav.ITEMS:
            assert item.get("hint"), item["endpoint"]

    def test_no_category_is_a_dumping_ground(self):
        """'관측' 하나에 열세 개가 들어 있던 게 이 작업의 출발점이다.
        한 칸이 다시 그렇게 커지면 카테고리가 아니라 '나머지 전부' 다."""
        for cid in nav.CATEGORY_IDS:
            n = len([i for i in nav.ITEMS if i["category"] == cid])
            assert n <= 6, f"{cid} 에 {n}개가 들어 있습니다"


class TestCoverage:
    """화면을 만들고 메뉴에 넣는 걸 잊는 실수를 잡는다."""

    # 메뉴에 없어도 되는 블루프린트와 그 이유.
    # 여기에 적어두면 "왜 없지?" 를 다시 확인하지 않아도 된다.
    NOT_IN_MENU = {
        "main": "홈은 카테고리 위에 따로 있다",
        "auth": "로그인/로그아웃은 사이드바 아래에 있다",
        "static": "정적 파일",
    }

    def test_every_blueprint_is_reachable_from_the_menu(self, app):
        in_menu = {i["endpoint"].split(".")[0] for i in nav.ITEMS}
        missing = set(app.blueprints) - in_menu - set(self.NOT_IN_MENU)
        assert not missing, (
            f"메뉴에서 갈 수 없는 블루프린트: {sorted(missing)}. "
            "app/nav.py 에 넣거나, 뺀 이유를 NOT_IN_MENU 에 적으세요."
        )

    def test_exemptions_are_real_blueprints(self, app):
        """면제 목록에 오타나 사라진 이름이 남아 있으면,
        진짜 빠진 것을 가려준다."""
        stale = set(self.NOT_IN_MENU) - set(app.blueprints) - {"static"}
        assert not stale, f"이제 없는 블루프린트가 면제 목록에 남아 있습니다: {stale}"


class TestActive:
    @pytest.mark.parametrize("endpoint, expected", [
        ("alarm.index", "alarm.index"),
        ("alarm.diagnose", "alarm.index"),        # 같은 블루프린트의 다른 화면
        ("work.detail", "work.index"),
        ("compliance.exceptions", "compliance.index"),
    ])
    def test_sub_pages_light_up_their_parent(self, endpoint, expected):
        assert nav.active_endpoint(endpoint) == expected

    def test_longest_prefix_wins(self):
        """'report.msr_page' 는 'report.' 와 'report.msr' 에 둘 다 걸린다.
        짧은 쪽을 고르면 월간 리뷰를 보는 동안 '리포트' 에 불이 들어온다."""
        assert nav.active_endpoint("report.msr_page") == "report.msr_page"
        assert nav.active_endpoint("report.index") == "report.index"
        assert nav.active_endpoint("report.download_pptx") == "report.index"

    def test_longest_prefix_wins_for_customer_too(self):
        assert nav.active_endpoint("customer.readiness_page") == "customer.readiness_page"
        assert nav.active_endpoint("customer.index") == "customer.index"

    def test_unknown_endpoint_lights_nothing(self):
        assert nav.active_endpoint("없는.화면") is None

    def test_none_endpoint_does_not_crash(self):
        """404 페이지에서는 request.endpoint 가 None 이다."""
        assert nav.active_endpoint(None) is None


class TestMenu:
    def test_admin_sees_admin_only_items(self):
        labels = {i["label"] for g in nav.menu("admin") for i in g["items"]}
        assert "콘솔" in labels and "관리자" in labels

    def test_operator_does_not(self):
        labels = {i["label"] for g in nav.menu("operator") for i in g["items"]}
        assert "콘솔" not in labels and "관리자" not in labels

    def test_operator_still_sees_the_rest(self):
        labels = {i["label"] for g in nav.menu("operator") for i in g["items"]}
        assert "이벤트" in labels and "작업 기록" in labels

    def test_viewer_sees_read_only_screens(self):
        labels = {i["label"] for g in nav.menu("viewer") for i in g["items"]}
        assert "대시보드" in labels
        assert "콘솔" not in labels

    def test_empty_category_is_dropped(self):
        """관리자 전용 항목만 있는 칸은 운영자에게 제목만 남으면 안 된다."""
        for group in nav.menu("operator"):
            assert group["items"]

    def test_unknown_endpoints_are_skipped(self):
        """메뉴에 적어둔 화면을 떼어냈을 때 사이드바가 앱을 죽이면 안 된다."""
        groups = nav.menu("admin", known_endpoints={"alarm.index"})
        labels = [i["label"] for g in groups for i in g["items"]]
        assert labels == ["이벤트"]

    def test_category_order_is_kept(self):
        ids = [g["id"] for g in nav.menu("admin")]
        assert ids == [c["id"] for c in nav.CATEGORIES if c["id"] in ids]


class TestRendered:
    def test_sidebar_shows_category_labels(self, logged_in):
        body = logged_in.get("/").get_data(as_text=True)
        for category in nav.CATEGORIES:
            assert f">{category['label']}</div>" in body, category["label"]

    def test_anonymous_sees_no_menu(self, client):
        body = client.get("/").get_data(as_text=True)
        assert "고객사 현황" not in body

    def test_menu_survives_a_404(self, logged_in):
        """404 에서 request.endpoint 는 None 이다. 여기서 터지면
        오류 페이지 대신 500 이 나간다."""
        assert logged_in.get("/없는페이지").status_code == 404
