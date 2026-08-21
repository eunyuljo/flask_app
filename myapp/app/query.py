# app/query.py
# 이벤트 조회용 질의어(PromQL 을 흉내 낸 작은 DSL)를 파싱해서 SQL 로 바꾼다.
# 핵심 원칙: 사용자가 쓴 글자는 절대 SQL 문장에 끼워 넣지 않는다.
# 라벨 이름은 미리 정한 목록에서만 고르고, 값은 전부 파라미터로 바인딩한다.
# 그래서 무슨 문자열을 넣어도 SQL 구조를 바꿀 수 없다.

import re


class QueryError(Exception):
    """질의어가 문법에 맞지 않을 때. 화면에 그대로 보여줄 안내 문구를 담는다."""


# ----------------------------------------------------------------------
# 허용 목록
# ----------------------------------------------------------------------
# 조회할 수 있는 라벨과, 그 라벨이 실제로 어느 컬럼인지의 대응표.
# 여기 없는 이름을 쓰면 파싱 단계에서 막힌다.
# 컬럼 이름을 사용자 입력에서 직접 가져오지 않는 것이 중요하다.
LABELS = {
    "severity": "severity",
    "source": "source",
    "type": "event_type",
    "message": "message",
    "fingerprint": "fingerprint",
}

# 연산자 -> SQL 연산자.
#   =   같음        !=  다름
#   =~  정규식 일치  !~  정규식 불일치   (PostgreSQL 의 ~ / !~ 연산자)
OPERATORS = {
    "=": "=",
    "!=": "<>",
    "=~": "~",
    "!~": "!~",
}

# 집계에 쓸 수 있는 함수
AGGREGATIONS = {"count"}

# {label op "value"} 한 덩어리를 읽는 정규식.
# 값은 반드시 큰따옴표로 감싸야 한다. 따옴표 안에서는 \" 로 따옴표를 넣을 수 있다.
_MATCHER = re.compile(
    r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*'
)


