import hashlib

from conftest import FIXTURES


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_page_hash_matches_manifest(manifest):
    assert sha(FIXTURES / manifest["page_file"]) == manifest["page_sha256"]


def test_words_hash_matches_manifest(manifest):
    assert sha(FIXTURES / manifest["words_file"]) == manifest["words_sha256"]


def test_manifest_names_the_pinned_filing(manifest):
    assert manifest["ticker"] == "IONS"
    assert manifest["form"] == "ARS"
    assert manifest["accession"] == "0001140361-26-016395"
    assert manifest["source_sha256"] == "aabcaf5ef7714c9390fc5bd135d9ab0f5e77ffc559241ad4b5427802ac2f09a2"
    assert manifest["page_number"] == 95 and manifest["source_page_count"] == 144


def test_words_file_non_empty(words_doc, manifest):
    words = words_doc["words"]
    assert len(words) == manifest["word_count"] > 100
    assert words_doc["page_width"] == 612 and words_doc["page_height"] == 792
    for i, w in enumerate(words):
        assert w["id"] == f"w{i}"
        assert w["text"]
        assert w["x0"] < w["x1"] and w["y0"] < w["y1"]
        assert {"block", "line", "word"} <= w.keys()
    texts = {w["text"] for w in words}
    assert {"115,315", "(381,684", ")", "$", "-", "OPERATIONS"} <= texts


def test_full_filing_not_in_repo():
    pdfs = [p.name for p in FIXTURES.parent.rglob("*.pdf") if ".git" not in p.parts]
    assert pdfs == ["ions_ars_2026_p95.pdf"]
