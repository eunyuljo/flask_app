# tests/test_customers.py
# 고객사를 실체로 만든다.
#
# ── 왜 필요했나 ────────────────────────────────────────────────────
# 고객사 목록이 SELECT DISTINCT customer FROM aws_accounts 였다.
# 13개 표가 고객사 이름을 문자열로 들고 있는데 외래키는 하나도 없었다.
# 그래서 세 가지가 조용히 깨졌다.
#
#   1) 고객사가 AWS 계정보다 먼저 존재할 수 없었다
#   2) 오타가 새 고객사를 만들었다
#   3) 마지막 계정을 지우면 고객사가 화면에서 사라졌다
#
# 3번은 가정이 아니다. audit_log 에 '다고객' 이 계정 없이 남아 있었고
# 어느 화면에서도 볼 수 없었다.

import pytest

from app import customer


# 외래키를 걸어야 하는 표. audit_log 는 일부러 뺀다 - 아래에 이유가 있다.
LINKED = (
    "aws_accounts", "customer_contacts", "customer_routines",
    "customer_standards", "deliveries", "incidents", "oncall_members",
    "routine_runs", "runbook_runs", "runbooks", "sla_targets", "work_orders",
)


class TestPureBits:
    def test_statuses_cover_the_lifecycle(self):
        """온보딩이 상태로 있어야 준비 상태 화면이 관문이 된다."""
        assert set(customer.STATUSES) == {
            "onboarding", "active", "suspended", "ended"}

    def test_ended_is_hidden_from_the_default_list(self):
        assert "ended" not in customer.LIVE_STATUSES

    def test_shared_is_the_empty_name(self):
        """공통 런북·기본 SLA 목표가 customer = '' 로 저장된다."""
        assert customer.SHARED == ""


@pytest.mark.db
class TestSchema:
    def test_customers_table_exists(self, db_app, db_uri):
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.customers')")
            assert cur.fetchone()[0] is not None

    def test_every_customer_column_has_a_foreign_key(self, db_app, db_uri):
        """하나라도 빠지면 그 표만 오타를 받아준다. 그게 제일 찾기 어렵다."""
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.table_constraints "
                " WHERE constraint_type = 'FOREIGN KEY' AND table_schema = 'public' "
                "   AND constraint_name LIKE %s",
                ("%_customer_fkey",),
            )
            linked = {r[0] for r in cur.fetchall()}
        assert set(LINKED) <= linked, f"외래키가 없는 표: {sorted(set(LINKED) - linked)}"

    def test_audit_log_has_no_foreign_key_on_purpose(self, db_app, db_uri):
        """감사 기록은 어떤 경우에도 남아야 한다.

        외래키를 걸면 '고객사가 없다' 는 이유로 감사 기록 쓰기가 실패할 수
        있는데, 그건 참조가 끊긴 것보다 훨씬 나쁘다.
        """
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM information_schema.table_constraints "
                " WHERE constraint_type = 'FOREIGN KEY' AND table_name = 'audit_log' "
                "   AND constraint_name LIKE %s",
                ("%_customer_fkey",),
            )
            assert cur.fetchone()[0] == 0

    def test_the_shared_row_exists(self, db_app, db_uri):
        """'' 에 외래키가 걸리려면 그 이름이 실재해야 한다."""
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM customers WHERE name = ''")
            assert cur.fetchone()[0] == 1

    def test_shared_runbooks_still_work(self, db_app, db_uri):
        """공통 런북(customer = '')이 외래키에 걸려 저장 못 하면 안 된다."""
        import psycopg

        from app import runbook

        with db_app.app_context():
            rid = runbook.save(fingerprint="zzq_shared", title="공통 절차",
                               body="x", author="시험", customer="")
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM runbooks WHERE id = %s", (rid,))


@pytest.mark.db
class TestTyposAreBlocked:
    def test_unknown_customer_is_rejected(self, db_app, db_uri):
        """이 한 줄이 이 변경을 만든 이유다.

        예전에는 '가고객' 과 '가고객 '(뒤 공백)이 둘 다 저장됐다. 연락처는
        뒤쪽에 붙고 화면은 앞쪽을 보여줘서, 아무 오류 없이 사라졌다.
        """
        import psycopg

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                cur.execute(
                    "INSERT INTO runbooks (fingerprint, title, body, author, customer)"
                    " VALUES ('zzq_typo', 't', 'b', 'a', 'zzq없는고객')"
                )
            conn.rollback()


