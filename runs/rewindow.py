"""Kept for the Job 39 import path: the put-back lives in runs.recheck_dates (python -m runs.recheck_dates)."""
import sys

from runs.recheck_dates import RULE, STEP, main, rewindow  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
