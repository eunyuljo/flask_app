# tests/test_inventory.py
# 리소스 목록.
#
# 질의를 만드는 부분이라 DB 없이 볼 수 있는 게 적다. 대신 엑셀 쪽은
# 순수 함수라 손으로 먹여서 확인한다.

import pytest

from app import inventory
from app.inventory_xlsx import _value

xlsxwriter = pytest.importorskip("xlsxwriter")

from app.inventory_xlsx import build  # noqa: E402


class TestValueFormatting:
    """수집 안 한 것과 값이 없는 것을 구분해야 한다."""

    def test_missing_key_is_a_dash(self):
        """'—' 는 이 값을 아예 수집하지 않았다는 뜻이다."""
        assert _value({}, "public_ip") == "—"

    def test_present_but_null_is_blank(self):
        """키가 있는데 None 이면 '수집했고 값이 없다' 는 뜻이다."""
        assert _value({"public_ip": None}, "public_ip") == ""

    def test_bool_is_korean(self):
        assert _value({"public": True}, "public") == "예"
        assert _value({"public": False}, "public") == "아니오"

    def test_list_is_joined(self):
        """파이썬 표기(['sg-web'])가 그대로 나가면 안 된다."""
        assert _value({"security_groups": ["a", "b"]}, "security_groups") == "a, b"

    def test_dict_is_joined(self):
        assert _value({"tags": {"Env": "prod"}}, "tags") == "Env=prod"

    def test_nested_tag_lookup(self):
        assert _value({"tags": {"Name": "web-01"}}, ("tags", "Name")) == "web-01"

    def test_missing_nested_tag(self):
        assert _value({"tags": {}}, ("tags", "Name")) == ""

    def test_no_tags_at_all_is_a_dash(self):
        """태그를 못 다는 리소스와 태그가 비어 있는 리소스는 다르다."""
        assert _value({}, ("tags", "Name")) == "—"


def item(resource_type="ec2:instance", resource_id="i-1", **attrs):
    from datetime import datetime, timezone
    return {
        "resource_id": resource_id, "resource_type": resource_type,
        "attributes": attrs, "account_id": "123456789012",
        "region": "ap-northeast-2",
        "collected_at": datetime(2026, 8, 21, tzinfo=timezone.utc),
    }


def result(items, truncated=False, total=None):
    from datetime import datetime, timezone
    return {
        "items": items, "truncated": truncated,
        "total": total if total is not None else len(items),
        "snapshots": [{"snapshot_id": 1, "account_id": "123456789012",
                       "region": "ap-northeast-2",
                       "collected_at": datetime(2026, 8, 21, tzinfo=timezone.utc)}],
    }


def load(buf):
    openpyxl = pytest.importorskip("openpyxl")
    from io import BytesIO
    return openpyxl.load_workbook(BytesIO(buf.getvalue()))


class TestWorkbook:
    def test_sheet_per_type(self):
        """한 시트에 다 넣으면 종류마다 의미 있는 열이 달라 빈 칸이 많아진다."""
        wb = load(build(result([
            item("ec2:instance", "i-1"),
            item("s3:bucket", "b-1"),
        ])))
        assert "ec2 instance" in wb.sheetnames
        assert "s3 bucket" in wb.sheetnames

    def test_sheet_name_has_no_colon(self):
        """엑셀 시트 이름에는 : 를 못 쓴다."""
        wb = load(build(result([item("ec2:instance")])))
        assert all(":" not in n for n in wb.sheetnames)

    def test_empty_result_says_so(self):
        wb = load(build(result([])))
        assert "결과 없음" in wb.sheetnames

    def test_truncation_is_disclosed(self):
        """잘라냈으면 잘랐다고 적어야 한다."""
        wb = load(build(result([item()], truncated=True, total=9999)))
        text = str([list(r) for r in wb["요약"].iter_rows(values_only=True)])
        assert "9999" in text and "일부만" in text

    def test_filters_are_recorded(self):
        """어떤 조건으로 뽑은 목록인지 없으면 나중에 다시 못 만든다."""
        wb = load(build(result([item()]), {"account_id": "123456789012", "q": "prod"}))
        text = str([list(r) for r in wb["요약"].iter_rows(values_only=True)])
        assert "prod" in text

    def test_unknown_type_still_gets_a_sheet(self):
        """수집 종류가 늘어나도 열 정의가 없다고 빠지면 안 된다."""
        wb = load(build(result([item("lambda:function", "fn-1", runtime="python3.9")])))
        assert "lambda function" in wb.sheetnames


@pytest.mark.db
class TestQueries:
    def test_current_runs(self, db_app, db_uri):
        with db_app.app_context():
            r = inventory.current()
            assert isinstance(r["items"], list)
            assert r["total"] >= len(r["items"])

    def test_type_filter_narrows(self, db_app, db_uri):
        with db_app.app_context():
            everything = inventory.current()["total"]
            only = inventory.current(resource_type="s3:bucket")
            assert only["total"] <= everything
            assert all(i["resource_type"] == "s3:bucket" for i in only["items"])

    def test_missing_tag_filter(self, db_app, db_uri):
        with db_app.app_context():
            r = inventory.current(missing_tag="Owner")
            for i in r["items"]:
                assert not (i["attributes"].get("tags") or {}).get("Owner")

    def test_missing_tag_skips_untaggable_resources(self, db_app, db_uri):
        """태그를 못 다는 리소스까지 '태그 없음' 으로 잡으면 목록이 쓸모없어진다."""
        with db_app.app_context():
            for i in inventory.current(missing_tag="Env")["items"]:
                assert "tags" in i["attributes"]

    def test_search_is_parameterised(self, db_app, db_uri):
        """따옴표가 든 검색어로 질의가 깨지면 주입 통로가 있다는 뜻이다."""
        with db_app.app_context():
            r = inventory.current(q="' OR 1=1 --")
            assert r["total"] == 0

    def test_only_the_latest_snapshot_per_scope(self, db_app, db_uri):
        """여러 스냅샷을 합치면 이미 지운 리소스가 살아 있는 것처럼 보인다."""
        with db_app.app_context():
            r = inventory.current()
            seen = {(s["account_id"], s["region"]) for s in r["snapshots"]}
            assert len(seen) == len(r["snapshots"])

    def test_facets(self, db_app, db_uri):
        with db_app.app_context():
            f = inventory.facets()
            assert isinstance(f["types"], list)
            assert isinstance(f["tag_keys"], list)

    def test_scopes(self, db_app, db_uri):
        with db_app.app_context():
            for s in inventory.scopes():
                assert s["account_id"] and s["region"]
