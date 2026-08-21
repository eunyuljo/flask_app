# tests/test_resources.py
# 리소스 정규화 / 다이제스트 / 필드 비교 테스트.
#
# 여기가 틀리면 "바뀌지 않은 것이 바뀐 것처럼" 보인다. 작업 증적과 사후
# 보고서가 전부 이 판정 위에 서 있으므로, 잡음이 섞이면 문서가 못 미더워진다.

from app.resources import normalize, digest_of, _field_diff, NOISY_KEYS


class TestNormalize:
    def test_noisy_keys_dropped(self):
        assert NOISY_KEYS, "무시할 키 목록이 비어 있으면 이 테스트가 무의미하다"
        key = sorted(NOISY_KEYS)[0]
        assert key not in normalize({key: "값", "keep": 1})

    def test_lists_are_sorted(self):
        """목록 순서만 다른 것은 같은 상태로 봐야 한다.

        AWS 응답은 같은 내용을 다른 순서로 주는 일이 흔하다.
        """
        a = normalize({"sg": ["b", "a", "c"]})
        b = normalize({"sg": ["c", "b", "a"]})
        assert a == b

    def test_nested_lists_sorted(self):
        a = normalize({"outer": {"inner": ["z", "a"]}})
        b = normalize({"outer": {"inner": ["a", "z"]}})
        assert a == b

    def test_real_change_survives(self):
        a = normalize({"instance_type": "t3.small"})
        b = normalize({"instance_type": "t3.large"})
        assert a != b


class TestDigest:
    def test_same_content_same_digest(self):
        assert digest_of({"a": 1, "b": [2, 3]}) == digest_of({"b": [3, 2], "a": 1})

    def test_different_content_different_digest(self):
        assert digest_of({"a": 1}) != digest_of({"a": 2})

    def test_noise_does_not_change_digest(self):
        key = sorted(NOISY_KEYS)[0]
        assert digest_of({"a": 1}) == digest_of({"a": 1, key: "아무값"})

    def test_digest_is_short_string(self):
        d = digest_of({"a": 1})
        assert isinstance(d, str) and 8 <= len(d) <= 64


class TestFieldDiff:
    def test_detects_changed_value(self):
        changes = _field_diff({"type": "t3.small"}, {"type": "t3.large"})
        assert len(changes) == 1
        assert changes[0]["field"] == "type"
        assert changes[0]["before"] == "t3.small"
        assert changes[0]["after"] == "t3.large"

    def test_detects_added_and_removed(self):
        fields = {c["field"] for c in _field_diff({"a": 1}, {"b": 2})}
        assert fields == {"a", "b"}

    def test_ignores_noisy_keys(self):
        """다이제스트가 무시하는 키는 필드 비교에서도 무시해야 한다.

        한쪽만 무시하면 "변경 없음인데 변경 목록에는 뜨는" 상태가 된다.
        """
        key = sorted(NOISY_KEYS)[0]
        assert _field_diff({key: "예전"}, {key: "지금"}) == []

    def test_list_order_is_not_a_change(self):
        assert _field_diff({"sg": ["a", "b"]}, {"sg": ["b", "a"]}) == []

    def test_list_content_change_is_detected(self):
        changes = _field_diff({"sg": ["a"]}, {"sg": ["a", "b"]})
        assert len(changes) == 1

    def test_no_change_returns_empty(self):
        assert _field_diff({"a": 1, "b": 2}, {"a": 1, "b": 2}) == []
