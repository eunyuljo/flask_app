# tests/test_csrf.py
# CSRF 방어.
#
# 이게 없으면 로그인한 사용자가 남의 페이지를 여는 것만으로 우리 쪽 쓰기가
# 실행된다. 그 페이지가 우리 주소로 폼을 제출하면 브라우저가 세션 쿠키를
# 알아서 붙이기 때문이다. 이 앱에는 알람 확인·런북 저장·작업 승인·사용자
# 생성처럼 되돌리기 어려운 POST 가 50개 있다.
#
# 예전에는 config 에 WTF_CSRF_ENABLED 가 있었지만 Flask-WTF 가 설치되어
# 있지 않아서 아무 일도 하지 않았다. 설정이 켜져 있다고 방어가 되는 것은
# 아니다 - 그 설정을 읽는 쪽이 있어야 한다.

import pathlib
import re

import pytest


TEMPLATES = pathlib.Path(__file__).parent.parent / "app" / "templates"
POST_FORM = re.compile(r'<form\b[^>]*method="post"[^>]*>', re.I | re.S)


def csrf_app():
    """CSRF 가 켜진 앱. testing 설정은 일부러 꺼두므로 development 를 쓴다."""
    from app import create_app

    return create_app("development")


def token_from(client, path):
    body = client.get(path).get_data(as_text=True)
    found = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert found, f"{path} 에 CSRF 토큰이 없습니다"
    return found.group(1)


class TestEveryFormCarriesAToken:
    """한 폼이라도 빠지면 그 화면만 조용히 깨진다.

    사용자에게는 '저장이 안 된다' 로 보이고, 원인이 CSRF 라는 것은
    화면 어디에도 안 나온다.
    """

    @pytest.mark.parametrize(
        "path", sorted(p.name for p in TEMPLATES.glob("*.html")))
    def test_post_forms_have_csrf_token(self, path):
        text = (TEMPLATES / path).read_text("utf-8")
        forms = POST_FORM.findall(text)
        if not forms:
            return
        tokens = text.count("csrf_token()")
        assert tokens >= len(forms), (
            f"{path}: POST 폼 {len(forms)}개인데 토큰은 {tokens}개입니다"
        )


class TestItActuallyBlocks:
    def test_post_without_a_token_is_rejected(self):
        """설정만 있고 방어가 없던 상태를 잡는다."""
        client = csrf_app().test_client()
        r = client.post("/auth/login",
                        data={"username": "admin", "password": "1234"})
        assert r.status_code == 400

    def test_post_with_a_token_goes_through(self):
        app = csrf_app()
        client = app.test_client()
        r = client.post(
            "/auth/login",
            data={"username": "admin", "password": "1234",
                  "csrf_token": token_from(client, "/auth/login")},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert "admin" in r.get_data(as_text=True)

    def test_a_stale_token_from_another_session_does_not_work(self):
        """토큰은 세션에 묶여 있어야 한다. 아무 토큰이나 통하면 의미가 없다."""
        app = csrf_app()
        stolen = token_from(app.test_client(), "/auth/login")
        other = app.test_client()
        r = other.post("/auth/login",
                       data={"username": "admin", "password": "1234",
                             "csrf_token": stolen})
        assert r.status_code == 400


class TestTheIngestApiIsExempt:
    """다른 서버가 부르는 곳이라 토큰을 받을 방법이 없다.

    쿠키를 쓰지 않으므로 CSRF 의 전제('브라우저가 쿠키를 자동으로 붙인다')
    자체가 성립하지 않는다. 대신 API 키로 막는다.
    """

    def test_ingest_works_without_a_token(self):
        client = csrf_app().test_client()
        r = client.post("/alarm/api/events",
                        json={"msg": "CSRF 시험", "level": "info"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_only_that_one_view_is_exempt(self):
        """블루프린트를 통째로 빼면 같은 블루프린트의 화면 폼까지 열린다."""
        app = csrf_app()
        client = app.test_client()
        client.post("/auth/login",
                    data={"username": "admin", "password": "1234",
                          "csrf_token": token_from(client, "/auth/login")})
        r = client.post("/alarm/send", data={"message": "토큰 없이"})
        assert r.status_code == 400, "같은 블루프린트의 화면 폼이 열려 있습니다"


class TestConfig:
    def test_flask_wtf_is_declared(self):
        """설치되어 있어야 설정이 뜻을 갖는다."""
        text = (pathlib.Path(__file__).parent.parent
                / "requirements.txt").read_text("utf-8")
        assert "Flask-WTF" in text

    def test_testing_config_turns_it_off(self):
        """테스트마다 토큰을 받아오게 하면 본 내용이 안 보인다."""
        from app import create_app

        assert create_app("testing").config["WTF_CSRF_ENABLED"] is False
