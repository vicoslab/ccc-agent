#!/usr/bin/env python3
"""Entry point for a human-operated official desktop/remote-client run."""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tests.user_facing_acceptance.manual_observed_driver import main


if __name__ == "__main__":
    raise SystemExit(main())
