# app/cli.py
# `flask --app run <명령>` 으로 실행할 수 있는 자체 CLI 명령들. DB 초기화처럼
# 웹 요청과 무관하지만 앱 설정(DB 주소 등)이 필요한 작업을 여기에 모은다.

import json
import os
import random
from datetime import datetime, timedelta, timezone

import click
from flask import current_app

# Lambda 핸들러의 정규화 함수를 그대로 가져다 쓴다.
# 샘플 데이터도 실제와 똑같은 경로를 거치게 하려는 것이다.
from api.normalize_handler import normalize

# schema.sql 은 프로젝트 루트의 db/ 에 있다.
# app/cli.py 기준으로 두 단계 위로 올라가야 루트다.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(_PROJECT_ROOT, "db", "schema.sql")


def _psycopg_uri():
    """SQLAlchemy 형식 URI 를 psycopg 가 이해하는 형식으로 바꾼다.

    설정에는 "postgresql+psycopg://..." 로 적혀 있는데, 가운데 "+psycopg" 는
    SQLAlchemy 에게 어떤 드라이버를 쓸지 알려주는 표시일 뿐이다.
    psycopg 에 직접 넘길 때는 빼야 한다.
    """
    return current_app.config["SQLALCHEMY_DATABASE_URI"].replace(
        "postgresql+psycopg://", "postgresql://"
    )


