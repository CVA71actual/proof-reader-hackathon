"""The saved, person-editable research assignment used by event readers."""

DEFAULT = (
    "Identify developments relevant to this company's investment catalyst calendar. Read broadly, including clinical, "
    "regulatory, commercial, financing and other meaningful developments. Include announced earnings dates, undated "
    "plans and conditional events. Explain what is expected to happen, why it matters, and what conditions or "
    "uncertainty apply. Distinguish upcoming developments from consequential historical context. Routine financial "
    "figures alone are not calendar events. Preserve the source's meaning, timing and qualifications. Cite the "
    "supporting passage. Do not invent missing information; leave uncertainty visible for the person reviewing it."
)
MAX_LENGTH = 4000


def validate(value) -> str:
    """Blank or missing input selects the default; nonblank text is preserved exactly."""
    if value is None:
        return DEFAULT
    if not isinstance(value, str):
        raise ValueError("Research instructions must be text or null.")
    if len(value) > MAX_LENGTH:
        raise ValueError(f"Research instructions must be {MAX_LENGTH} characters or fewer.")
    return value if value.strip() else DEFAULT


def prompt_block(instructions: str | None, as_of: str | None) -> str:
    if instructions is None:
        return ""
    date_line = f"Run reference date: {as_of}. Use this date to distinguish upcoming from historical developments.\n" if as_of else ""
    return ("Research assignment (guide what to notice; do not invent or change source evidence):\n"
            f"{instructions}\n{date_line}")