def _unescape(value):
    r"""따옴표 안의 \" 와 \\ 를 원래 문자로 되돌린다."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _parse_selector(text):
    """{...} 부분을 읽어 조건 목록으로 만든다.

    빈 중괄호 {} 는 "조건 없음"(전체)을 뜻한다.
    """
    text = text.strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise QueryError('조건은 중괄호로 감싸야 합니다. 예) {severity="critical"}')

    inner = text[1:-1].strip()
    if not inner:
        return []

    matchers = []
    pos = 0
    while pos < len(inner):
        m = _MATCHER.match(inner, pos)
        if not m:
            raise QueryError(
                f'조건을 읽을 수 없습니다: ...{inner[pos:pos + 24]!r}\n'
                '형식은 라벨="값" 입니다. 값은 큰따옴표로 감싸주세요.'
            )
        label, op, raw_value = m.group(1), m.group(2), m.group(3)

        if label not in LABELS:
            raise QueryError(
                f'알 수 없는 라벨입니다: {label}\n'
                f'쓸 수 있는 라벨: {", ".join(sorted(LABELS))}'
            )

        matchers.append(
            {"label": label, "column": LABELS[label], "op": op, "value": _unescape(raw_value)}
        )

        pos = m.end()
        # 다음 조건이 있으면 쉼표로 이어진다.
        if pos < len(inner):
            if inner[pos] != ",":
                raise QueryError("조건 사이는 쉼표로 구분합니다.")
            pos += 1

    return matchers


def _parse_pipeline(text):
    """`| count by source` 같은 뒷부분을 읽는다. 없으면 None."""
    text = text.strip()
    if not text:
        return None

    # count by (source)  /  count by source  /  count
    m = re.fullmatch(
        r"(\w+)\s*(?:by\s*\(?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)?)?", text
    )
    if not m:
        raise QueryError('집계는 `count` 또는 `count by 라벨` 형식입니다.')

    func, by_label = m.group(1), m.group(2)
    if func not in AGGREGATIONS:
        raise QueryError(
            f'알 수 없는 함수입니다: {func}\n쓸 수 있는 함수: {", ".join(sorted(AGGREGATIONS))}'
        )
    if by_label is not None and by_label not in LABELS:
        raise QueryError(
            f'알 수 없는 라벨입니다: {by_label}\n'
            f'쓸 수 있는 라벨: {", ".join(sorted(LABELS))}'
        )

    return {"func": func, "by": by_label, "column": LABELS[by_label] if by_label else None}


def _split_selector_and_pipeline(text):
    """{...} 부분과 그 뒤 파이프 부분을 나눈다.

    그냥 text.split("|") 로 나누면 안 된다.
    {severity=~"critical|error"} 처럼 정규식 안에 파이프가 들어 있으면
    엉뚱한 곳에서 잘리기 때문이다.
    따옴표 밖에 있는 중괄호를 세어 selector 가 끝나는 지점을 먼저 찾는다.
    """
    depth = 0
    in_quotes = False
    escaped = False

    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if in_quotes:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                # 중괄호가 닫힌 지점. 여기까지가 selector 다.
                return text[: i + 1], text[i + 1 :]

    if in_quotes:
        raise QueryError("따옴표가 닫히지 않았습니다.")
    raise QueryError('중괄호가 닫히지 않았습니다. 예) {severity="critical"}')


def parse(text):
    """질의어 한 줄을 구조로 바꾼다."""
    if text is None or not text.strip():
        raise QueryError("질의어가 비어 있습니다.")

    selector_text, rest = _split_selector_and_pipeline(text.strip())
    rest = rest.strip()

    pipeline = None
    if rest:
        if not rest.startswith("|"):
            raise QueryError(
                f"조건 뒤에는 파이프(|)가 와야 합니다: ...{rest[:20]!r}"
            )
        rest = rest[1:]
        if "|" in rest:
            raise QueryError("파이프(|)는 한 번만 쓸 수 있습니다.")
        pipeline = _parse_pipeline(rest)

    return {"matchers": _parse_selector(selector_text), "pipeline": pipeline}


def compile_sql(parsed, hours, limit=200):
    """구조를 SQL 과 파라미터 목록으로 바꾼다.

    반환하는 SQL 문자열에는 사용자가 쓴 글자가 하나도 들어가지 않는다.
    컬럼 이름은 위의 허용 목록에서 온 것이고, 값은 전부 %s 자리표시자로 나간다.
    """
    where = ["occurred_at >= now() - make_interval(hours => %s)"]
    params = [hours]

    for m in parsed["matchers"]:
        # m["column"] 은 LABELS 에서 온 값이라 안전하다(사용자 입력이 아니다).
        where.append(f'{m["column"]} {OPERATORS[m["op"]]} %s')
        params.append(m["value"])

    where_sql = " AND ".join(where)
    pipeline = parsed["pipeline"]

    if pipeline is None:
        # 집계가 없으면 원본 행을 최신순으로 보여준다.
        sql = f"""
            SELECT occurred_at, severity, source, event_type, message
            FROM events
            WHERE {where_sql}
            ORDER BY occurred_at DESC
            LIMIT %s
        """
        params.append(limit)
        return sql, params, "rows"

    if pipeline["by"] is None:
        sql = f"SELECT count(*) AS count FROM events WHERE {where_sql}"
        return sql, params, "scalar"

    col = pipeline["column"]
    sql = f"""
        SELECT {col} AS label, count(*) AS count
        FROM events
        WHERE {where_sql}
        GROUP BY {col}
        ORDER BY count DESC, {col}
        LIMIT %s
    """
    params.append(limit)
    return sql, params, "grouped"


# 화면에 보여줄 예시. 처음 쓰는 사람이 뭘 칠지 몰라 막히는 걸 줄인다.
EXAMPLES = [
    ('{}', "전체 이벤트"),
    ('{severity="critical"}', "심각도가 critical 인 것"),
    ('{severity=~"critical|error"}', "critical 또는 error (정규식)"),
    ('{source="web-01", severity!="info"}', "web-01 의 info 아닌 것"),
    ('{message=~"디스크.*"}', "메시지가 '디스크' 로 시작"),
    ('{} | count by severity', "심각도별 건수"),
    ('{severity=~"critical|error"} | count by source', "알람 대상을 출처별로"),
    ('{source=~"web-.*"} | count', "web- 로 시작하는 출처의 총 건수"),
]


# ----------------------------------------------------------------------
# 실행
# ----------------------------------------------------------------------
def run(uri, text, hours, limit=200, timeout_ms=5000):
    """질의어를 실행해 결과를 돌려준다.

    uri        : psycopg 접속 문자열
    timeout_ms : 이 시간을 넘기면 DB 가 스스로 질의를 끊는다.
                 정규식이 오래 걸리거나 데이터가 많을 때 화면이 멈추지 않게 하는 안전장치다.
    """
    import psycopg

    parsed = parse(text)                       # 문법 오류는 여기서 QueryError 로 난다
    sql, params, kind = compile_sql(parsed, hours, limit)

    with psycopg.connect(uri) as conn:
        with conn.cursor() as cur:
            # 이 트랜잭션에만 적용되는 제한 시간.
            # SET 은 유틸리티 구문이라 %s 파라미터를 못 받는다("syntax error at or near $1").
            # 대신 파라미터를 받는 set_config() 함수를 쓴다.
            # 세 번째 인자 true 는 "이 트랜잭션 안에서만"이라는 뜻(SET LOCAL 과 같다).
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(timeout_ms)),),
            )
            try:
                cur.execute(sql, params)
            except psycopg.errors.QueryCanceled:
                raise QueryError(
                    f"질의가 {timeout_ms / 1000:.0f}초를 넘겨 중단됐습니다. 조건을 좁혀보세요."
                )
            except psycopg.errors.InvalidRegularExpression as e:
                raise QueryError(f"정규식이 올바르지 않습니다: {e}")
            except psycopg.errors.UndefinedTable:
                raise QueryError(
                    "events 테이블이 없습니다. flask --app run init-db 를 실행하세요."
                )
            columns = [d.name for d in cur.description]
            rows = cur.fetchall()

    return {
        "kind": kind,
        "columns": columns,
        "rows": rows,
        "sql": " ".join(sql.split()),   # 화면에 보여줄 용도 (무엇으로 바뀌었는지 확인)
        "params": params,
        "truncated": kind != "scalar" and len(rows) >= limit,
    }
