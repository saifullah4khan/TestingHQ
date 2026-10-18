"""TestingHQ's command line, one module per command.

    main.py       the assembly and the dispatch table. Knows what commands
                  exist; nothing else does.
    common.py     the pieces several commands share: the readback options,
                  the parser subclass, target resolution.
    blast.py      generate, fire, replay, and the send path they share.
    barrage.py    load testing, and compare, whose parser is built here.
    pipeline.py   the tools that take a readback.
    utility.py    report and config validate: read something, send nothing.

`build_parser` and `main` are the public surface: `pyproject.toml` points its
entry point at `testinghq.cli:main`, and `python -m testinghq.cli` goes
through `__main__.py`.
"""
from __future__ import annotations

from . import barrage, blast, common, pipeline, utility
from .common import DEFAULT_RATE, DEFAULT_TARGET_CONFIG
from .main import build_parser, main

__all__ = [
    "DEFAULT_RATE",
    "DEFAULT_TARGET_CONFIG",
    "barrage",
    "blast",
    "build_parser",
    "common",
    "main",
    "pipeline",
    "utility",
]
