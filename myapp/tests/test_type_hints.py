# tests/test_type_hints.py
# 타입 힌트가 붙어 있고, 실제와 맞는가.
#
# 힌트는 안 맞아도 실행이 되기 때문에 조용히 틀린 채로 남는다. 그러면
# IDE 와 정적 분석이 틀린 답을 자신 있게 알려주게 되어, 없느니만 못하다.
# 실제로 users.create 를 -> None 으로 적었다가 여기서 잡혔다 - id 를
# 돌려주는 함수였다.
#
# 서비스 모듈부터 붙였다(app/db.py 는 21개 모듈의 입구라 우선).

import inspect

import pytest

from app import db, event_store, noise, users, work


MODULES = {"db": db, "users": users, "noise": noise,
           "event_store": event_store, "work": work}


def public_functions(module):
    for name, fn in vars(module).items():
        if inspect.isfunction(fn) and fn.__module__ == module.__name__:
            yield name, fn


class TestEverythingIsAnnotated:
    @pytest.mark.parametrize("mod_name", sorted(MODULES))
    def test_return_types(self, mod_name):
        missing = [
            name for name, fn in public_functions(MODULES[mod_name])
            if inspect.signature(fn).return_annotation is inspect.Signature.empty
        ]
        assert not missing, f"{mod_name}: 반환 힌트 없음 {missing}"

    @pytest.mark.parametrize("mod_name", sorted(MODULES))
    def test_parameter_types(self, mod_name):
        missing = []
        for name, fn in public_functions(MODULES[mod_name]):
            for p in inspect.signature(fn).parameters.values():
                if p.annotation is inspect.Parameter.empty:
                    missing.append(f"{name}({p.name})")
        assert not missing, f"{mod_name}: 인자 힌트 없음 {missing}"


class TestModernSyntax:
    """list[dict] 를 쓴다. typing.List 는 3.9 부터 필요 없다."""

    @pytest.mark.parametrize("mod_name", sorted(MODULES))
    def test_no_legacy_typing_generics(self, mod_name):
        import pathlib

        path = (pathlib.Path(__file__).parent.parent / "app"
                / f"{mod_name}.py")
        text = path.read_text("utf-8")
        for legacy in ("typing.List", "typing.Dict", "typing.Tuple",
                       "List[", "Dict[", "Tuple["):
            assert legacy not in text, f"{mod_name} 에 {legacy} 가 있습니다"


class TestPsycopgIsNotImportedAtRuntime:
    """psycopg 가 없어도 앱은 떠야 한다.

    설치 안 된 상태에서 화면이 안내를 띄우고 막는 것이 이 앱의 전제인데,
    타입 힌트 때문에 모듈 import 단계에서 죽으면 그 전제가 깨진다.
    """

    @pytest.mark.parametrize("mod_name", sorted(MODULES))
    def test_guarded_by_type_checking(self, mod_name):
        import ast
        import pathlib

        path = (pathlib.Path(__file__).parent.parent / "app"
                / f"{mod_name}.py")
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                    a.name == "psycopg" for a in node.names):
                # 함수 안에서의 import 는 괜찮다(그때만 필요하다).
                parents = [n for n in ast.walk(tree)
                           if isinstance(n, (ast.FunctionDef, ast.If))
                           and node in ast.walk(n)]
                assert parents, f"{mod_name} 가 psycopg 를 최상위에서 import 합니다"


@pytest.mark.db
class TestHintsMatchReality:
    """힌트와 실제 반환값이 다르면 없느니만 못하다."""

    def test_readers(self, db_app, db_uri):
        with db_app.app_context():
            assert isinstance(db.uri(), str)
            assert isinstance(users.can("admin", "operator"), bool)
            assert isinstance(users.listing(), list)
            assert users.authenticate("zzq없는사람", "x") is None
            assert isinstance(noise.ranking(24), list)
            assert isinstance(noise.summary(24), dict)
            assert isinstance(noise.coverage({"c": 1, "severity": "info"}), tuple)
            assert isinstance(event_store.unacked_count(), dict)
            assert event_store.get("zzq없는이벤트") is None
            assert isinstance(work.pending(), list)
            assert work.get(99999999) is None

    def test_writers_return_ids(self, db_app, db_uri):
        """-> None 로 적었다가 여기서 잡혔던 자리다."""
        import psycopg

        with db_app.app_context():
            uid = users.create("zzq힌트시험", "test1234!", role="viewer")
            assert isinstance(uid, int)
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = 'zzq힌트시험'")
