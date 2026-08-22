# tests/test_compliance_xlsx.py
# 컴플라이언스 점검 결과를 엑셀로 내보내는 부분.
#
# 만든 파일을 다시 열어서 확인한다. "예외 없이 끝났다" 만 보면
# 빈 시트를 내보내도 통과한다.
#
# 읽는 쪽은 openpyxl 을 쓴다. 쓰기용(xlsxwriter)과 읽기용을 다른 것으로
# 두면, 한쪽의 버그를 다른 쪽이 덮어주지 않는다.

from datetime import datetime, timedelta, timezone

import pytest

from app import compliance as C

xlsxwriter = pytest.importorskip("xlsxwriter")

from app.compliance_xlsx import build, ExcelNotAvailable  # noqa: E402


NOW = datetime(2026, 8, 21, 16, 53, tzinfo=timezone.utc)


def violation(check_id="sg-admin-port-open", resource_id="sg-web",
              severity="critical", excused=False, reason=""):
    check = C.CHECKS_BY_ID[check_id]
    return {
        "check_id": check_id, "title": check["title"], "severity": severity,
        "standard": check["standard"], "why": check["why"],
        "resource_id": resource_id, "detail": "SSH(22/tcp) 가 0.0.0.0/0 에 열려 있습니다",
        "excused": excused, "excuse_reason": reason,
    }


def report(account_id="123456789012", customer="A커머스", violations=None,
           points=None, repeats=None, exceptions=None):
    violations = violations if violations is not None else [violation()]
    return {
        "account_id": account_id, "region": "us-east-1", "customer": customer,
        "snapshot_id": 14, "collected_at": NOW, "resources": 9,
        "violations": violations,
        "summary": C.summarize(violations),
        "points": points if points is not None else [{
            "snapshot_id": 14, "collected_at": NOW, "resources": 9,
            "keys": set(), "all_keys": set(), "summary": C.summarize(violations),
            "opened": [], "closed": [], "first": True,
        }],
        "repeats": repeats or [],
        "since": {("sg-admin-port-open", "sg-web"): NOW},
        "exceptions": exceptions or [],
    }


def load(buf):
    openpyxl = pytest.importorskip("openpyxl")
    from io import BytesIO
    return openpyxl.load_workbook(BytesIO(buf.getvalue()))


