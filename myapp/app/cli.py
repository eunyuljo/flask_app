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
from app.accounts import upsert_account, list_accounts
from app.collect import demo_resources, aws_resources, CollectError
from app.resources import save_snapshot, psycopg_uri

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

    @app.cli.command("collect-resources")
    @click.option("--demo", is_flag=True, help="AWS 대신 합성 리소스를 만들어 넣는다")
    @click.option("--drift", default=0.15, help="--demo 에서 변경이 일어날 확률 (기본 0.15)")
    @click.option("--region", default=None, help="수집 리전 (기본: AWS_REGION 설정값)")
    def collect_resources(demo, drift, region):
        """AWS 리소스 상태를 한 벌 수집해 스냅샷으로 저장한다.

        두 번 이상 실행하면 /resources/ 화면에서 스냅샷 간 차이를 볼 수 있다.
        --demo 는 AWS 없이 합성 리소스를 만들며, 두 번째 실행부터 일부를
        무작위로 바꿔서 diff 가 어떻게 보이는지 확인할 수 있게 한다.
        """
        region = region or current_app.config["AWS_REGION"]
        uri = psycopg_uri()

        try:
            if demo:
                account_id = "123456789012"
                items = demo_resources(uri, account_id, region, drift)
                source = "demo"
            else:
                items, account_id = aws_resources(region)
                source = "aws"
        except CollectError as e:
            raise click.ClickException(str(e))

        try:
            snapshot_id = save_snapshot(
                uri, items, account_id=account_id, region=region, source=source
            )
        except Exception as e:
            if type(e).__module__.split(".")[0] == "psycopg":
                raise click.ClickException(
                    f"DB 작업에 실패했습니다.\n  {e}\n"
                    "  resources 테이블이 없다면 flask --app run init-db 를 실행하세요."
                )
            raise

        click.echo(f"스냅샷 #{snapshot_id} 저장 ({source}, {region}, 리소스 {len(items)}개)")
        click.echo("차이 보기: http://127.0.0.1:5000/resources/")

    @app.cli.command("add-account")
    @click.option("--customer", required=True, help="고객사 이름")
    @click.option("--account-id", required=True, help="12자리 AWS 계정 번호")
    @click.option("--alias", default=None, help="화면에 표시할 짧은 이름")
    @click.option("--role-arn", default="", help="AssumeRole 대상. 비우면 데모 계정")
    @click.option("--external-id", default="", help="AssumeRole 의 ExternalId")
    @click.option("--regions", default="ap-northeast-2", help="쉼표로 구분한 리전 목록")
    @click.option("--disabled", is_flag=True, help="등록만 하고 사용은 막아둠")
    def add_account(customer, account_id, alias, role_arn, external_id, regions, disabled):
        """고객사 AWS 계정을 등록한다(이미 있으면 갱신).

        --role-arn 을 비워두면 데모 계정이 되어 실제 AWS 를 호출하지 않는다.
        자격증명 없이 화면과 명령 판정 흐름을 확인할 때 쓴다.
        """
        region_list = [r.strip() for r in regions.split(",") if r.strip()]
        if not region_list:
            raise click.ClickException("리전을 최소 하나는 지정해야 합니다.")

        try:
            upsert_account(
                customer=customer, account_id=account_id, alias=alias,
                role_arn=role_arn, external_id=external_id,
                regions=region_list, enabled=not disabled,
            )
        except Exception as e:
            if type(e).__module__.split(".")[0] == "psycopg":
                raise click.ClickException(
                    f"DB 작업에 실패했습니다.\n  {e}\n"
                    "  aws_accounts 테이블이 없다면 flask --app run init-db 를 실행하세요."
                )
            raise

        mode = "데모" if not role_arn else "AssumeRole"
        click.echo(f"등록: {customer} / {account_id} ({mode}, {', '.join(region_list)})")

    @app.cli.command("list-accounts")
    def list_accounts_cmd():
        """등록된 고객사 계정을 보여준다."""
        rows = list_accounts(enabled_only=False)
        if not rows:
            click.echo("등록된 계정이 없습니다. flask --app run add-account 로 추가하세요.")
            return
        click.echo(f"{'고객사':<14}{'계정':<16}{'모드':<12}{'리전':<28}사용")
        for a in rows:
            mode = "데모" if not a["role_arn"] else "AssumeRole"
            click.echo(
                f"{a['customer']:<14}{a['account_id']:<16}{mode:<12}"
                f"{','.join(a['regions']):<28}{'예' if a['enabled'] else '아니오'}"
            )
