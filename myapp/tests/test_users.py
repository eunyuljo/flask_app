# tests/test_users.py
# 로그인과 계정.
#
# 이 앱은 오랫동안 아이디/비밀번호가 코드에 적혀 있었다(admin/1234).
# 그걸 users 테이블로 옮기면서, "DB 가 없어도 앱은 떠야 한다" 는 전제와
# "아무나 들어오면 안 된다" 는 요구가 부딪혔다. 그 타협이 부트스트랩이다.
# 아래 테스트는 그 타협이 의도한 대로만 열리는지를 본다.

import pytest

from app import users


class TestRole:
    """역할 비교. 상위 역할은 하위를 포함한다."""

    @pytest.mark.parametrize("role, required, expected", [
        ("admin", "admin", True),
        ("admin", "operator", True),
        ("admin", "viewer", True),
        ("operator", "admin", False),
        ("operator", "operator", True),
        ("viewer", "operator", False),
        ("viewer", "viewer", True),
        (None, "viewer", False),
        ("", "viewer", False),
        ("없는역할", "viewer", False),
    ])
    def test_can(self, role, required, expected):
        assert users.can(role, required) is expected

    def test_unknown_requirement_denies(self):
        """모르는 권한을 요구하면 아무도 통과하지 못해야 한다.

        오타(예: "adimn")가 '전부 허용' 으로 조용히 바뀌면 안 된다.
        """
        assert users.can("admin", "adimn") is False


class TestBootstrap:
    """계정이 하나도 없을 때만 열리는 통로."""

    def test_login_works_without_db(self, client):
        """DB 가 없어도 들어올 수는 있어야 한다."""
        r = client.post(
            "/auth/login",
            data={"username": "admin", "password": "1234"},
            follow_redirects=True,
        )
        assert r.status_code == 200
        with client.session_transaction() as s:
            assert s["username"] == "admin"
            assert s["role"] == "admin"
            assert s["bootstrap"] is True

    def test_wrong_password_rejected(self, client):
        assert users._bootstrap_login("admin", "5678") is None

    def test_wrong_username_rejected(self, client):
        assert users._bootstrap_login("root", "1234") is None

    def test_non_ascii_password_does_not_crash(self, client):
        """hmac.compare_digest 는 str 이면 ASCII 만 받는다.

        한글 비밀번호를 넣었다가 TypeError 로 500 이 났었다.
        """
        assert users._bootstrap_login("admin", "틀린비밀번호") is None

    def test_banner_shows_on_login_page(self, client):
        """계정이 없으면 로그인 화면이 그 사실을 알려준다."""
        body = client.get("/auth/login").get_data(as_text=True)
        assert "기본 계정" in body

    def test_banner_shows_after_login(self, logged_in):
        body = logged_in.get("/").get_data(as_text=True)
        assert "코드에 그대로" in body


class TestSession:
    def test_logout_clears_everything(self, logged_in):
        logged_in.get("/auth/logout")
        with logged_in.session_transaction() as s:
            assert "username" not in s
            assert "role" not in s
            assert "bootstrap" not in s

    def test_login_starts_a_fresh_session(self, client):
        """로그인 전에 심어둔 세션 값이 로그인 뒤로 넘어오면 안 된다(세션 고정)."""
        with client.session_transaction() as s:
            s["심어둔값"] = "남아있으면안됨"
        client.post("/auth/login", data={"username": "admin", "password": "1234"})
        with client.session_transaction() as s:
            assert "심어둔값" not in s

    def test_session_is_permanent(self, logged_in, app):
        """permanent 가 아니면 PERMANENT_SESSION_LIFETIME 이 적용되지 않는다."""
        with logged_in.session_transaction() as s:
            assert s.permanent is True
        assert app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() > 0


class TestReadOnly:
    """조회 전용 계정은 바꾸는 작업을 할 수 없다."""

    def _as(self, app, role):
        c = app.test_client()
        with c.session_transaction() as s:
            s["username"] = "보는사람"
            s["role"] = role
        return c

    def test_viewer_post_is_blocked(self, app):
        c = self._as(app, "viewer")
        r = c.post("/alarm/", data={"message": "x"})
        assert r.status_code == 302

    def test_operator_post_is_not_blocked_by_role(self, app):
        """운영자는 역할 때문에 막히지는 않는다(그 뒤 처리에서 실패할 수는 있다)."""
        c = self._as(app, "operator")
        r = c.post("/alarm/", data={"message": "x"}, follow_redirects=True)
        assert "조회 전용" not in r.get_data(as_text=True)

    def test_viewer_can_still_read(self, app):
        c = self._as(app, "viewer")
        assert c.get("/alarm/").status_code == 200

    def test_viewer_can_log_out(self, app):
        """로그아웃까지 막으면 갇힌다. logout 은 GET 이지만 auth 는 통째로 뺀다."""
        c = self._as(app, "viewer")
        c.get("/auth/logout")
        with c.session_transaction() as s:
            assert "username" not in s

    def test_anonymous_api_is_not_blocked_by_role(self, app):
        """로그인하지 않은 요청은 이 검사에 걸리지 않는다(각자 알아서 막는다)."""
        r = app.test_client().post("/alarm/api/events", json={"msg": "x"})
        assert r.status_code != 302


