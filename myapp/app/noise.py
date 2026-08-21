# app/noise.py
# 알람 노이즈를 보고 줄이는 쪽을 담당한다.
#   - 어떤 알람이 얼마나 시끄러운가 (지문별 랭킹)
#   - 그 알람에 억제 규칙과 런북이 붙어 있는가
#   - 규칙을 만들고 지우기
#
# 실제 억제 판정은 여기가 아니라 api/normalize_handler.py 에서 한다.
# 알람은 Lambda 가 보내므로 판정도 거기서 일어나야 한다.
# 이 모듈은 사람이 보고 규칙을 정하는 쪽만 맡는다.

from flask import current_app


class NoiseError(Exception):
    """집계나 규칙 저장에 실패했을 때."""


def psycopg_uri():
    return current_app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _rows(cur):
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _connect():
    try:
        import psycopg
    except ImportError as e:
        raise NoiseError("psycopg 가 설치되어 있지 않습니다.") from e
    try:
        return psycopg.connect(psycopg_uri())
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise NoiseError(f"DB 에 접속하지 못했습니다: {e}") from e
        raise


# 심각도를 '나쁜 순' 으로 세우는 SQL 조각.
# max(severity) 는 문자열 비교라 알파벳 순으로 warning 이 이긴다
# (critical < error < info < warning). 묶음의 심각도를 그렇게 뽑으면
# critical 이 섞인 알람이 warning 으로 보인다.
SEVERITY_RANK = ("CASE severity WHEN 'critical' THEN 1 WHEN 'error' THEN 2 "
                 "WHEN 'warning' THEN 3 ELSE 4 END")


def ranking(hours=168, limit=30):
    """시끄러운 알람 순위.

    '건수' 만으로는 부족하다. 500번 났어도 절차가 있고 억제 규칙이 걸려
    있으면 관리되고 있는 것이고, 20번 났는데 아무것도 없으면 그게 문제다.
    그래서 런북 유무와 규칙 유무를 같은 줄에 붙여서 본다.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.events')")
        if cur.fetchone()[0] is None:
            raise NoiseError("events 테이블이 없습니다. flask --app run init-db 를 실행하세요.")

        cur.execute(
            f"""
            SELECT e.fingerprint,
                   count(*) AS c,
                   (array_agg(e.message  ORDER BY e.occurred_at DESC))[1] AS sample,
                   (array_agg(e.source   ORDER BY e.occurred_at DESC))[1] AS source,
                   (array_agg(e.severity ORDER BY {SEVERITY_RANK.replace('severity', 'e.severity')}))[1]
                       AS severity,
                   min(e.occurred_at) AS first_seen,
                   max(e.occurred_at) AS last_seen,
                   r.window_minutes,
                   r.muted,
                   r.note,
                   (rb.fingerprint IS NOT NULL) AS has_runbook,
                   s.sent_count,
                   s.suppressed_count
              FROM events e
              LEFT JOIN alarm_rules r  ON r.fingerprint = e.fingerprint
              LEFT JOIN alarm_state s  ON s.fingerprint = e.fingerprint
              -- 런북은 고객사별로 여러 개일 수 있다. 있는지만 보면 되므로
              -- DISTINCT 로 접어서 조인한다. 그냥 조인하면 건수가 부풀려진다.
              LEFT JOIN (SELECT DISTINCT fingerprint FROM runbooks) rb
                     ON rb.fingerprint = e.fingerprint
             WHERE e.occurred_at >= now() - make_interval(hours => %s)
             GROUP BY e.fingerprint, r.window_minutes, r.muted, r.note,
                      rb.fingerprint, s.sent_count, s.suppressed_count
             ORDER BY c DESC, last_seen DESC
             LIMIT %s
            """,
            (hours, limit),
        )
        return _rows(cur)


def summary(hours=168):
    """전체 그림. 상위 몇 종이 전체의 몇 %를 차지하는지."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH per_kind AS (
                SELECT fingerprint, count(*) AS c
                  FROM events
                 WHERE occurred_at >= now() - make_interval(hours => %s)
                 GROUP BY fingerprint
            )
            SELECT
                COALESCE(sum(c), 0)                                        AS total,
                count(*)                                                   AS kinds,
                -- 상위 5종이 차지하는 비중. 이 값이 높으면 몇 개만 잡아도
                -- 노이즈가 크게 줄어든다는 뜻이다.
                COALESCE((SELECT sum(c) FROM (
                    SELECT c FROM per_kind ORDER BY c DESC LIMIT 5
                ) t), 0)                                                   AS top5
              FROM per_kind
            """,
            (hours,),
        )
        total, kinds, top5 = cur.fetchone()

        cur.execute("SELECT to_regclass('public.alarm_rules')")
        rules = muted = 0
        suppressed = 0
        if cur.fetchone()[0] is not None:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE muted) FROM alarm_rules")
            rules, muted = cur.fetchone()
            cur.execute("SELECT COALESCE(sum(suppressed_count), 0) FROM alarm_state")
            suppressed = cur.fetchone()[0]

    return {
        "hours": hours,
        "total": total,
        "kinds": kinds,
        "top5": top5,
        "top5_pct": round(top5 * 100 / total) if total else 0,
        "rules": rules,
        "muted": muted,
        "suppressed": suppressed,
    }


def save_rule(fingerprint, window_minutes, muted, note, author, sample=""):
    """억제 규칙을 만들거나 갱신한다."""
    if not fingerprint.strip():
        raise NoiseError("지문이 없습니다.")
    try:
        window_minutes = int(window_minutes or 0)
    except (TypeError, ValueError):
        raise NoiseError("억제 창은 숫자여야 합니다.")
    if window_minutes < 0:
        raise NoiseError("억제 창은 0 이상이어야 합니다.")

    # 아무 효과도 없는 규칙은 만들지 않는다. 목록만 늘리고,
    # "규칙이 걸려 있다" 는 착각을 준다.
    if window_minutes == 0 and not muted:
        raise NoiseError(
            "억제 창이 0 이고 muted 도 아니면 아무것도 억제하지 않습니다. "
            "분을 지정하거나 muted 를 켜세요."
        )

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO alarm_rules (fingerprint, window_minutes, muted, note, author, sample)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO UPDATE SET
                window_minutes = EXCLUDED.window_minutes,
                muted          = EXCLUDED.muted,
                note           = EXCLUDED.note,
                author         = EXCLUDED.author,
                sample         = EXCLUDED.sample,
                updated_at     = now()
            """,
            (fingerprint.strip(), window_minutes, bool(muted),
             note.strip(), author, sample.strip()[:200]),
        )


def delete_rule(fingerprint):
    """억제 규칙을 지운다. 발송 이력(alarm_state)은 남긴다."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM alarm_rules WHERE fingerprint = %s RETURNING fingerprint",
            (fingerprint,),
        )
        if cur.fetchone() is None:
            raise NoiseError("규칙을 찾지 못했습니다.")
