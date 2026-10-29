"""Muse Wonder: the stronger model a row is escalated to when a check fails or the two readers disagree.

The same call as capture/reader.py LiveReader (Meta Model API, https://api.meta.ai/v1, key MODEL_API_KEY from the
env loader, never printed), to model `muse-wonder-1.0`, the id the Meta Model API lists for it. It is sent one row's
page region (a PNG) and the failing check's reason, never the whole run; what it answers is a new proposal that goes
through the same code checks (runs/escalate.py). Its price is a placeholder in runs/prices.py until confirmed.
"""

from __future__ import annotations

from capture.reader import LiveReader

NAME = "Muse Wonder"
MODEL = "muse-wonder-1.0"


class WonderReader(LiveReader):
    name = NAME
    model = MODEL