class TestAdminGate:
    """관리자 화면은 역할로 막는다. 예전에는 설정 파일의 이름 목록이었다."""

    def _as(self, app, role):
        c = app.test_client()
        with c.session_transaction() as s:
            s["username"] = "누구"
            s["role"] = role
        return c

    def test_operator_gets_403(self, app):
        assert self._as(app, "operator").get("/admin/").status_code == 403

    def test_admin_gets_in(self, app):
        assert self._as(app, "admin").get("/admin/").status_code == 200

    def test_console_rejects_operator(self, app):
        r = self._as(app, "operator").get("/console/")
        assert r.status_code == 302

    def test_admin_menu_hidden_from_operator(self, app):
        body = self._as(app, "operator").get("/").get_data(as_text=True)
        assert "/admin/" not in body

    def test_admin_menu_shown_to_admin(self, app):
        body = self._as(app, "admin").get("/").get_data(as_text=True)
        assert "/admin/" in body


class TestIngestKey:
    """알람 수집 API 의 키."""

    KEY = "secret-key-123"

    def _post(self, app, headers=None, data=None):
        app.config["INGEST_API_KEYS"] = {self.KEY}
        kwargs = {"headers": headers or {}}
        if data is None:
            kwargs["json"] = {"message": "x"}
        else:
            kwargs["data"] = data
        return app.test_client().post("/alarm/api/events", **kwargs)

    def test_open_when_no_keys_configured(self, app):
        app.config["INGEST_API_KEYS"] = set()
        r = app.test_client().post("/alarm/api/events", json={"message": "x"})
        assert r.status_code != 401

    def test_rejected_without_key(self, app):
        assert self._post(app).status_code == 401

    def test_accepted_with_header(self, app):
        assert self._post(app, {"X-API-Key": self.KEY}).status_code != 401

    def test_accepted_with_bearer(self, app):
        assert self._post(app, {"Authorization": f"Bearer {self.KEY}"}).status_code != 401

    def test_bearer_prefix_is_case_insensitive(self, app):
        assert self._post(app, {"Authorization": f"bearer {self.KEY}"}).status_code != 401

    def test_wrong_key_rejected(self, app):
        assert self._post(app, {"X-API-Key": "다른키"}).status_code == 401

    def test_empty_header_rejected(self, app):
        assert self._post(app, {"X-API-Key": ""}).status_code == 401

    def test_bare_authorization_without_bearer_rejected(self, app):
        assert self._post(app, {"Authorization": self.KEY}).status_code == 401

    def test_non_ascii_key_survives_wsgi_header_decoding(self, app):
        """헤더는 WSGI 규약에 따라 latin-1 로 디코딩되어 들어온다.

        키에 한글이 있으면 앱에는 글자가 깨진 채 도착한다. 테스트 클라이언트는
        그 과정을 흉내 내지 않아서, 실제 서버에서만 401 이 나는 걸
        curl 로 확인하고서야 알았다. 여기서는 그 디코딩을 직접 재현한다.
        """
        key = "고객사A키"
        app.config["INGEST_API_KEYS"] = {key}
        # 실제 서버가 앱에 넘겨주는 형태: UTF-8 바이트를 latin-1 로 읽은 문자열
        as_server_sees_it = key.encode("utf-8").decode("latin-1")
        r = app.test_client().post(
            "/alarm/api/events", json={"message": "x"},
            headers={"X-API-Key": as_server_sees_it},
        )
        assert r.status_code != 401

    def test_key_check_runs_before_body_parsing(self, app):
        """본문이 깨져 있어도 401 이 먼저 나와야 한다.

        400 을 먼저 돌려주면 '키 없이도 여기까지는 왔다' 는 정보를 준다.
        """
        assert self._post(app, data="JSON 아님").status_code == 401


