# Proof Reader

I research investment catalysts for biotech companies: trial starts and readouts, regulatory decisions, launches and investor events for the next six months. I do this with several AI models, and I review their claims against the sources they cite. For the Meta Global AI Developer Hackathon 2026 (Indie track, "Build for Your World"), I tried to automate that workflow with Meta's models. Proof Reader is the result.

This README is an honest after-action report: what worked, what did not, and why.

**Status:** the workflow is demonstrated in saved runs. Research quality, repeatability and practical value remain to be evaluated. We have not established reliable autonomous research.

Demo video: [YouTube link]

## How it works

1. **Muse Spark** (Meta Model API) works as a research agent for one company. It searches the web (Brave Search), captures pages, and proposes each event with an exact quote from a captured page.
2. **Code** then runs mechanical checks on each proposal: is the quote on the captured page, is the citation located, is the date printed in the quote, and does the source follow the source rule. Each check is recorded as passed, failed or not checked. These checks do not establish whether an event is true, current or complete.
3. **Muse Spark Open** (Meta's open-weight model, served on Baseten as `meta-models/muse-spark-open-1026`) reviews the rows. It looks for stale guidance and missing events, and can fetch sources itself. DeepSeek V4 Pro is a fallback only if Muse Spark Open is unavailable. The reviewer is from the same model family as the researcher, and the app says so: it is a second look, not independent verification.
4. **Muse Wonder** (Meta Model API) proposes a resolution for each disagreement it is sent. Its ruling is never a human acceptance.
5. **LettuceDetect**, a model-based support check, may flag wording for a person to inspect. No flag is not proof of correctness.
6. **A person decides every row**: keep, fix or remove, by click or by voice (**Muse Voice Transcribe**). Voice only pre-fills; nothing is saved until the person clicks.
7. Only kept rows reach the **calendar**, and the **PDF and Word reports** are built by code from the saved run.

The research brief is one visible, editable box. The exact request sent to the models is frozen and saved with each run.

## Demo runs (PTGX, Protagonist Therapeutics, 4 Nov 2026)

- **Run 17** (build b3eaa46):
  - Research: 207 turns, 27 sources captured, 9 rows generated, including one placed after the window and one the researcher excluded as already launched.
  - Review: Muse Spark Open assessed all 9 rows and made 3 findings. Code closed the review at its turn limit, and one source the reviewer fetched was left unconcluded.
  - LettuceDetect: 0 flags.
  - Recorded spend: $4.19.
  - The person kept 8 rows and removed 1. The PDF and Word reports contain exactly the 8 kept rows.
- **Run 18** (same build, recorded live for the video):
  - 109 turns, 8 rows.
  - Review complete with 4 findings, each about a dropped "expected" or "anticipated" qualifier.
  - Recorded spend: $2.79.

The run folders, roughly 100 to 200 MB each with captured pages, are not in this repo.

## What helped, and why

The mechanical parts and the saved records:
- exact quotes tied to captured, fingerprinted pages
- recorded check results
- spending and turn limits
- stage labels that say what actually ran
- one saved request per run

Saved records helped us investigate failures. These parts also needed fixes along the way. For example, an early run let the same conference be accepted twice as two separate rows. Code can reject a repeated action on one row, but semantic duplicates across different rows still need detection and review.

## What remained unreliable, and why

**Research coverage and judgment.** On an exploratory list of 15 candidate items for one company, our runs found roughly 3 to 11. That list is not a validated recall score. Broader, longer runs often found more, but our tests did not isolate the effects of instructions, tools or limits. Run 17 missed partner investor events.

The reviewer was uneven. In run 17 it flagged stale launch guidance on a row the researcher had already excluded, and it wrongly asked to add a past approval.

## Why LLMs were hard to build on

Our experiments exposed four practical risks in our implementation:

1. **Models did not reliably follow instructions.** Across runs, the reviewer was told to give every row a verdict and then finish. Instead it looped for 92 calls, replied in prose instead of calling tools, filed findings without verdicts, and left sources it had opened unconcluded. Code either recorded the review as incomplete, or closed it under explicit completion rules with the remaining limitations kept.
2. **Outputs changed with prompts and settings.** After we added one sentence ("each conference is its own event"), four investor conferences came back as four rows instead of one. Other settings also changed, and we did not isolate prompt sensitivity in controlled repeats.
3. **Results varied between runs.** The reviewer caught stale guidance in one run and missed similar issues in another. Inputs and settings also differed.
4. **Passing tests did not establish live quality.** More than 3,000 passing tests did not represent the failures that live runs exposed. Live runs, and audits against the sources afterwards, found them.

What we would keep: code that constrains what can be accepted and preserves evidence, honest stage labels, and a person deciding. Checking a model's format does not make its judgment correct. Models explore, code checks what can be checked mechanically, and a person judges.

## Meta products used

- **Muse Spark** through the Meta Model API: the research agent.
- **Muse Spark Open**, open weights on Baseten: the reviewer.
- **Muse Wonder** through the Meta Model API: proposed resolutions for disagreements.
- **Muse Voice Transcribe**: spoken decisions.

Also tried and not in the final path:
- Muse Spark Open reading slide-style pages as images
- SAM 3.1 for text regions
- letting the researcher call the checks itself

Not used: Muse Code.

## Run it

### Without a key (saved runs, no network, no cost)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m runs.demo
```

This serves the app locally on the saved runs included here (`ptgx-r5`, `twelve-4`, `sample`) from the earlier page-reading version. See [docs/BUILD_NOTES_PAGE_READER.md](docs/BUILD_NOTES_PAGE_READER.md).

### With keys (new research runs cost money)

Put your keys in `~/.config/proof-reader/env`, one per line. The code never prints or saves them.
- `MODEL_API_KEY`: the Meta Model API key, for Muse Spark, Muse Wonder and Muse Voice Transcribe.
- `BASETEN_API_KEY`: Muse Spark Open.
- `BRAVE_API_KEY`: Brave Search.

Then:

```sh
set -a; . ~/.config/proof-reader/env; set +a
.venv/bin/python -m uvicorn server.app:app --port 8790
```

Open http://127.0.0.1:8790, click **New run**, choose a company, set a spending limit and a turn limit, and review the brief. Then click **Run now**. A one-company run cost about $3 to $5 in our tests. LettuceDetect is optional: set `LETTUCE_PYTHON` and `LETTUCE_WEIGHTS_PATH` to a local install of `KRLabsOrg/lettucedect-v2-mmbert-base`.

## Built during the hackathon

This repository was created on 14 Oct 2026 with only a placeholder README (one commit). All product code was written during the hackathon, from 26 Oct to 4 Nov 2026, and the full commit history is included. Development used AI coding assistants (Claude, Codex, Kimi) under my direction, with reviews by other models.

## Licence

MIT. See [LICENSE](LICENSE).
