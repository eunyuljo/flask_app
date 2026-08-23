# tests/test_collect_targets.py
# 정기 수집이 고객사 계정별로 도는가.
#
# 예전에는 collect-resources 가 이 도구 자신의 자격증명으로 한 리전만
# 훑었다. 컴플라이언스·리소스 목록·리소스 변경이 전부 그 스냅샷 위에서
# 도는데, 고객사가 몇이든 실제로 담기는 건 계정 하나뿐이었다.
#
# 여기서 지키는 것:
#   1. 등록된 계정 × 그 계정에 허용된 리전만 대상이 된다
#   2. 한 고객사가 실패해도 나머지는 수집된다
#   3. 그래도 명령 자체는 실패로 끝난다 (부분 실패를 성공으로 적지 않는다)

import pytest

from app.collect import targets


def account(account_id, regions, customer="고객"):
    return {"account_id": account_id, "customer": customer,
            "role_arn": "", "regions": regions}


class TestTargets:
    ACCOUNTS = [
        account("111111111111", ["ap-northeast-2", "us-east-1"], "가고객"),
        account("222222222222", ["ap-northeast-2"], "나고객"),
    ]

    def test_expands_every_account_and_region(self):
        pairs = targets(self.ACCOUNTS)
        assert [(a["account_id"], r) for a, r in pairs] == [
            ("111111111111", "ap-northeast-2"),
            ("111111111111", "us-east-1"),
            ("222222222222", "ap-northeast-2"),
        ]

    def test_narrow_by_account(self):
        pairs = targets(self.ACCOUNTS, account_id="222222222222")
        assert [r for _, r in pairs] == ["ap-northeast-2"]

    def test_narrow_by_region(self):
        """그 리전을 허용하지 않은 계정은 조용히 빠진다.

        여기서 예외를 내면 리전 하나 때문에 다른 고객사 수집까지 멈춘다.
        """
        pairs = targets(self.ACCOUNTS, region="us-east-1")
        assert [a["account_id"] for a, _ in pairs] == ["111111111111"]

    def test_unknown_region_yields_nothing(self):
        assert targets(self.ACCOUNTS, region="eu-west-1") == []

    def test_account_without_regions_is_skipped(self):
        """리전을 안 적은 계정은 수집할 곳이 없다. 빈 리전으로 부르면 안 된다."""
        assert targets([account("333333333333", [])]) == []

    def test_no_accounts(self):
        assert targets([]) == []


@pytest.mark.db
class TestCommand:
    """실제로 계정을 등록하고 명령을 돌려본다. 전부 데모 계정이라
    AWS 를 부르지 않는다."""

    @pytest.fixture
    def two_customers(self, db_app, db_uri):
        import psycopg
        from app.accounts import upsert_account

        ids = ["911111111111", "922222222222"]
        with db_app.app_context():
            upsert_account("시험가", ids[0], regions=["ap-northeast-2", "us-east-1"])
            upsert_account("시험나", ids[1], regions=["ap-northeast-2"])
        yield ids
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM resources WHERE snapshot_id IN "
                        "(SELECT snapshot_id FROM resource_snapshots "
                        "  WHERE account_id = ANY(%s))", (ids,))
            cur.execute("DELETE FROM resource_snapshots WHERE account_id = ANY(%s)",
                        (ids,))
            cur.execute("DELETE FROM aws_accounts WHERE account_id = ANY(%s)", (ids,))

    def test_collects_each_account_and_region(self, db_app, db_uri, two_customers):
        import psycopg

        runner = db_app.test_cli_runner()
        result = runner.invoke(args=["collect-resources", "--account", two_customers[0]])
        assert result.exit_code == 0, result.output

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT region FROM resource_snapshots "
                " WHERE account_id = %s ORDER BY region",
                (two_customers[0],),
            )
            regions = [r[0] for r in cur.fetchall()]
        # 계정 하나에 허용 리전이 둘이면 스냅샷도 둘이다.
        assert regions == ["ap-northeast-2", "us-east-1"]

    def test_snapshot_belongs_to_the_customer_account(self, db_app, db_uri,
                                                      two_customers):
        """예전에는 여기에 이 도구 자신의 계정 번호가 들어갔다."""
        import psycopg

        runner = db_app.test_cli_runner()
        runner.invoke(args=["collect-resources", "--account", two_customers[1]])

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM resource_snapshots WHERE account_id = %s",
                (two_customers[1],),
            )
            assert cur.fetchone()[0] >= 1

    def test_no_targets_is_an_error_not_a_silent_success(self, db_app, two_customers):
        """대상이 0건인데 성공으로 끝나면 '수집되고 있다' 고 착각한다.

        계정과 리전을 함께 좁힌다. 리전만 좁히면 개발 DB 에 그 리전을
        허용한 다른 계정이 있는지에 따라 결과가 달라진다.
        """
        runner = db_app.test_cli_runner()
        result = runner.invoke(args=["collect-resources",
                                     "--account", two_customers[1],   # 시험나
                                     "--region", "us-east-1"])        # 허용 안 함
        assert result.exit_code != 0
        assert "수집할 대상이 없습니다" in result.output
