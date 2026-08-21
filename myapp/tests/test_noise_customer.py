# tests/test_noise_customer.py
# 노이즈 집계와 고객사 현황 테스트. 둘 다 DB 가 필요하다.

import pytest


@pytest.mark.db
class TestNoiseRanking:
    def test_ranking_returns_rows(self, db_app, db_uri):
        from app.noise import ranking

        with db_app.app_context():
            rows = ranking(hours=8760, limit=5)
        assert isinstance(rows, list)
        if rows:
            assert {"fingerprint", "c", "sample", "severity"} <= set(rows[0])

    def test_worst_severity_not_alphabetical(self, db_app, db_uri):
        """묶음의 심각도는 '가장 나쁜 것' 이어야 한다.

        max(severity) 를 쓰면 알파벳 순으로 warning 이 이긴다
        (critical < error < info < warning).
        """
        import psycopg
        from app.noise import ranking

        order = ["critical", "error", "warning", "info"]
        with db_app.app_context():
            rows = ranking(hours=8760, limit=20)

        wrong = []
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    "SELECT DISTINCT severity FROM events WHERE fingerprint = %s",
                    (row["fingerprint"],),
                )
                present = {r[0] for r in cur.fetchall()}
                worst = next(s for s in order if s in present)
                if row["severity"] != worst:
                    wrong.append((row["fingerprint"], row["severity"], worst))
        assert not wrong, f"심각도가 가장 나쁜 것이 아닌 묶음: {wrong}"

    def test_summary_percentages_are_sane(self, db_app, db_uri):
        from app.noise import summary

        with db_app.app_context():
            s = summary(hours=8760)
        assert 0 <= s["top5_pct"] <= 100
        assert s["top5"] <= s["total"]
        assert s["kinds"] <= s["total"]


@pytest.mark.db
class TestNoiseRules:
    def test_useless_rule_rejected(self, db_app, db_uri):
        """억제 창 0 + muted 아님 = 아무것도 안 하는 규칙. 만들지 않는다."""
        from app.noise import save_rule, NoiseError

        with db_app.app_context():
            with pytest.raises(NoiseError) as e:
                save_rule("test-useless", 0, False, "", "tester")
            assert "아무것도 억제하지 않습니다" in str(e.value)

    def test_negative_window_rejected(self, db_app, db_uri):
        from app.noise import save_rule, NoiseError

        with db_app.app_context():
            with pytest.raises(NoiseError):
                save_rule("test-neg", -5, False, "", "tester")

    def test_non_numeric_window_rejected(self, db_app, db_uri):
        from app.noise import save_rule, NoiseError

        with db_app.app_context():
            with pytest.raises(NoiseError):
                save_rule("test-nan", "삼십", False, "", "tester")

    def test_save_and_delete(self, db_app, db_uri):
        from app.noise import save_rule, delete_rule, NoiseError

        with db_app.app_context():
            save_rule("test-roundtrip", 30, False, "테스트", "tester")
            save_rule("test-roundtrip", 60, False, "고침", "tester")   # 덮어쓰기
            delete_rule("test-roundtrip")
            with pytest.raises(NoiseError):
                delete_rule("test-roundtrip")     # 두 번은 안 된다


@pytest.mark.db
class TestCustomerOverview:
    def test_names_returns_list(self, db_app, db_uri):
        from app.customer import names

        with db_app.app_context():
            assert isinstance(names(), list)

    def test_overview_shape(self, db_app, db_uri):
        from app.customer import names, overview

        with db_app.app_context():
            all_names = names()
            if not all_names:
                pytest.skip("등록된 고객사가 없습니다")
            data = overview(all_names[0])

        assert data["customer"] == all_names[0]
        assert data["account_count"] == len(data["accounts"])
        for key in ("works", "incidents", "runbooks"):
            assert key in data

    def test_unknown_customer_is_empty_not_error(self, db_app, db_uri):
        """없는 고객사는 빈 결과여야 한다. 예외를 던지면 화면이 깨진다."""
        from app.customer import overview

        with db_app.app_context():
            data = overview("존재하지-않는-고객사")
        assert data["account_count"] == 0
        assert data["works"] == []

    def test_only_that_customers_rows(self, db_app, db_uri):
        """다른 고객사의 작업이 섞이면 안 된다."""
        from app.customer import names, overview

        with db_app.app_context():
            all_names = names()
            if len(all_names) < 2:
                pytest.skip("고객사가 둘 이상이어야 검증된다")
            for name in all_names:
                data = overview(name)
                for w in data["works"]:
                    assert w["account_id"] in {
                        a["account_id"] for a in data["accounts"]
                    }
