# app/stats.py
# 대시보드가 쓰는 집계 질의를 모아둔 모듈. SQL 은 전부 여기에만 있고, 뷰와 템플릿은
# 여기서 나온 숫자만 받아 그린다. psycopg 로 DB 를 직접 조회한다(SQLAlchemy 미사용).

from flask import current_app

from app import db

# 심각도는 정해진 순서로만 보여준다. 개수 순으로 정렬하면 매번 순서가 바뀌어서
# "위에서 두 번째가 error" 같은 위치 기억이 깨진다. 색뿐 아니라 위치도 식별 단서다.
SEVERITY_ORDER = ("critical", "error", "warning", "info")

# 알람 발송 대상 심각도 (api/normalize_handler.py 의 ALARM_SEVERITIES 와 같은 기준)
ALARM_SEVERITIES = ("critical", "error")


class StatsUnavailable(Exception):
    """DB 가 없거나 테이블이 아직 없어서 집계를 못 낼 때."""


# 접속 문자열은 app/db.py 가 만든다.
_psycopg_uri = db.uri


def collect(hours=24, top_sources=6):
    """대시보드에 필요한 숫자를 한 번의 접속으로 모두 모아 온다.

    커넥션을 매 질의마다 새로 열지 않고 하나로 묶는 이유는,
    접속 자체가 질의보다 훨씬 비싸기 때문이다.
    """
    try:
        with db.connect(StatsUnavailable) as conn:
            with conn.cursor() as cur:
                # 테이블이 없으면 아래 질의들이 전부 에러를 내므로 먼저 확인한다.
                cur.execute("SELECT to_regclass('public.events')")
                if cur.fetchone()[0] is None:
                    raise StatsUnavailable(
                        "events 테이블이 없습니다. flask --app run init-db 를 실행하세요."
                    )

                # ---- 총계 ----
                cur.execute("SELECT count(*) FROM events")
                total = cur.fetchone()[0]

                # ---- 심각도별 ----
                # 값을 SQL 에 이어붙이지 않고 %s 자리표시자로 넘긴다(인젝션 방지).
                cur.execute(
                    "SELECT severity, count(*) FROM events GROUP BY severity"
                )
                by_sev_raw = dict(cur.fetchall())
                # 0 건인 심각도도 자리를 지켜야 순서가 유지된다.
                by_severity = [
                    {"name": s, "count": by_sev_raw.get(s, 0)} for s in SEVERITY_ORDER
                ]

                # ---- 출처별 상위 N ----
                cur.execute(
                    """
                    SELECT source, count(*) AS c
                    FROM events
                    GROUP BY source
                    ORDER BY c DESC, source
                    LIMIT %s
                    """,
                    (top_sources,),
                )
                by_source = [{"name": r[0], "count": r[1]} for r in cur.fetchall()]

                cur.execute("SELECT count(DISTINCT source) FROM events")
                source_count = cur.fetchone()[0]

                # ---- 최근 N시간, 1시간 단위 ----
                # generate_series 로 빈 시간대도 0 으로 채운다.
                # 이걸 안 하면 이벤트가 없는 시간이 통째로 빠져서 시간 축이 왜곡된다.
                cur.execute(
                    """
                    WITH slots AS (
                        SELECT generate_series(
                            date_trunc('hour', now()) - make_interval(hours => %s - 1),
                            date_trunc('hour', now()),
                            interval '1 hour'
                        ) AS slot
                    )
                    SELECT slots.slot, count(e.event_id) AS c
                    FROM slots
                    LEFT JOIN events e
                      ON date_trunc('hour', e.occurred_at) = slots.slot
                    GROUP BY slots.slot
                    ORDER BY slots.slot
                    """,
                    (hours,),
                )
                timeline = [
                    {"hour": r[0], "label": r[0].strftime("%H"), "count": r[1]}
                    for r in cur.fetchall()
                ]

                # ---- 최근 N시간 합계 / 알람 대상 ----
                cur.execute(
                    "SELECT count(*) FROM events "
                    "WHERE occurred_at >= now() - make_interval(hours => %s)",
                    (hours,),
                )
                recent_total = cur.fetchone()[0]

                cur.execute(
                    "SELECT count(*) FROM events WHERE severity = ANY(%s)",
                    (list(ALARM_SEVERITIES),),
                )
                alarm_total = cur.fetchone()[0]

    except StatsUnavailable:
        raise
    except Exception as e:
        # psycopg.OperationalError 등 접속 계열 오류를 화면에 안내로 바꾼다.
        if type(e).__module__.split(".")[0] == "psycopg":
            raise StatsUnavailable(f"DB 에 접속하지 못했습니다: {e}") from e
        raise

    return {
        "total": total,
        "recent_total": recent_total,
        "alarm_total": alarm_total,
        "source_count": source_count,
        "by_severity": by_severity,
        "by_source": by_source,
        "timeline": timeline,
        "hours": hours,
    }


def fingerprint_history(fingerprint, hours=168, sample=5):
    """같은 지문(= 같은 종류)의 이벤트가 최근에 얼마나 났는지 돌려준다.

    진단할 때 제일 먼저 필요한 정보다. 같은 알람이 '처음 발생'인지
    '사흘째 매시간'인지에 따라 봐야 할 곳이 완전히 달라지기 때문이다.

    지문이 제 역할을 해야만 의미가 있는 집계다
    (api/normalize_handler.py 의 _fingerprint 참고).
    """
    try:
        with db.connect(StatsUnavailable) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.events')")
            if cur.fetchone()[0] is None:
                raise StatsUnavailable("events 테이블이 없습니다.")

            # 전체 기간 기준 총 횟수와 처음/마지막 발생 시각
            cur.execute(
                """
                SELECT count(*), min(occurred_at), max(occurred_at)
                FROM events WHERE fingerprint = %s
                """,
                (fingerprint,),
            )
            total, first_seen, last_seen = cur.fetchone()

            # 최근 N시간 안에서의 횟수
            cur.execute(
                """
                SELECT count(*) FROM events
                WHERE fingerprint = %s
                  AND occurred_at >= now() - make_interval(hours => %s)
                """,
                (fingerprint, hours),
            )
            recent = cur.fetchone()[0]

            # 최근 몇 건의 실제 메시지. 값이 어떻게 변해왔는지(악화 중인지)를 본다.
            cur.execute(
                """
                SELECT occurred_at, severity, message
                FROM events WHERE fingerprint = %s
                ORDER BY occurred_at DESC LIMIT %s
                """,
                (fingerprint, sample),
            )
            recent_rows = [
                {"occurred_at": r[0], "severity": r[1], "message": r[2]}
                for r in cur.fetchall()
            ]
    except StatsUnavailable:
        raise
    except Exception as e:
        if type(e).__module__.split(".")[0] == "psycopg":
            raise StatsUnavailable(f"DB 에 접속하지 못했습니다: {e}") from e
        raise

    return {
        "total": total,
        "recent": recent,
        "hours": hours,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "samples": recent_rows,
    }
