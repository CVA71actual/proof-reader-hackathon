# accuracy_legacy_128d299

`accuracy.json` is a legacy Accuracy export, exactly as written by the base
writer `accuracy/export.py` at commit `128d2997b9b9976cd0ed091441924c3789ecb04d`
(call: `write(d, rep, now=NOW, tip='128d299', dirty=False)`).

How it was produced: the writer source was taken from that commit
(`git show 128d2997…:accuracy/export.py`), loaded as a module, and run once on a
temporary copy of `runs/sample` scored by `tests/test_pr138_adviser.stale_case`.
The report was scored BEFORE the run was edited, so it records Muse Spark
found = 69 of N = 69. `stale_case` then edits one cell of `run.json`
(value `115,316`, status failed), so a fresh score of the same run gives found = 68.
The fixture is therefore a stale 69 that must not be served against 68-input.

It has no `words_sha256` (the legacy format), and its `content_sha256` is valid
for the legacy content. Generated once; do not regenerate with a newer writer.
