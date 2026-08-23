# tests/test_weekly.py
# 주간 리포트 자동 발송.
#
# 지키는 것:
#   1. 주간과 월간이 같은 수집기를 쓴다 (숫자가 갈라지면 고객사가 알아챈다)
#   2. 같은 주에 두 번 보내지 않는다 (발송 기록이 그대로 중복 방지가 된다)
#   3. 대상이 0건인 SLA 를 '미달' 이라고 적지 않는다
#   4. 한 고객사가 실패해도 나머지는 보낸다

from datetime import datetime, timedelta, timezone

import pytest

from app import msr


class TestWeekKey:
    """같은 주를 흔들리지 않게 부를 이름. 중복 방지가 여기에 걸린다."""

    def test_same_week_same_key(self):
        monday = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
        friday = datetime(2026, 8, 21, 23, 59, tzinfo=timezone.utc)
        assert msr.week_key(monday) == msr.week_key(friday)

    def test_next_week_differs(self):
        this = datetime(2026, 8, 21, tzinfo=timezone.utc)
        following = this + timedelta(days=7)
        assert msr.week_key(this) != msr.week_key(following)

    def test_shape(self):
        assert msr.week_key(datetime(2026, 8, 21, tzinfo=timezone.utc)) == "2026-W34"

    def test_year_boundary_uses_iso_year(self):
        """2027-01-01 은 ISO 로는 2026 년 53주다. 달력 연도를 쓰면
        연말에 같은 주를 두 번 보내거나 건너뛴다."""
        end_of_year = datetime(2027, 1, 1, tzinfo=timezone.utc)
        assert msr.week_key(end_of_year).startswith("2026-W")


class TestSlackRendering:
    def base(self, **over):
        data = {
            "customer": "가고객",
            "label": "08-16 ~ 08-23",
            "start": datetime(2026, 8, 16, tzinfo=timezone.utc),
            "end": datetime(2026, 8, 23, tzinfo=timezone.utc),
            "alarms": {"total": 12, "prev_total": 8, "change": 50,
                       "by_severity": {"critical": 1, "error": 3,
                                       "warning": 6, "info": 2}},
            "sla": None, "incidents": [], "works": [],
            "top_alarms": [], "notes": [],
        }
        data.update(over)
        return data

    def test_no_markdown_tables(self):
        """Slack mrkdwn 은 표를 렌더링하지 못한다.
        파이프를 그대로 보내면 글덩어리가 된다."""
        text = msr.to_slack_week(self.base())
        assert "|---" not in text
        assert "**" not in text        # Slack 은 *굵게* 다

    def test_shows_the_trend(self):
        text = msr.to_slack_week(self.base())
        assert "직전 주 8건 대비 +50%" in text

    def test_zero_target_sla_is_not_a_miss(self):
        """알람이 안 난 주를 '미달' 로 적어 보내면 그건 거짓말이다."""
        data = self.base(sla={
            "by_severity": [{"severity": "critical", "tracked": True,
                             "target_minutes": 30, "total": 0, "answered": 0,
                             "median_minutes": 0, "met": False}],
            "inferred": 0,
        })
        text = msr.to_slack_week(data)
        assert "해당 없음" in text
        assert "미달" not in text

    def test_real_miss_is_a_miss(self):
        data = self.base(sla={
            "by_severity": [{"severity": "critical", "tracked": True,
                             "target_minutes": 30, "total": 5, "answered": 3,
                             "median_minutes": 44, "met": False}],
            "inferred": 0,
        })
        assert "미달" in msr.to_slack_week(data)

    def test_untracked_severity_is_not_listed(self):
        """목표 0분(집계 안 함)을 '달성' 으로도 '미달' 로도 적지 않는다."""
        data = self.base(sla={
            "by_severity": [{"severity": "info", "tracked": False,
                             "target_minutes": 0, "total": 9, "answered": 0,
                             "median_minutes": 0, "met": False}],
            "inferred": 0,
        })
        text = msr.to_slack_week(data)
        assert "SLA" not in text

    def test_inferred_is_disclosed(self):
        """계약 이행 증거처럼 읽히면 안 된다."""
        data = self.base(sla={
            "by_severity": [{"severity": "critical", "tracked": True,
                             "target_minutes": 30, "total": 5, "answered": 5,
                             "median_minutes": 10, "met": True}],
            "inferred": 3,
        })
        text = msr.to_slack_week(data)
        assert "추정한 값" in text

    def test_runbook_gaps_are_surfaced(self):
        data = self.base(top_alarms=[
            {"times": 30, "sample": "CPU 높음", "has_runbook": False},
            {"times": 40, "sample": "디스크", "has_runbook": True},
        ])
        text = msr.to_slack_week(data)
        assert "절차가 없는 잦은 알람" in text
        assert "CPU 높음" in text
        assert "디스크" not in text

    def test_notes_are_kept(self):
        text = msr.to_slack_week(self.base(notes=["수집하지 않은 점검이 있습니다"]))
        assert "수집하지 않은 점검이 있습니다" in text


