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

    @app.cli.command("backfill-account-ids")
    @click.option("--dry-run", is_flag=True, help="바꾸지 않고 몇 건인지만 센다")
    def backfill_account_ids(dry_run):
        """예전 이벤트의 계정 번호를 meta 에서 끌어올린다.

        account_id 열은 나중에 추가됐다. 그 전에 들어온 이벤트는 계정이
        meta 안에 문자열로만 남아 있어서, 고객사별 집계에 잡히지 않는다.
        meta 에 12자리 계정 번호가 있으면 표준 열로 옮긴다.
        """
        try:
            import psycopg
        except ImportError:
            raise click.ClickException("psycopg 가 설치되어 있지 않습니다.")

        # meta 안에서 계정으로 쓸 수 있는 키들. 정규화의 별칭 목록과 같다.
        keys = ["account_id_raw", "account", "accountid", "aws_account_id",
                "awsaccountid", "recipientaccountid"]

        # COALESCE 로 처음 발견되는 값을 쓴다. 값이 12자리 숫자일 때만 옮긴다.
        picked = " , ".join(f"meta ->> '{k}'" for k in keys)
        sql_where = (
            "account_id = '' AND COALESCE(" + picked + ") ~ '^[0-9]{12}$'"
        )

        uri = _psycopg_uri()
        try:
            with psycopg.connect(uri) as conn, conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.events')")
                if cur.fetchone()[0] is None:
                    raise click.ClickException(
                        "events 테이블이 없습니다. flask --app run init-db 를 실행하세요."
                    )

                cur.execute(f"SELECT count(*) FROM events WHERE {sql_where}")
                count = cur.fetchone()[0]

                if dry_run:
                    click.echo(f"옮길 수 있는 이벤트: {count}건 (--dry-run 이라 바꾸지 않음)")
                    return

                cur.execute(
                    f"UPDATE events SET account_id = COALESCE({picked}) WHERE {sql_where}"
                )
                click.echo(f"이벤트 {cur.rowcount}건의 계정 번호를 채웠습니다.")

                cur.execute("SELECT count(*) FROM events WHERE account_id = ''")
                click.echo(f"아직 계정을 모르는 이벤트: {cur.fetchone()[0]}건")
        except click.ClickException:
            raise
        except Exception as e:
            if type(e).__module__.split(".")[0] == "psycopg":
                raise click.ClickException(f"DB 작업에 실패했습니다.\n  {e}")
            raise

    @app.cli.command("prune-events")
    @click.option("--days", default=90, help="이 일수보다 오래된 것을 지운다 (기본 90)")
    @click.option("--snapshots", is_flag=True, help="리소스 스냅샷도 함께 정리한다")
    @click.option("--dry-run", is_flag=True, help="지우지 않고 몇 건인지만 센다")
    @click.option("--yes", is_flag=True, help="확인 없이 실행")
    def prune_events(days, snapshots, dry_run, yes):
        """오래된 이벤트를 지운다.

        \b
        지우는 것   : events
        지우지 않는 것 : audit_log, incidents, work_orders, runbooks
        감사 로그는 '우리가 고객 인프라에 한 일' 이라 이벤트 정리와 수명이
        다르다. 그래서 애초에 테이블을 나눠뒀다.

        \b
        주의: 사후 보고서(incidents)는 이벤트를 참조하지 않고 시간 범위로
        조회한다. 그래서 이벤트를 지우면 그 기간 장애의 타임라인이 빈다.
        이미 문서로 내보낸 것은 남지만, 화면에서 다시 조립하면 비어 보인다.
        """
        if days < 1:
            raise click.ClickException("--days 는 1 이상이어야 합니다.")

        try:
            import psycopg
        except ImportError:
            raise click.ClickException("psycopg 가 설치되어 있지 않습니다.")

        uri = _psycopg_uri()
        try:
            with psycopg.connect(uri) as conn, conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.events')")
                if cur.fetchone()[0] is None:
                    raise click.ClickException(
                        "events 테이블이 없습니다. flask --app run init-db 를 실행하세요."
                    )

                cur.execute(
                    "SELECT count(*), min(occurred_at) FROM events "
                    "WHERE occurred_at < now() - make_interval(days => %s)",
                    (days,),
                )
                ev_count, oldest = cur.fetchone()

                # 작업 증적이 참조하는 스냅샷은 지우지 않는다. 증적의 근거가
                # 사라지면 그 문서가 무의미해진다(스키마도 RESTRICT 로 막는다).
                snap_count = 0
                if snapshots:
                    cur.execute("SELECT to_regclass('public.resource_snapshots')")
                    if cur.fetchone()[0] is not None:
                        cur.execute(
                            """
                            SELECT count(*) FROM resource_snapshots s
                             WHERE s.collected_at < now() - make_interval(days => %s)
                               AND NOT EXISTS (
                                     SELECT 1 FROM work_orders w
                                      WHERE w.before_snapshot_id = s.snapshot_id
                                         OR w.after_snapshot_id  = s.snapshot_id
                                   )
                            """,
                            (days,),
                        )
                        snap_count = cur.fetchone()[0]

                # 지우면 타임라인이 비게 될 장애가 있는지 미리 알려준다.
                affected = 0
                cur.execute("SELECT to_regclass('public.incidents')")
                if cur.fetchone()[0] is not None:
                    cur.execute(
                        "SELECT count(*) FROM incidents "
                        "WHERE started_at < now() - make_interval(days => %s)",
                        (days,),
                    )
                    affected = cur.fetchone()[0]

                click.echo(f"{days}일보다 오래된 것:")
                click.echo(f"  이벤트   {ev_count}건" +
                           (f" (가장 오래된 것 {oldest:%Y-%m-%d})" if oldest else ""))
                if snapshots:
                    click.echo(f"  스냅샷   {snap_count}개 (작업 증적이 참조하는 것은 제외)")
                if affected:
                    click.echo(
                        f"  주의: 이 기간의 사후 보고서 {affected}건은 화면에서 "
                        "타임라인이 비게 됩니다."
                    )
                click.echo("  감사 로그는 지우지 않습니다.")

                if dry_run:
                    click.echo("\n--dry-run 이라 아무것도 지우지 않았습니다.")
                    return
                if not ev_count and not snap_count:
                    click.echo("\n지울 것이 없습니다.")
                    return
                if not yes and not click.confirm("\n정말 지울까요?"):
                    click.echo("취소했습니다.")
                    return

                cur.execute(
                    "DELETE FROM events WHERE occurred_at < now() - make_interval(days => %s)",
                    (days,),
                )
                click.echo(f"이벤트 {cur.rowcount}건 삭제")

                if snapshots and snap_count:
                    # resources 는 ON DELETE CASCADE 라 함께 지워진다.
                    cur.execute(
                        """
                        DELETE FROM resource_snapshots s
                         WHERE s.collected_at < now() - make_interval(days => %s)
                           AND NOT EXISTS (
                                 SELECT 1 FROM work_orders w
                                  WHERE w.before_snapshot_id = s.snapshot_id
                                     OR w.after_snapshot_id  = s.snapshot_id
                               )
                        """,
                        (days,),
                    )
                    click.echo(f"스냅샷 {cur.rowcount}개 삭제")
        except click.ClickException:
            raise
        except Exception as e:
            if type(e).__module__.split(".")[0] == "psycopg":
                raise click.ClickException(f"DB 작업에 실패했습니다.\n  {e}")
            raise

    @app.cli.command("handover")
    @click.option("--hours", default=12, type=click.Choice(["8", "12", "24"]),
                  callback=lambda c, p, v: int(v), help="근무 구간 (기본 12)")
    @click.option("--slack", is_flag=True, help="Slack 으로 보낸다")
    @click.option("--base-url", default="", help="링크에 쓸 앱 주소")
    def handover_cmd(hours, slack, base_url):
        """당직 인계를 만든다. 기본은 Markdown 을 화면에 출력한다.

        \b
        cron 예시 (매일 09:00, 지난 12시간):
          0 9 * * *  cd /path/to/myapp && \
            .venv/bin/flask --app run handover --hours 12 --slack
        """
        from app.handover import collect, to_markdown, to_slack, HandoverError

        try:
            data = collect(hours)
        except HandoverError as e:
            raise click.ClickException(str(e))

        if not slack:
            click.echo(to_markdown(data))
            return

        from app import slack as slack_mod
        from app.slack import SlackError, SlackNotConfigured

        try:
            slack_mod.post(to_slack(data, base_url), purpose="handover")
        except SlackNotConfigured as e:
            raise click.ClickException(
                f"{e}\n  .env 에 SLACK_HANDOVER_WEBHOOK 또는 "
                "SLACK_WEBHOOK_URL 을 넣으세요."
            )
        except SlackError as e:
            raise click.ClickException(str(e))

        click.echo(f"Slack 으로 보냈습니다 (지난 {hours}시간, 이벤트 {data['total']}건)")

    @app.cli.command("sla-check")
    @click.option("--hours", default=24, help="이 시간 안의 알람만 본다 (기본 24)")
    @click.option("--slack", is_flag=True, help="Slack 으로 보낸다")
    @click.option("--all", "send_all", is_flag=True,
                  help="이미 알린 것도 다시 보낸다(억제 무시)")
    @click.option("--escalate", is_flag=True,
                  help="단계를 올려 담당자를 부르고, 일정 단계부터 Jira 로 넘긴다")
    def sla_check(hours, slack, send_all, escalate):
        """목표를 넘겼는데 대응 기록이 없는 알람을 찾는다.

        \b
        cron 예시 (10분마다):
          */10 * * * *  cd /path/to/myapp && \
            .venv/bin/flask --app run sla-check --slack

        같은 (계정, 지문) 은 SLA_NOTICE_WINDOW_MINUTES 안에 한 번만 보낸다.
        이 억제가 없으면 주기 실행마다 같은 위반을 다시 알린다.
        """
        from app import sla
        from app.sla import SlaError

        try:
            found = sla.breaches(hours)
        except SlaError as e:
            raise click.ClickException(str(e))

        if not found:
            click.echo("목표를 넘긴 알람이 없습니다.")
            return

        window = current_app.config["SLA_NOTICE_WINDOW_MINUTES"]
        targets_ = found if send_all else sla.unnotified(found, window)

        click.echo(f"목표 초과 {len(found)}종" +
                   ("" if send_all else f", 이 중 새로 알릴 것 {len(targets_)}종"))
        for i in (targets_ or found):
            click.echo(
                f"  [{i['severity']}] {i['customer']} · {i['sample'][:50]} "
                f"— {i['count']}건, 목표 {i['minutes']}분 / 경과 {int(i['elapsed_minutes'])}분"
            )

        # 요약 알림과 에스컬레이션은 서로 독립이다. 억제 기록도 따로 둔다
        #   sla_notices  : "위반이 있다" 는 요약을 얼마나 자주 보낼지
        #   escalations  : 어느 단계까지 사람을 불렀는지
        # 이걸 묶어두면, 요약이 억제 창에 걸린 사이에 위반이 3단계까지
        # 커져도 아무도 불리지 않는다.
        if slack and targets_:
            from app import slack as slack_mod
            from app.slack import SlackError, SlackNotConfigured

            try:
                slack_mod.post(sla.to_slack(targets_), purpose="sla")
                sla.mark_notified(targets_)
                click.echo(f"\nSlack 으로 보냈습니다 ({len(targets_)}종)")
            except SlackNotConfigured as e:
                click.echo(f"\n요약 알림 건너뜀: {e}")
            except SlackError as e:
                click.echo(f"\n요약 알림 실패: {e}")
        elif slack:
            click.echo("\n새로 알릴 것이 없습니다(억제 창 안).")

        if escalate:
            _run_escalation(found)

    def _run_escalation(found):
        """위반 목록으로 단계를 올린다. sla-check --escalate 에서만 부른다."""
        from app import escalation
        from app.escalation import EscalationError

        steps = current_app.config["ESCALATION_STEPS"]
        jira_from = current_app.config["JIRA_ESCALATION_LEVEL"]

        try:
            todo = escalation.pending(found, steps)
        except EscalationError as e:
            raise click.ClickException(str(e))

        if not todo:
            click.echo("\n올릴 단계가 없습니다.")
            return

        click.echo(f"\n에스컬레이션 대상 {len(todo)}종")

        from app import slack as slack_mod
        from app import jira as jira_mod
        from app.slack import SlackError, SlackNotConfigured
        from app.jira import JiraError, JiraNotConfigured

        for item in todo:
            people_all = escalation.members(item["customer"])
            for level in item["levels"]:
                people = escalation._for_level(people_all, level)
                names = ", ".join(m["name"] for m in people) or "(담당자 미등록)"
                click.echo(f"  {level}단계 -> {names} :: {item['sample'][:44]}")

                # Slack 으로 부른다. 실패해도 다음 단계와 Jira 는 계속한다 -
                # 한 채널이 막혔다고 에스컬레이션 전체가 멎으면 안 된다.
                try:
                    slack_mod.post(
                        escalation.to_slack(item, level, people), purpose="sla"
                    )
                except SlackNotConfigured:
                    click.echo("      (Slack 미설정 - 건너뜀)")
                except SlackError as e:
                    click.echo(f"      (Slack 실패: {e})")

                jira_key = ""
                if level >= jira_from:
                    summary, description = escalation.to_jira(item, level)
                    try:
                        jira_key = jira_mod.create_issue(
                            summary, description,
                            labels=["sla", f"level-{level}"],
                        )
                        click.echo(f"      Jira {jira_key} 생성")
                    except JiraNotConfigured:
                        click.echo("      (Jira 미설정 - 건너뜀)")
                    except JiraError as e:
                        click.echo(f"      (Jira 실패: {e})")

                escalation.record(
                    item["account_id"], item["fingerprint"], level, jira_key
                )

    @app.cli.command("add-oncall")
    @click.option("--name", required=True, help="담당자 이름")
    @click.option("--level", default=1, help="1=1차 대응자, 2=2차, 3=관리자")
    @click.option("--slack-id", default="", help="Slack 사용자 ID (U01ABCDEF)")
    @click.option("--customer", default="", help="전담 고객사 (비우면 전체)")
    def add_oncall(name, level, slack_id, customer):
        """에스컬레이션 담당자를 등록한다.

        slack-id 는 @이름 이 아니라 사용자 ID 여야 멘션이 걸린다.
        Slack 프로필 > 더보기 > 멤버 ID 복사 로 얻는다.
        """
        from app import escalation
        from app.escalation import EscalationError

        try:
            member_id = escalation.add_member(name, level, slack_id, customer)
        except EscalationError as e:
            raise click.ClickException(str(e))
        scope = customer or "전체"
        click.echo(f"담당자 #{member_id} 등록: {name} ({level}단계, {scope})")

    @app.cli.command("list-oncall")
    def list_oncall():
        """에스컬레이션 담당자 목록."""
        from app import escalation
        from app.escalation import EscalationError

        try:
            rows = escalation.members()
        except EscalationError as e:
            raise click.ClickException(str(e))

        if not rows:
            click.echo("등록된 담당자가 없습니다. flask --app run add-oncall 로 등록하세요.")
            return

        steps = current_app.config["ESCALATION_STEPS"]
        click.echo(f"단계별 호출 시점(목표 초과 후): {steps} 분")
        click.echo(f"{'ID':>4}  {'단계':>4}  {'이름':<12} {'Slack ID':<14} 범위")
        for r in rows:
            mark = "" if r["enabled"] else "  (비활성)"
            click.echo(f"{r['id']:>4}  {r['level']:>4}  {r['name']:<12} "
                       f"{r['slack_id'] or '-':<14} {r['customer'] or '전체'}{mark}")

    @app.cli.command("link-incidents")
    def link_incidents():
        """확정된 사후 보고서를 알람 종류(지문)와 잇는다.

        연결은 보고서를 확정할 때 자동으로 만들어진다. 이 명령은 그 전에
        확정된 보고서를 따라잡기 위한 것이다.
        """
        from app import incident
        from app.incident import IncidentError

        try:
            items = [i for i in incident.recent(200) if i["status"] == "published"]
        except IncidentError as e:
            raise click.ClickException(str(e))

        if not items:
            click.echo("확정된 사후 보고서가 없습니다.")
            return

        total = 0
        for item in items:
            try:
                n = incident.link_fingerprints(item["id"])
            except IncidentError as e:
                click.echo(f"  #{item['id']} 실패: {e}")
                continue
            total += n
            click.echo(f"  #{item['id']} {item['title'][:40]} -> 지문 {n}종")
        click.echo(f"\n보고서 {len(items)}건, 지문 연결 {total}개")

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
