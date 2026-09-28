"""`python -m testinghq.cli`, the invocation the e2e tests use.

It was a single file before the split and a package after, and this is the file
that keeps that invocation working. The suite builds commands with this exact
string in `tests/e2e/test_real_sockets.py`, so it is part of the interface
rather than a convenience.
"""
from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
