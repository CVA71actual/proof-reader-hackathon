import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
FIXTURES = REPO / "fixtures"


@pytest.fixture(scope="session")
def manifest():
    return json.loads((FIXTURES / "MANIFEST.json").read_text())


@pytest.fixture(scope="session")
def words_doc(manifest):
    return json.loads((FIXTURES / manifest["words_file"]).read_text())


@pytest.fixture(scope="session")
def words(words_doc):
    return words_doc["words"]


def find_word(words, text):
    return next(w for w in words if w["text"] == text)


def box_of(*ws, pad=0.5):
    return [min(w["x0"] for w in ws) - pad, min(w["y0"] for w in ws) - pad,
            max(w["x1"] for w in ws) + pad, max(w["y1"] for w in ws) + pad]
