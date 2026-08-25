# tests/test_db_helper.py
# DB 접속을 한 곳에 모았다.
#
# psycopg_uri / _rows / _connect 세 함수가 서비스 모듈 21개에 그대로
# 복사되어 있었다. 복사본이 21개라는 것은 고칠 일이 생기면 21곳을 고쳐야
# 한다는 뜻이고, 실제로 바로 앞 커밋에서 설정 키 이름 하나 바꾸느라
# 33개 파일을 건드렸다.
#
# ── 여기서 지키는 것 ────────────────────────────────────────────────
# 모으는 것 자체보다, 모으면서 잃기 쉬운 두 가지를 지킨다.
#   1) 도메인 에러가 그대로 나와야 한다 (화면이 그걸 잡는다)
#   2) psycopg 잘못이 아닌 예외는 삼키면 안 된다

import ast
import pathlib

import pytest

from app import db


APP = pathlib.Path(__file__).parent.parent / "app"


class TestDomainErrorsSurvive:
    """공통 DbError 하나로 바꿔버리면 21개 화면이 전부 500 이 된다.

    화면은 자기가 부른 모듈의 에러만 잡는다. 노이즈 화면은 NoiseError 를
    잡지 DbError 를 잡지 않는다.
    """

    def test_connect_raises_the_error_it_was_given(self):
        class 내에러(Exception):
            pass

        app = __import__("app", fromlist=["create_app"]).create_app("testing")
        with app.app_context():
            with pytest.raises(내에러):
                db.connect(내에러)

    def test_the_message_says_it_was_the_db(self):
        class 내에러(Exception):
            pass

        app = __import__("app", fromlist=["create_app"]).create_app("testing")
        with app.app_context():
            with pytest.raises(내에러) as e:
                db.connect(내에러)
        assert "DB" in str(e.value)


class TestNonPsycopgErrorsAreNotSwallowed:
    def test_a_missing_config_key_is_not_disguised_as_a_db_failure(self):
        """설정 키가 없어서 나는 KeyError 까지 'DB 에 접속하지 못했습니다' 로
        덮으면 진짜 원인이 메시지 뒤에 숨는다."""
        class 내에러(Exception):
            pass

        app = __import__("app", fromlist=["create_app"]).create_app("testing")
        with app.app_context():
            app.config.pop("DATABASE_URI")
            with pytest.raises(KeyError):
                db.connect(내에러)


class TestNoDuplicatesLeft:
    """복사본이 다시 생기면 이 테스트가 잡는다."""

    def test_no_module_reimplements_the_uri(self):
        offenders = [
            p.name for p in APP.glob("*.py")
            if p.name != "db.py"
            and 'config["DATABASE_URI"]' in p.read_text("utf-8")
        ]
        assert not offenders, f"접속 문자열을 직접 만드는 모듈: {offenders}"

    def test_no_module_reimplements_rows(self):
        offenders = [
            p.name for p in APP.glob("*.py")
            if p.name != "db.py"
            and "cols = [d.name for d in cur.description]" in p.read_text("utf-8")
        ]
        assert not offenders, f"_rows 를 다시 만든 모듈: {offenders}"

    def test_no_module_reimplements_connect(self):
        offenders = []
        for p in APP.glob("*.py"):
            if p.name in ("db.py", "cli.py"):
                continue          # cli 는 click 예외로 바꿔야 해서 따로 둔다
            text = p.read_text("utf-8")
            if "psycopg 가 설치되어 있지 않습니다" in text:
                offenders.append(p.name)
        assert not offenders, f"_connect 를 다시 만든 모듈: {offenders}"


class TestDbHasNoAppImports:
    """21개 모듈이 이 파일을 부른다. 이 파일이 그 중 하나라도 부르면
    순환 import 가 된다."""

    def test_only_flask_and_stdlib(self):
        tree = ast.parse((APP / "db.py").read_text("utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "app" not in imported, "app/db.py 가 앱 모듈을 import 합니다"


@pytest.mark.db
class TestItStillWorks:
    def test_rows_returns_dicts_with_column_names(self, db_app, db_uri):
        with db_app.app_context():
            with db.connect(RuntimeError) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 AS 하나, 2 AS 둘")
                got = db.rows(cur)
        assert got == [{"하나": 1, "둘": 2}]

    def test_table_exists(self, db_app, db_uri):
        with db_app.app_context():
            with db.connect(RuntimeError) as conn, conn.cursor() as cur:
                assert db.table_exists(cur, "events") is True
                assert db.table_exists(cur, "zzq없는표") is False

    def test_uri_has_no_sqlalchemy_driver_notation(self, db_app):
        with db_app.app_context():
            assert "+psycopg" not in db.uri()

    def test_legacy_driver_notation_is_normalised(self, db_app):
        """운영에서 DATABASE_URL 을 예전 표기로 주는 곳이 있을 수 있다."""
        with db_app.app_context():
            db_app.config["DATABASE_URI"] = "postgresql+psycopg://a:b@h:5432/n"
            assert db.uri() == "postgresql://a:b@h:5432/n"
