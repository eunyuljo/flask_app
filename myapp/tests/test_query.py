# tests/test_query.py
# 이벤트 질의어(PromQL 스타일) 테스트.
#
# 사용자 입력이 SQL 이 되는 유일한 자리다. 컴파일 결과에 사용자 글자가
# 하나도 남지 않는다는 것을 여기서 못 박는다.

import pytest

from app.query import parse, compile_sql, QueryError


def sql_of(text, hours=24):
    """질의어 문자열을 SQL 로 컴파일한다.

    compile_sql 은 (sql, params, kind) 튜플을 돌려주므로 읽기 쉽게 dict 로 바꾼다.
    """
    sql, params, kind = compile_sql(parse(text), hours)
    return {"sql": sql, "params": params, "kind": kind}


class TestSelector:
    def test_equality(self):
        result = sql_of('{severity="critical"}')
        assert "severity = %s" in result["sql"]
        assert "critical" in result["params"]

    def test_regex(self):
        result = sql_of('{message=~"CPU"}')
        assert "~" in result["sql"]
        assert "CPU" in result["params"]

    def test_regex_with_pipe_is_not_split(self):
        """정규식 안의 | 를 파이프라인 구분자로 오해하면 안 된다."""
        result = sql_of('{severity=~"critical|error"}')
        assert "critical|error" in result["params"]

    def test_multiple_labels(self):
        result = sql_of('{severity="error", source="pay-api"}')
        assert "error" in result["params"] and "pay-api" in result["params"]

    def test_unknown_label_rejected(self):
        with pytest.raises(QueryError):
            sql_of('{nonexistent="x"}')

    def test_unknown_operator_rejected(self):
        with pytest.raises(QueryError):
            sql_of('{severity<"critical"}')


class TestInjection:
    """사용자 글자가 SQL 본문에 들어가지 않는지."""

    @pytest.mark.parametrize("payload", [
        "'; DROP TABLE events; --",
        "' OR 1=1 --",
        "x'); DELETE FROM events; --",
        "\\'; TRUNCATE events; --",
    ])
    def test_value_never_reaches_sql_text(self, payload):
        result = sql_of(f'{{source="{payload}"}}')
        # 값은 반드시 파라미터로만 간다.
        assert payload in result["params"]
        # SQL 본문에는 흔적도 없어야 한다.
        assert "DROP" not in result["sql"].upper()
        assert "DELETE" not in result["sql"].upper()
        assert "TRUNCATE" not in result["sql"].upper()
        assert payload not in result["sql"]

    def test_label_name_is_allowlisted_not_interpolated(self):
        """라벨 이름은 SQL 식별자가 되므로 허용 목록에서만 나와야 한다."""
        with pytest.raises(QueryError):
            sql_of('{"source; DROP TABLE events"="x"}')


class TestPipeline:
    def test_count_by(self):
        result = sql_of('{severity="error"} | count by source')
        assert "count(*)" in result["sql"]
        assert result["kind"] == "grouped"

    def test_count_without_by(self):
        result = sql_of('{severity="error"} | count')
        assert result["kind"] == "scalar"

    def test_no_pipeline_returns_rows(self):
        assert sql_of('{severity="error"}')["kind"] == "rows"

    def test_unknown_pipeline_stage_rejected(self):
        with pytest.raises(QueryError):
            sql_of('{severity="error"} | 이상한명령')

    def test_group_by_unknown_label_rejected(self):
        with pytest.raises(QueryError):
            sql_of('{severity="error"} | count by nonexistent')


class TestMalformed:
    @pytest.mark.parametrize("text", ["", "   ", "{", "}", "{severity}", "severity=x"])
    def test_rejected(self, text):
        with pytest.raises(QueryError):
            sql_of(text)
