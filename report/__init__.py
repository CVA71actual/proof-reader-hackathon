"""Code-built PDF and Word report from saved run results (M1)."""

__all__ = ["build"]


def __getattr__(name: str):
    if name == "build":
        from report.build import build as _build
        return _build
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
