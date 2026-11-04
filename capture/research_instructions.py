"""The saved, person-editable research assignment used by event readers."""

# The short assignment: page mode's default (legacy, off the demo path; its pinned prompt is unchanged), and what the
# research box held before CVA's one-box ruling (2026-11-03). A saved research note equal to it maps to the full
# RESEARCH_DEFAULT below, so those runs read what they always read.
DEFAULT = (
    "Identify developments relevant to this company's investment catalyst calendar. Read broadly, including clinical, "
    "regulatory, commercial, financing and other meaningful developments. Include announced earnings dates, undated "
    "plans and conditional events. Explain what is expected to happen, why it matters, and what conditions or "
    "uncertainty apply. Distinguish upcoming developments from consequential historical context. Routine financial "
    "figures alone are not calendar events. Preserve the source's meaning, timing and qualifications. Cite the "
    "supporting passage. Do not invent missing information; leave uncertainty visible for the person reviewing it."
)
LEGACY_DEFAULT = PAGE_DEFAULT = DEFAULT
# BRIEF_V2 (CVA 2026-11-02, glass-queue/prompts/BRIEF_V2_2026-11-02.txt): where a research run looks, verbatim. The
# block is kept verbatim in a data file next to this module: its wording is CVA's instruction to the model (it names
# regulators' decisions on marketed or filed assets), not a claim made by this code.
WHERE_TO_LOOK = (__import__("pathlib").Path(__file__).with_name("brief_v2_where_to_look.txt")
                 .read_text(encoding="utf-8").strip())
# BRIEF_V1 (2026-11-03 morning, run 9): the short assignment plus the where-to-look block. Kept so a saved note equal
# to it is still recognized as a default box; it is no longer what a blank box selects.
RESEARCH_DEFAULT_V1 = DEFAULT + "\n\n" + WHERE_TO_LOOK
# CVA (2026-11-03, after run 9): the research brief is ONE visible, editable text and the whole research policy. It is
# sent FIRST, as the leading block of the researcher's instructions; the code-owned technical contract
# (runs/research_contract.CONTROLLER: tool mechanics and output format only) follows it. Nothing else steers the
# research: no policy in code, no silent append. The default wording is a data file next to this module.
RESEARCH_DEFAULT = (__import__("pathlib").Path(__file__).with_name("research_brief_default.txt")
                    .read_text(encoding="utf-8").strip())


def research_assignment(note: str | None) -> str:
    """The research loop's assignment: exactly the box. Blank or missing selects RESEARCH_DEFAULT; the old short
    default maps to RESEARCH_DEFAULT (legacy runs). Nothing is appended, so a block is never sent twice and a deleted
    block stays deleted."""
    if note is None or not note.strip() or note == LEGACY_DEFAULT:
        return RESEARCH_DEFAULT
    return note


def is_default(note: str | None) -> bool:
    """The default box, in its research (full) or page/legacy (short) form."""
    return note in (RESEARCH_DEFAULT, RESEARCH_DEFAULT_V1, DEFAULT)


MAX_LENGTH = 4000


def validate(value, default: str = DEFAULT) -> str:
    """Blank or missing input selects `default` (page mode: DEFAULT; research: RESEARCH_DEFAULT); nonblank text is
    preserved exactly."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError("Research instructions must be text or null.")
    if len(value) > MAX_LENGTH:
        raise ValueError(f"Research instructions must be {MAX_LENGTH} characters or fewer.")
    return value if value.strip() else default


def prompt_block(instructions: str | None, as_of: str | None) -> str:
    if instructions is None:
        return ""
    date_line = f"Run reference date: {as_of}. Use this date to distinguish upcoming from historical developments.\n" if as_of else ""
    return ("Research assignment (guide what to notice; do not invent or change source evidence):\n"
            f"{instructions}\n{date_line}")