@pytest.mark.db
class TestLifecycle:
    @pytest.fixture
    def made(self, db_app, db_uri):
        name = "zzq시험고객"
        with db_app.app_context():
            try:
                customer.delete(name)
            except customer.CustomerError:
                pass
            customer.create(name, status="onboarding",
                            report_interval_days=30, note="계약 완료, 계정 대기")
        yield name
        with db_app.app_context():
            try:
                customer.delete(name)
            except customer.CustomerError:
                pass

    def test_can_exist_without_an_aws_account(self, db_app, made):
        """온보딩의 시작점이 시스템 안으로 들어왔다.

        예전에는 계정 ARN 을 받기 전에는 고객사를 넣을 방법이 없었다.
        """
        with db_app.app_context():
            assert customer.get(made)["status"] == "onboarding"
            assert made in customer.names()
            row = [r for r in customer.listing() if r["name"] == made][0]
            assert row["accounts"] == 0

    def test_report_interval_is_stored(self, db_app, made):
        """보고 주기가 고객사에 붙는다. 지금은 저장만 하고 아직 안 쓴다."""
        with db_app.app_context():
            assert customer.get(made)["report_interval_days"] == 30

    def test_update_changes_status(self, db_app, made):
        with db_app.app_context():
            customer.update(made, {"status": "active"})
            assert customer.get(made)["status"] == "active"

    def test_ended_drops_out_of_the_default_list(self, db_app, made):
        with db_app.app_context():
            customer.update(made, {"status": "ended"})
            assert made not in customer.names()
            assert made in customer.names(include_ended=True)

    def test_duplicate_is_rejected(self, db_app, made):
        with db_app.app_context():
            with pytest.raises(customer.CustomerError):
                customer.create(made)

    def test_unknown_status_is_rejected(self, db_app, made):
        with db_app.app_context():
            with pytest.raises(customer.CustomerError):
                customer.update(made, {"status": "이상한상태"})

    def test_update_rejects_unknown_fields(self, db_app, made):
        """폼에서 온 키를 그대로 SQL 에 이어붙이면 임의의 열을 고칠 수 있다."""
        with db_app.app_context():
            with pytest.raises(customer.CustomerError):
                customer.update(made, {"name": "다른이름"})


@pytest.mark.db
class TestRenameCascades:
    def test_renaming_carries_the_linked_tables(self, db_app, db_uri):
        """예전 구조였다면 표마다 UPDATE 를 돌려야 했고, 하나라도 빠뜨리면
        그 표의 자료가 조용히 고아가 됐다."""
        import psycopg

        old, new = "zzq이름바꿀고객", "zzq바뀐이름"
        with db_app.app_context():
            for n in (old, new):
                try:
                    customer.delete(n)
                except customer.CustomerError:
                    pass
            customer.create(old)
        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO customer_contacts (customer, name, kind) "
                " VALUES (%s, '연락처', 'primary')", (old,))

        with db_app.app_context():
            customer.rename(old, new)

        with psycopg.connect(db_uri) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM customer_contacts WHERE customer = %s", (new,))
            carried = cur.fetchone()[0]
            cur.execute("DELETE FROM customer_contacts WHERE customer = %s", (new,))
        with db_app.app_context():
            customer.delete(new)
        assert carried == 1, "이름을 바꿨는데 연락처가 따라오지 않았습니다"


@pytest.mark.db
class TestDeleteIsRefusedWhenDataRemains:
    def test_customer_with_accounts_cannot_be_deleted(self, db_app, db_uri):
        """조용히 고아를 만드는 것보다 지우지 못하는 편이 낫다."""
        with db_app.app_context():
            # 계정이 붙은 고객사여야 한다. 계정 없는 고객사는 실제로
            # 지워지고, 그러면 다른 테스트의 준비물을 망가뜨린다.
            existing = customer.names(with_accounts=True)
            if not existing:
                pytest.skip("계정이 붙은 고객사가 없습니다")
            with pytest.raises(customer.CustomerError) as e:
                customer.delete(existing[0])
            assert "지울 수 없습니다" in str(e.value)

    def test_reserved_name_is_protected(self, db_app, db_uri):
        with db_app.app_context():
            with pytest.raises(customer.CustomerError):
                customer.delete(customer.SHARED)
            with pytest.raises(customer.CustomerError):
                customer.rename(customer.SHARED, "뭐든지")
            assert customer.get(customer.SHARED) is not None


@pytest.mark.db
class TestScreen:
    @pytest.fixture
    def client(self, db_app, db_uri):
        db_app.config["WTF_CSRF_ENABLED"] = False
        c = db_app.test_client()
        c.post("/auth/login", data={"username": "admin", "password": "1234"})
        return c

    def test_form_creates_without_an_account(self, db_app, client):
        name = "zzq화면시험고객"
        with db_app.app_context():
            try:
                customer.delete(name)
            except customer.CustomerError:
                pass
        body = client.post("/customer/new",
                           data={"name": name, "status": "onboarding",
                                 "report_interval_days": "30", "note": "화면"},
                           follow_redirects=True).get_data(as_text=True)
        assert name in body
        with db_app.app_context():
            assert customer.get(name)["report_interval_days"] == 30
            customer.delete(name)

    def test_page_says_an_account_is_not_required(self, client):
        body = client.get("/customer/").get_data(as_text=True)
        assert "AWS 계정이 없어도" in body or "계정 대기" in body