@pytest.mark.db
class TestCollectWeek:
    def test_shares_the_monthly_collector(self, db_app, db_uri):
        """주간과 월간이 다른 함수에서 나오면 언젠가 숫자가 갈라진다."""
        from app.customer import names

        with db_app.app_context():
            who = names()
            if not who:
                pytest.skip("등록된 고객사가 없습니다")
            week = msr.collect_week(who[0])
            month = msr.collect(who[0], 2026, 8)

        # 같은 자료 구조여야 같은 렌더러·같은 화면이 쓸 수 있다.
        shared = set(month) - {"year", "month"}
        assert shared <= set(week)
        assert week["days"] == 7
        assert "year" not in week      # 주간에는 달력 연/월이 뜻이 없다


@pytest.mark.db
class TestCommand:
    @pytest.fixture
    def clean(self, db_uri):
        yield
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM deliveries WHERE sent_by = %s", ("cron",))

    def test_dry_run_prints_and_does_not_record(self, db_app, db_uri, clean):
        """Slack 없이 돌리면 화면에 뿌리기만 한다. 보내지 않았는데
        기록이 남으면 다음 주에 진짜 발송이 건너뛰어진다."""
        import psycopg
        from app.customer import names

        with db_app.app_context():
            who = names()
        if not who:
            pytest.skip("등록된 고객사가 없습니다")

        runner = db_app.test_cli_runner()
        result = runner.invoke(args=["weekly-report", "--customer", who[0]])
        assert result.exit_code == 0, result.output
        assert "주간 리포트" in result.output

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM deliveries WHERE sent_by = 'cron'")
            assert cur.fetchone()[0] == 0

    def test_skips_a_week_already_sent(self, db_app, db_uri, clean):
        from datetime import datetime, timezone

        from app import delivery
        from app.customer import names

        with db_app.app_context():
            who = names()
            if not who:
                pytest.skip("등록된 고객사가 없습니다")
            ref = msr.week_key(datetime.now(timezone.utc))
            delivery.record(who[0], "report", "slack", "cron", ref=ref,
                            title="주간 리포트", recipients="Slack 채널")

        runner = db_app.test_cli_runner()
        result = runner.invoke(args=["weekly-report", "--customer", who[0]])
        assert "건너뜀" in result.output
        assert "이미 보냈습니다" in result.output

    def test_force_overrides(self, db_app, db_uri, clean):
        from datetime import datetime, timezone

        from app import delivery
        from app.customer import names

        with db_app.app_context():
            who = names()
            if not who:
                pytest.skip("등록된 고객사가 없습니다")
            ref = msr.week_key(datetime.now(timezone.utc))
            delivery.record(who[0], "report", "slack", "cron", ref=ref,
                            title="주간 리포트", recipients="Slack 채널")

        runner = db_app.test_cli_runner()
        result = runner.invoke(
            args=["weekly-report", "--customer", who[0], "--force"])
        assert "건너뜀 0건" in result.output


class TestJobRegistration:
    def test_weekly_report_is_expected_to_run(self):
        """cron 에 걸어두고 죽어도 아무도 모르면 안 된다."""
        from app import jobs

        assert "weekly-report" in jobs.EXPECTED
        assert jobs.EXPECTED["weekly-report"]["hours"] >= 24 * 7