@pytest.mark.db
class TestUserTable:
    """진짜 DB 를 쓰는 부분. PostgreSQL 이 없으면 건너뛴다."""

    @pytest.fixture
    def clean(self, db_app, db_uri):
        """테스트가 만든 계정만 지운다. 실제 계정은 건드리지 않는다."""
        import psycopg

        def purge():
            with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE username LIKE 'test-%'")

        purge()
        yield
        purge()

    def test_password_is_hashed(self, db_app, clean):
        import psycopg

        with db_app.app_context():
            users.create("test-해시", "비밀번호12345", "operator")
        uri = db_app.config["DATABASE_URI"].replace(
            "postgresql+psycopg://", "postgresql://"
        )
        with psycopg.connect(uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM users WHERE username = %s", ("test-해시",)
            )
            stored = cur.fetchone()[0]
        assert "비밀번호12345" not in stored
        assert stored.count("$") >= 2  # 알고리즘$솔트$해시

    def test_authenticate_round_trip(self, db_app, clean):
        with db_app.app_context():
            users.create("test-로그인", "비밀번호12345", "operator")
            user = users.authenticate("test-로그인", "비밀번호12345")
            assert user["role"] == "operator"
            assert "password_hash" not in user
            assert users.authenticate("test-로그인", "틀린비밀번호12345") is None

    def test_disabled_user_cannot_log_in(self, db_app, clean):
        with db_app.app_context():
            users.create("test-중지", "비밀번호12345", "operator")
            users.set_enabled("test-중지", False)
            assert users.authenticate("test-중지", "비밀번호12345") is None

    def test_short_password_rejected(self, db_app, clean):
        with db_app.app_context():
            with pytest.raises(users.UserError):
                users.create("test-짧은", "1234", "operator")

    def test_duplicate_username_rejected(self, db_app, clean):
        with db_app.app_context():
            users.create("test-중복", "비밀번호12345", "operator")
            with pytest.raises(users.UserError):
                users.create("test-중복", "다른비밀번호12345", "admin")

    def test_unknown_role_rejected(self, db_app, clean):
        with db_app.app_context():
            with pytest.raises(users.UserError):
                users.create("test-역할", "비밀번호12345", "superuser")

    def test_last_admin_cannot_be_disabled(self, db_app, clean):
        """관리자를 전부 끄면 아무도 관리자 화면에 못 들어간다.

        이 DB 에 이미 관리자가 있을 수 있으므로, 테스트 관리자를 만들어 두고
        나머지를 잠시 껐다가 되돌린다. '관리자가 하나뿐인 상태' 를 실제로
        만들어야 검사가 걸리는지 볼 수 있다.
        """
        with db_app.app_context():
            users.create("test-마지막관리자", "비밀번호12345", "admin")
            others = [
                u["username"] for u in users.listing()
                if u["role"] == "admin" and u["enabled"]
                and u["username"] != "test-마지막관리자"
            ]
            for name in others:
                users.set_enabled(name, False)
            try:
                with pytest.raises(users.UserError):
                    users.set_enabled("test-마지막관리자", False)
            finally:
                for name in others:
                    users.set_enabled(name, True)

    def test_non_admin_can_be_disabled_even_as_the_only_one(self, db_app, clean):
        """'마지막 관리자' 검사가 운영자에게 걸리면 안 된다.

        처음에 대상이 관리자인지 보지 않고 '다른 관리자가 있는가' 만
        따졌더니, 관리자가 하나뿐일 때 운영자를 끄는 것까지 막혔다.
        """
        with db_app.app_context():
            users.create("test-혼자관리자", "비밀번호12345", "admin")
            users.create("test-운영자", "비밀번호12345", "operator")
            others = [
                u["username"] for u in users.listing()
                if u["role"] == "admin" and u["enabled"]
                and u["username"] != "test-혼자관리자"
            ]
            for name in others:
                users.set_enabled(name, False)
            try:
                users.set_enabled("test-운영자", False)  # 예외가 나면 안 된다
                assert users.authenticate("test-운영자", "비밀번호12345") is None
            finally:
                for name in others:
                    users.set_enabled(name, True)

    def test_bootstrap_closes_once_a_user_exists(self, db_app, clean):
        with db_app.app_context():
            users.create("test-첫계정", "비밀번호12345", "admin")
            assert users.bootstrap_mode() is False
            # 계정이 생겼으므로 하드코딩 계정은 더 이상 통하지 않는다.
            assert users.authenticate("admin", "1234") is None

    def test_last_login_is_recorded(self, db_app, clean):
        with db_app.app_context():
            users.create("test-접속시각", "비밀번호12345", "operator")
            assert users.listing()  # 목록이 나온다
            before = [u for u in users.listing() if u["username"] == "test-접속시각"][0]
            assert before["last_login_at"] is None
            users.authenticate("test-접속시각", "비밀번호12345")
            after = [u for u in users.listing() if u["username"] == "test-접속시각"][0]
            assert after["last_login_at"] is not None