def register_cli(app):
    """앱에 CLI 명령들을 등록한다. create_app() 에서 호출한다."""

    @app.cli.command("init-db")
    def init_db():
        """db/schema.sql 을 실행해 테이블과 인덱스를 만든다.

        이 함수 안에서 current_app.config 를 그냥 쓸 수 있는 이유:
        Flask CLI 명령은 실행될 때 자동으로 '앱 컨텍스트' 안에서 돌기 때문이다.
        (웹 요청이 아닌데도 앱 설정에 접근할 수 있는 게 이 덕분이다.)
        """
        try:
            import psycopg
        except ImportError:
            raise click.ClickException(
                "psycopg 가 설치되어 있지 않습니다. pip install -r requirements.txt 를 실행하세요."
            )

        if not os.path.exists(SCHEMA_PATH):
            raise click.ClickException(f"스키마 파일을 찾을 수 없습니다: {SCHEMA_PATH}")

        with open(SCHEMA_PATH, encoding="utf-8") as f:
            sql = f.read()

        uri = _psycopg_uri()
        try:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
        except psycopg.OperationalError as e:
            # 접속 자체가 안 되는 경우(DB 안 떠 있음, 비밀번호 틀림 등)
            raise click.ClickException(
                f"DB 에 접속하지 못했습니다.\n  {e}\n"
                "  docker compose up -d 로 DB 가 떠 있는지 확인하세요."
            )

        click.echo(f"완료: {SCHEMA_PATH} 적용됨")

    @app.cli.command("db-check")
    def db_check():
        """DB 접속과 테이블 상태를 점검한다. 설정이 맞는지 빠르게 확인할 때 쓴다."""
        try:
            import psycopg
        except ImportError:
            raise click.ClickException("psycopg 가 설치되어 있지 않습니다.")

        uri = _psycopg_uri()
        # 비밀번호가 화면과 로그에 찍히지 않도록 가린다.
        safe = uri
        if "@" in safe and "//" in safe:
            head, tail = safe.split("//", 1)
            if "@" in tail:
                creds, host = tail.split("@", 1)
                user = creds.split(":")[0]
                safe = f"{head}//{user}:***@{host}"
        click.echo(f"접속 대상: {safe}")

        try:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version()")
                    click.echo(f"서버: {cur.fetchone()[0].split(',')[0]}")

                    cur.execute("SELECT to_regclass('public.events')")
                    if cur.fetchone()[0] is None:
                        click.echo("events 테이블: 없음 -> flask --app run init-db 를 실행하세요")
                        return

                    cur.execute("SELECT count(*) FROM events")
                    total = cur.fetchone()[0]
                    cur.execute(
                        "SELECT severity, count(*) FROM events GROUP BY severity ORDER BY 2 DESC"
                    )
                    rows = cur.fetchall()
        except psycopg.OperationalError as e:
            raise click.ClickException(f"DB 에 접속하지 못했습니다.\n  {e}")

        click.echo(f"events 테이블: 있음 ({total} 건)")
        for severity, count in rows:
            click.echo(f"  {severity:9} {count}")

    @app.cli.command("seed-events")
    @click.option("--count", default=200, help="만들 이벤트 수 (기본 200)")
    @click.option("--hours", default=48, help="몇 시간에 걸쳐 흩뿌릴지 (기본 48)")
    @click.option("--clear", is_flag=True, help="기존 이벤트를 모두 지우고 시작")
    def seed_events(count, hours, clear):
        """대시보드를 채워볼 샘플 이벤트를 만들어 넣는다.

        원본 표기를 일부러 제각각으로 만들어서(FATAL, p1, msg 등)
        Lambda 의 정규화가 실제로 동작하는 것도 함께 확인할 수 있게 했다.
        """
        try:
            import psycopg
        except ImportError:
            raise click.ClickException(
                "psycopg 가 설치되어 있지 않습니다. pip install -r requirements.txt 를 실행하세요."
            )

        # 실제로 들어올 법한 값들. 표기가 제각각인 것이 핵심이다.
        sources = ["web-01", "web-02", "pay-api", "api-gw", "batch", "db-primary"]
        types = ["disk", "http", "deploy", "auth", "query"]
        # (원본 심각도 표기, 가중치) - info 가 가장 흔하고 critical 이 드물게
        severities = [
            ("FATAL", 3), ("p1", 2),
            ("error", 8), ("high", 4),
            ("warn", 14), ("p3", 6),
            ("info", 40), ("debug", 13),
        ]
        messages = [
            "디스크 사용률 {n}% 초과", "응답 지연 {n}ms 감지", "결제 실패율 {n}% 급증",
            "메모리 사용률 {n}%", "커넥션 풀 {n}% 점유", "배포 완료 (빌드 #{n})",
            "헬스체크 실패 {n}회 연속", "느린 쿼리 {n}ms",
        ]

        sev_values = [s for s, _ in severities]
        sev_weights = [w for _, w in severities]
        now = datetime.now(timezone.utc)

        records = []
        for i in range(count):
            # 시간은 최근일수록 촘촘하게. 그냥 균등분포로 두면 그래프가 밋밋하다.
            offset = random.random() ** 1.6 * hours
            occurred = now - timedelta(hours=offset)
            raw = {
                # 필드 이름도 일부러 섞는다. 정규화가 별칭을 흡수하는지 보기 위함이다.
                random.choice(["msg", "message", "text"]):
                    random.choice(messages).format(n=random.randint(1, 999)),
                random.choice(["level", "severity", "priority"]):
                    random.choices(sev_values, weights=sev_weights)[0],
                random.choice(["source", "service", "origin"]): random.choice(sources),
                random.choice(["type", "kind"]): random.choice(types),
                "occurred_at": occurred.isoformat(),
                "seeded": True,
            }
            records.append(normalize(raw))

        uri = _psycopg_uri()
        try:
            with psycopg.connect(uri) as conn:
                with conn.cursor() as cur:
                    if clear:
                        cur.execute("DELETE FROM events")
                        click.echo("기존 이벤트를 모두 삭제했습니다.")

                    # executemany 로 한 번에 보낸다. 건마다 왕복하면 느리다.
                    cur.executemany(
                        """
                        INSERT INTO events (
                            event_id, event_type, source, severity,
                            message, occurred_at, received_at, fingerprint, meta
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (event_id) DO NOTHING
                        """,
                        [
                            (
                                r["event_id"], r["event_type"], r["source"], r["severity"],
                                r["message"], r["occurred_at"], r["received_at"],
                                r["fingerprint"], json.dumps(r["meta"], ensure_ascii=False),
                            )
                            for r in records
                        ],
                    )
                    cur.execute("SELECT count(*) FROM events")
                    total = cur.fetchone()[0]
                    cur.execute(
                        "SELECT severity, count(*) FROM events "
                        "GROUP BY severity ORDER BY 2 DESC"
                    )
                    rows = cur.fetchall()
        except psycopg.errors.UndefinedTable:
            raise click.ClickException(
                "events 테이블이 없습니다. flask --app run init-db 를 먼저 실행하세요."
            )
        except psycopg.OperationalError as e:
            raise click.ClickException(
                f"DB 에 접속하지 못했습니다.\n  {e}"
            )

        click.echo(f"{count} 건을 최근 {hours} 시간에 걸쳐 넣었습니다. (현재 총 {total} 건)")
        for severity, c in rows:
            click.echo(f"  {severity:9} {c}")
        click.echo("대시보드에서 확인하세요: http://127.0.0.1:5000/dashboard/")
