# app/cli.py
# `flask --app run <명령>` 으로 실행할 수 있는 자체 CLI 명령들. DB 초기화처럼
# 웹 요청과 무관하지만 앱 설정(DB 주소 등)이 필요한 작업을 여기에 모은다.

import os

import click
from flask import current_app

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