def values(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


class TestWorkbook:
    def test_produces_a_real_xlsx(self):
        data = build([report()]).getvalue()
        # xlsx 는 zip 이다. 앞 두 바이트가 PK 가 아니면 엑셀이 열지 못한다.
        assert data[:2] == b"PK"

    def test_sheet_names(self):
        wb = load(build([report()]))
        assert wb.sheetnames == ["요약", "위반 상세", "시간축", "재발", "예외", "점검 항목"]

    def test_every_sheet_has_a_frozen_header(self):
        """스크롤하면 머리글이 사라지는 표는 읽기 어렵다."""
        wb = load(build([report()]))
        for name in wb.sheetnames:
            assert wb[name].freeze_panes, name

    def test_tables_have_autofilter(self):
        """받은 사람이 심각도로 걸러 보는 건 당연한 동작이다."""
        wb = load(build([report()]))
        for name in ("위반 상세", "시간축", "재발", "예외", "점검 항목"):
            assert wb[name].auto_filter.ref, name


class TestViolationSheet:
    def test_violation_appears(self):
        wb = load(build([report()]))
        rows = values(wb["위반 상세"])
        assert any("sg-web" in str(r) for r in rows[1:])

    def test_customer_name_is_included(self):
        """계정 번호만 찍히면 누구 것인지 모른다."""
        wb = load(build([report(customer="A커머스")]))
        assert wb["위반 상세"].cell(row=2, column=1).value == "A커머스"

    def test_sorted_by_severity_across_accounts(self):
        """계정별로 묶으면 두 번째 계정의 critical 이 첫 계정의 low 아래로 간다."""
        low = violation("missing-required-tags", "i-1", "low")
        critical = violation("sg-admin-port-open", "sg-2", "critical")
        wb = load(build([
            report("111111111111", "가고객", [low]),
            report("222222222222", "나고객", [critical]),
        ]))
        first = wb["위반 상세"].cell(row=2, column=3).value
        assert first == "critical"

    def test_blank_columns_for_the_reader(self):
        """담당자·조치 결과 열이 없으면 받은 사람이 옆에 새 열을 만들고,
        그러면 다음 달 파일과 모양이 달라진다."""
        wb = load(build([report()]))
        head = values(wb["위반 상세"])[0]
        assert head[-2:] == ["담당자", "조치 결과"]
        assert wb["위반 상세"].cell(row=2, column=10).value is None

    def test_excused_violation_carries_its_reason(self):
        v = violation(excused=True, reason="고객사 승인 - 점검 기간 한정")
        wb = load(build([report(violations=[v])]))
        row = values(wb["위반 상세"])[1]
        assert "예외" in row
        assert "고객사 승인 - 점검 기간 한정" in row

    def test_no_violations_says_so(self):
        wb = load(build([report(violations=[])]))
        assert "위반이 없습니다." in str(values(wb["위반 상세"])[1])


class TestSummarySheet:
    def test_totals_across_accounts(self):
        wb = load(build([
            report("111111111111", "가고객", [violation()]),
            report("222222222222", "나고객", [violation(resource_id="sg-9")]),
        ]))
        rows = values(wb["요약"])
        # KPI 값 줄에 위반 합계 2 가 들어 있다.
        assert any(r[0] == 2 for r in rows if isinstance(r[0], int))

    def test_lists_every_account(self):
        wb = load(build([
            report("111111111111", "가고객"),
            report("222222222222", "나고객"),
        ]))
        text = str(values(wb["요약"]))
        assert "111111111111" in text and "222222222222" in text

    def test_says_what_the_basis_is(self):
        """Config 결과로 오해하면 '없는 위반' 을 없다고 읽는다."""
        wb = load(build([report()]))
        assert "스냅샷" in str(values(wb["요약"])[2])


class TestOtherSheets:
    def test_checks_sheet_lists_all_checks(self):
        wb = load(build([report()]))
        text = str(values(wb["점검 항목"]))
        for check in C.CHECKS:
            assert check["title"] in text

    def test_checks_sheet_states_the_limit(self):
        """'위반 0건' 이 무슨 뜻인지 알려면 무엇을 안 봤는지도 있어야 한다."""
        wb = load(build([report()]))
        assert "점검하지 않았습니다" in str(values(wb["점검 항목"])[1])

    def test_recurring_sheet(self):
        repeats = [{"check_id": "sg-admin-port-open", "title": "관리 포트가 인터넷에 열려 있음",
                    "severity": "critical", "resource_id": "sg-web", "times": 3}]
        wb = load(build([report(repeats=repeats)]))
        assert "sg-web" in str(values(wb["재발"]))

    def test_recurring_sheet_when_empty(self):
        wb = load(build([report()]))
        assert "재발한 항목이 없습니다." in str(values(wb["재발"]))

    def test_exceptions_sheet(self):
        exc = [{"account_id": "123456789012", "check_id": "s3-versioning",
                "resource_id": "b", "reason": "고객사 승인", "approved_by": "홍길동",
                "expires_at": NOW + timedelta(days=30)}]
        wb = load(build([report(exceptions=exc)]))
        text = str(values(wb["예외"]))
        assert "고객사 승인" in text and "홍길동" in text

    def test_timeline_marks_the_first_snapshot(self):
        wb = load(build([report()]))
        assert "비교 대상 없음" in str(values(wb["시간축"]))


class TestTimezone:
    def test_aware_datetimes_do_not_crash(self):
        """xlsxwriter 는 시간대가 붙은 datetime 을 거부한다."""
        assert build([report()]).getvalue()[:2] == b"PK"

    def test_written_as_utc(self):
        """로컬 시각으로 바꾸면 같은 점검을 두 사람이 뽑을 때 다른 문서가 나온다."""
        kst = timezone(timedelta(hours=9))
        r = report()
        r["collected_at"] = NOW.astimezone(kst)
        wb = load(build([r]))
        # 요약 표의 수집 시각 칸(5열)
        cell = wb["요약"].cell(row=10, column=5).value
        assert cell.hour == NOW.hour
        assert cell.tzinfo is None


class TestMissingLibrary:
    def test_friendly_error_when_xlsxwriter_is_absent(self, monkeypatch):
        """엑셀 내려받기 하나 때문에 앱 전체가 죽으면 안 된다."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "xlsxwriter":
                raise ImportError("없음")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(ExcelNotAvailable):
            build([report()])
