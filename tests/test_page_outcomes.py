from runs import page_outcomes
from runs.sources import visual_evidence


def _page(sid, ticker="AAA", company="A", number=1, **record):
    return {"source_id": sid, "record": {"ticker": ticker, "company": company,
                                         "page_number": number, **record}}


def test_page_facts_keep_capture_attempt_read_failure_skip_and_unread_distinct():
    meta = {}
    outcomes = page_outcomes.initialize(meta, [_page("read"), _page("bad", number=2),
                                               _page("skip", number=3), _page("later", number=4)], {}, {})
    page_outcomes.update(meta, "read", captured=True, attempted=True, read=True)
    page_outcomes.update(meta, "bad", captured=True, attempted=True, unreadable=True, reason="parse failed")
    page_outcomes.update(meta, "skip", skipped=True, reason="capture hash changed")

    by_id = {x["source_id"]: x for x in outcomes}
    assert by_id["read"]["read"] and not by_id["read"]["unread"] and not by_id["read"]["unreadable"]
    assert by_id["bad"]["captured"] and by_id["bad"]["attempted"]
    assert by_id["bad"]["unreadable"] and not by_id["bad"]["read"] and by_id["bad"]["unread"]
    assert by_id["skip"]["skipped"] and by_id["skip"]["unread"] and not by_id["skip"]["attempted"]
    assert by_id["later"]["unread"] and not by_id["later"]["attempted"]
    assert meta["pages_read"] == 1 and meta["pages_unread"] == 3


def test_legacy_source_proves_capture_not_successful_interpretation():
    meta = {}
    outcomes = page_outcomes.initialize(meta, [_page("legacy")],
                                        {"sources": [{"id": "legacy"}], "rows": []}, {})
    assert outcomes[0]["captured"] and not outcomes[0]["attempted"]
    assert not outcomes[0]["read"] and outcomes[0]["unread"]
    assert meta["pages_read"] == 0


def test_page_outcome_summary_is_per_company_and_counts_empty_success_as_read():
    outcomes = [{"ticker": "AAA", "company": "A", "captured": True, "attempted": True,
                 "read": True, "unreadable": False, "skipped": False, "unread": False},
                {"ticker": "BBB", "company": "B", "captured": True, "attempted": True,
                 "read": False, "unreadable": True, "skipped": False, "unread": True}]
    [a, b] = page_outcomes.by_company(outcomes)
    assert (a["ticker"], a["read"], a["unread"]) == ("AAA", 1, 0)
    assert (b["ticker"], b["unreadable"], b["unread"]) == ("BBB", 1, 1)
    view = page_outcomes.read_view(outcomes)
    assert view["recorded"] and view["items"] == outcomes and view["counts"]["read"] == 1


def test_scan_read_requires_every_region_to_parse_but_empty_events_is_a_read():
    empty = page_outcomes.interpretation([{"events": [], "parse_ok": True}], attempted=True)
    malformed = page_outcomes.interpretation([{"events": None, "parse_ok": False,
                                                "parse_problem": "not JSON"}], attempted=True)
    partial = page_outcomes.interpretation([{"events": [], "parse_ok": True},
                                             {"events": None, "parse_ok": False,
                                              "parse_problem": "bad second region"}], attempted=True)
    assert empty["read"] and not empty.get("unreadable")
    assert not malformed["read"] and malformed["unreadable"] and malformed["reason"] == "not JSON"
    assert not partial["read"] and partial["unreadable"]


def test_captured_scan_without_a_reader_attempt_is_not_called_unreadable():
    pending = page_outcomes.interpretation([], attempted=False)
    assert pending["captured"] and not pending["attempted"] and pending["unread"]
    assert not pending["read"] and not pending["unreadable"]


def test_visual_evidence_rejects_hidden_slivers_and_approximate_transcription_geometry():
    hidden = {"words": [{"id": f"w{i}", "text": "word", "y0": 20, "y1": 20.56} for i in range(196)]}
    ordinary = {"words": [{"id": "w1", "text": "word", "y0": 20, "y1": 32}]}
    assert visual_evidence({}, hidden)["origin"] == "hidden_text"
    assert visual_evidence({}, hidden)["overlays_supported"] is False
    assert visual_evidence({"derived_words": {"geometry": "approximate"}}, ordinary)["origin"] == "approximate"
    assert visual_evidence({}, ordinary)["overlays_supported"] is True


def test_visual_evidence_reads_picture_transcription_markers_from_the_words_artifact():
    from runs import picture

    native_height = {"words": [{"id": "w1", "text": "phase", "x0": 10, "y0": 20,
                                "x1": 35, "y1": 30}]}
    for marker in ({"kind": picture.KIND}, {"words_from": picture.WORDS_FROM}):
        evidence = visual_evidence({}, {**native_height, **marker})
        assert evidence["origin"] == "approximate"
        assert evidence["overlays_supported"] is False


def test_hidden_text_can_never_aggregate_to_a_matched_event():
    from checks import event_checks
    from runs.recheck import row_checks

    words = [{"id": f"w{i}", "text": token, "x0": i * 12, "y0": 20, "x1": i * 12 + 10, "y1": 20.56}
             for i, token in enumerate("Company expects results by June 2027".split())]
    doc = {"page_width": 612, "page_height": 792, "words": words}
    page = {"page_number": 1, "visual_evidence": visual_evidence({}, doc)}
    source = {"id": "src", "kind": "filing", "published_date": "2026-01-01", "pages": [page]}
    row = {"quote": "Company expects results by June 2027", "box": [0, 0, 150, 30],
           "page_number": 1, "date_text": "June 2027", "date": None, "date_end": None,
           "event_type": "milestone", "title": "Results", "program": None}

    checks = row_checks(row, source, doc, consumed_artifact={"file": "p.words.json", "sha256": "a" * 64,
                                                             "kind": "captured"})
    by_name = {check.check: check for check in checks}
    assert by_name[event_checks.QUOTE_ID].result == "pass"
    assert by_name["selector_match"].result == "could_not_run"
    assert event_checks.row_status(checks) != event_checks.MATCHED
