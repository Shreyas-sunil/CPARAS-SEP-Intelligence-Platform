"""
Regression tests for utils/json_utils.extract_json_safe.

Run: python -m pytest backend/tests/ -q   (or: python backend/tests/test_json_utils.py)

Each case corresponds to a real LLM output shape observed from small local
models. Two are regressions for bugs that caused *silent* data loss rather
than an error, which is why they are pinned explicitly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.json_utils import extract_json_safe  # noqa: E402


def test_plain_object():
    assert extract_json_safe('{"patents":[{"n":1}]}') == {"patents": [{"n": 1}]}


def test_markdown_fenced():
    assert extract_json_safe('```json\n{"a":1}\n```') == {"a": 1}


def test_unterminated_fence():
    """Generation cut off before the closing fence."""
    assert extract_json_safe('```json\n{"a":1}') == {"a": 1}


def test_preamble_and_trailing_commentary():
    raw = 'Sure! Here is the JSON:\n{"a":1}\nHope that helps!'
    assert extract_json_safe(raw) == {"a": 1}


def test_trailing_commas_are_repaired():
    assert extract_json_safe('{"patents":[{"n":1},],}') == {"patents": [{"n": 1}]}


def test_backticks_inside_a_string_value_survive():
    """REGRESSION: global ``` stripping silently corrupted field contents.

    'Encoding using ```delta``` frames' previously parsed as
    'Encoding using deltaframes' — wrong data, no error, straight into the
    database and the report.
    """
    raw = '{"title":"Encoding using ```delta``` frames"}'
    assert extract_json_safe(raw)["title"] == "Encoding using ```delta``` frames"


def test_top_level_array_is_not_truncated_to_one_element():
    """REGRESSION: a bare top-level array lost every element but the first.

    The old implementation searched for the first '{', which inside
    '[{...},{...}]' is the first *element*, so a 40-patent response returned
    as an array yielded exactly one patent with no error anywhere.
    """
    raw = "[" + ",".join('{"n":%d}' % i for i in range(40)) + "]"
    assert len(extract_json_safe(raw)["patents"]) == 40


def test_top_level_array_respects_array_key():
    assert extract_json_safe('[{"n":1}]', array_key="patent_disclosures") == {
        "patent_disclosures": [{"n": 1}]
    }


def test_braces_and_commas_inside_strings():
    raw = '{"title":"Method {with} braces, ]"}'
    assert extract_json_safe(raw)["title"] == "Method {with} braces, ]"


def test_unicode_escapes():
    assert extract_json_safe('{"declarant":"Nokia \\u00d6y"}')["declarant"] == "Nokia Öy"


def test_malformed_inputs_return_none_not_raise():
    for raw in ("", None, "no json here", '{"patents":[{"n":', "[[[", "{{{"):
        assert extract_json_safe(raw) is None


def test_only_first_of_two_objects_is_taken():
    assert extract_json_safe('{"a":1}\n{"b":2}') == {"a": 1}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\n{len(fns)}/{len(fns)} passed")
