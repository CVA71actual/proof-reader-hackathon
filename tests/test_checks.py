import itertools

import pytest
from conftest import box_of, find_word

from checks.common import COULD_NOT_RUN, FAIL, PASS, CheckResult, normalize
from checks.selector_match import selector_match
from checks.status import FAILED, MATCHED, PROPOSED, combine_status
from checks.text_layer_match import text_layer_match

W, H = 612, 792


# ------------------------------------------------------------ normalize

@pytest.mark.parametrize("a,b", [
    ("(381,684 )", "(381,684)"),
    ("$ 115,315", "115,315"),
    ("$115,315", "115,315"),
    ("  1,325,395 ", "1,325,395"),
    ("( 2.38 )", "(2.38)"),
])
def test_normalize_equal(a, b):
    assert normalize(a) == normalize(b)


def test_normalize_keeps_digits_and_signs():
    assert normalize("(381,684)") != normalize("381,684")
    assert normalize("115,315") != normalize("115315")


# ------------------------------------------------------------ text_layer_match

@pytest.mark.parametrize("value", ["115,315", "(381,684 )", "(381,684)", "$ 115,315", "-", "1,325,395", "(2.38)"])
def test_text_layer_pass(words, value):
    r = text_layer_match(value, words)
    assert r.result == PASS, r.reason
    assert r.kind == "Code" and r.label == "Matches source text"


@pytest.mark.parametrize("value", ["115,316", "(381,685)", "943,717", "381,684"])
def test_text_layer_fail_on_changed_value(words, value):
    assert text_layer_match(value, words).result == FAIL


def test_text_layer_could_not_run_without_text_layer():
    r = text_layer_match("115,315", [])
    assert r.result == COULD_NOT_RUN
    assert "no text layer" in r.reason


def test_text_layer_could_not_run_without_value(words):
    assert text_layer_match(None, words).result == COULD_NOT_RUN


# ------------------------------------------------------------ selector_match

def test_selector_pass_with_right_box(words):
    w = find_word(words, "115,315")
    r = selector_match("115,315", box_of(w), words, W, H)
    assert r.result == PASS, r.reason
    assert r.compared["word_ids"] == [w["id"]]


def test_selector_pass_with_split_parenthesis(words):
    num = find_word(words, "(381,684")
    close = next(x for x in words if x["text"] == ")" and abs(x["y0"] - num["y0"]) < 1 and x["x0"] > num["x0"])
    r = selector_match("(381,684)", box_of(num, close), words, W, H)
    assert r.result == PASS, r.reason


def test_selector_fail_with_neighbouring_cell_box(words):
    neighbour = find_word(words, "(475,081")  # 2024 column, same row as (381,684
    close = next(x for x in words if x["text"] == ")" and abs(x["y0"] - neighbour["y0"]) < 1
                 and x["x0"] > neighbour["x0"])
    r = selector_match("(381,684)", box_of(neighbour, close), words, W, H)
    assert r.result == FAIL
    assert r.compared["found"] == "(475,081 )"


def test_selector_could_not_run_without_box(words):
    assert selector_match("115,315", None, words, W, H).result == COULD_NOT_RUN


@pytest.mark.parametrize("box", [[10, 10, 10, 20], [50, 50, 40, 60], [700, 10, 720, 20], [-50, -50, -10, -10],
                                 [1, 2, 3], ["a", 1, 2, 3]])
def test_selector_fail_on_empty_off_page_or_broken_box(words, box):
    assert selector_match("115,315", box, words, W, H).result == FAIL


def test_selector_fail_on_blank_area(words):
    r = selector_match("115,315", [300, 600, 400, 700], words, W, H)
    assert r.result == FAIL and r.compared["found"] == ""


# ------------------------------------------------------------ status

def _r(check, result):
    return CheckResult(check, check, result, "")


def test_status_matched_only_when_all_three_pass():
    for a, b, c in itertools.product([PASS, FAIL, COULD_NOT_RUN], repeat=3):
        s = combine_status([_r("text_layer_match", a), _r("selector_match", b), _r("row_column_match", c)])
        if a == b == c == PASS:
            assert s == MATCHED
        elif FAIL in (a, b, c):
            assert s == FAILED
        else:
            assert s == PROPOSED


def test_status_never_matched_with_a_missing_check():
    assert combine_status([_r("text_layer_match", PASS)]) == PROPOSED
    assert combine_status([]) == PROPOSED
    assert combine_status([_r("text_layer_match", PASS), _r("other", PASS)]) == PROPOSED
    assert combine_status([_r("text_layer_match", PASS), _r("selector_match", PASS)]) == PROPOSED


def test_box_alone_never_matches(words):
    # A box that holds the value but a value that is not on the page: not matched.
    w = find_word(words, "115,315")
    from checks.status import check_cell
    cell = check_cell({"column": "2025", "value": "115,316", "box": box_of(w)},
                      {"words": words, "page_width": W, "page_height": H})
    assert cell["status"] == FAILED
