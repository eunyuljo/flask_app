# tests/test_app.py
# 앱 자체와 라우트 테스트.
#
# 이 프로젝트에서 500 에러가 두 번 났는데(pptx 표 좌표, RCA 커서 덮어쓰기)
# 둘 다 "화면을 열어봤더니 터졌다" 로 발견했다. 라우트를 훑는 테스트 하나면
# 즉시 잡혔을 것들이다.

import pytest


class TestFactory:
    def test_creates_app(self, app):
        assert app is not None

    def test_config_name_recorded(self, app):
        assert app.config["CONFIG_NAME"] == "testing"

    def test_all_blueprints_registered(self, app):
        expected = {
            "main", "auth", "agent", "alarm", "admin", "dashboard", "explore",
            "resources", "report", "console", "work", "runbook", "handover",
            "incident", "noise", "customer",
        }
        assert expected <= set(app.blueprints)

    def test_no_module_level_app(self):
        """import 만으로 앱이 만들어지면 안 된다(팩토리 패턴의 핵심)."""
        import app as package
        assert not hasattr(package, "app")


class TestAuth:
    def test_login_page_is_public(self, client):
        assert client.get("/auth/login").status_code == 200

    def test_index_is_public(self, client):
        assert client.get("/").status_code == 200

    @pytest.mark.parametrize("path", [
        "/agent/", "/alarm/", "/admin/", "/dashboard/", "/explore/",
        "/resources/", "/report/", "/console/", "/work/", "/runbook/",
        "/handover/", "/incident/", "/noise/", "/customer/",
    ])
    def test_protected_pages_redirect_when_anonymous(self, client, path):
        r = client.get(path)
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_wrong_password_rejected(self, client):
        client.post("/auth/login", data={"username": "admin", "password": "틀림"})
        assert client.get("/admin/").status_code == 302

    def test_ingest_api_is_not_behind_login(self, client):
        """다른 서버가 부르는 입구는 브라우저 세션이 없다."""
        r = client.post("/alarm/api/events", json={"msg": "x"})
        assert r.status_code != 302


class TestRoutes:
    """로그인 상태에서 모든 GET 라우트가 열리는지.

    DB 가 없어도 200 이어야 한다 - 이 앱은 DB 없이도 뜨는 것을 전제로 하고,
    DB 가 필요한 화면은 500 대신 안내를 보여주기로 되어 있다.
    """

    def _get_routes(self, app):
        routes = []
        for rule in app.url_map.iter_rules():
            if "GET" not in rule.methods or rule.endpoint == "static":
                continue
            path = str(rule)
            if path == "/auth/logout":
                continue          # 먼저 돌면 뒤의 요청이 전부 로그아웃된다
            for var in ("<int:work_id>", "<int:runbook_id>", "<int:incident_id>"):
                path = path.replace(var, "1")
            if "<" in path:
                continue          # 값을 모르는 동적 라우트는 건너뛴다
            routes.append(path)
        return sorted(routes)

    def test_every_get_route_responds(self, app, logged_in):
        failures = []
        for path in self._get_routes(app):
            status = logged_in.get(path).status_code
            if status >= 400:
                failures.append((path, status))
        assert not failures, f"오류가 난 라우트: {failures}"

    def test_route_list_is_not_empty(self, app):
        assert len(self._get_routes(app)) > 10

    def test_unknown_page_is_404(self, logged_in):
        assert logged_in.get("/없는페이지").status_code == 404
