"""Job 41: the quote check names the near miss. No model call, no network, no key."""

from __future__ import annotations

from checks.common import COULD_NOT_RUN, FAIL, PASS
from checks.event_checks import quote_match

PAGE = ("The company expects topline data for zelicapavir in the second half of 2027 and plans "
        "to file the application shortly afterwards.")


def _words(text):
    return [{"id": f"w{i}", "text": t} for i, t in enumerate(text.split())]


QUOTE = "The company expects topline data for zelicapavir in the second half of 2027"


def test_exact_match_unchanged():
    r = quote_match(QUOTE, _words(PAGE))
    assert r.result == PASS and r.near_match is None and r.differs == []


def test_one_word_misspelling_names_the_pair():
    r = quote_match(QUOTE.replace("zelicapavir", "zelicacapavir"), _words(PAGE))
    assert r.result == FAIL
    assert r.reason == ("The quote nearly matches the page; 1 word differs — "
                        "quote ‘zelicacapavir’, page ‘zelicapavir’.")
    assert r.near_match == QUOTE
    assert r.differs == [("zelicacapavir", "zelicapavir")]


def test_two_words_plural():
    q = "The company expects topline data for zelicacapavir in the second half of 2027 and plans to file the application shortly afterwards"
    q = q.replace("shortly", "shorty")
    r = quote_match(q, _words(PAGE))
    assert r.result == FAIL and r.reason.startswith("The quote nearly matches the page; 2 words differ — ")
    assert r.differs == [("zelicacapavir", "zelicapavir"), ("shorty", "shortly")]
    assert "not printed anywhere" not in r.reason


def test_below_ninety_percent_is_old_sentence():
    q = "The company expects topline data for zelicacapavir in the third half of 2028"   # 3 of 14 differ
    r = quote_match(q, _words(PAGE))
    assert r.result == FAIL and r.reason == "The quote is not printed anywhere on the page."
    assert r.near_match is None and r.differs == []


def test_near_match_inside_longer_page():
    page = "Intro words here. " + PAGE + " Closing words follow, with more text."
    r = quote_match(QUOTE.replace("2027", "2037"), _words(page))
    assert r.result == FAIL and r.near_match == QUOTE
    assert r.differs == [("2037", "2027")]


def test_only_three_pairs_listed():
    q = "a b c d e f g h i j k l m n o p q r s t u v w x y z"
    page = "a b c d e f g h i j k l m n o p q r s t u v w x y z"
    qq = "A B c d e f g h i j k l m n o p q r s t u v w x y z".replace("A", "a1").replace("B", "b1")
    r = quote_match(qq, _words(page))
    assert r.result == FAIL and len(r.differs) == 2
    big = "a1 b1 c1 d1 " + "e f g h i j k l m n o p q r s t u v w x y z e f g h i j k l m n o p q r s t u v w x y z"
    r = quote_match(big, _words("a b c d " + "e f g h i j k l m n o p q r s t u v w x y z e f g h i j k l m n o p q r s t u v w x y z"))
    assert r.result == FAIL and len(r.differs) == 4 and r.reason.count("page ‘") == 3 and "(and 1 more)" in r.reason


def test_empty_quote_unchanged():
    r = quote_match("", _words(PAGE))
    assert r.result == COULD_NOT_RUN and r.reason == "The reader gave no quote for this row."
