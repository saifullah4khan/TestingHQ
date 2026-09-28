"""TestingHQ's command line, one module per command.

`testinghq/cli.py` was 1570 lines and 61 KB: ten commands, their argument
parsers, their handlers, the readback option plumbing shared by five of them,
and the dispatch table, in one file. Finding out what commands existed meant
reading all of it, and a change to one command meant a diff through the other
nine.

This package is that file, split by what changes together:

    main.py       the assembly and the dispatch table. Knows what commands
                  exist; nothing else does.
    common.py     the pieces five or more commands share: the readback
                  options, the parser subclass, target resolution.
    blast.py      generate, fire, replay, and the send path they share.
    barrage.py    load testing, and compare, whose parser is built here.
    pipeline.py   verify, ledger, redeliver, loop, steady. The five that all
                  take a readback, which is why they live together.
    utility.py    report and config validate: read something, send nothing.

`build_parser` and `main` are re-exported here because they are the public
surface: `pyproject.toml` points its entry point at `testinghq.cli:main`, and
`python -m testinghq.cli` goes through `__main__.py`. Everything else is
imported from the module that owns it, so a reader looking for a command's logic
finds one file rather than a 1570-line search.

The bodies were moved verbatim, by AST line span, from the original file. Only
the module docstrings and the import lists are new. The test suite is what
established that.
"""
from __future__ import annotations

from . import barrage, blast, common, pipeline, utility
from .common import (
    DEFAULT_RATE,
    DEFAULT_TARGET_CONFIG,
    _add_readback_args,
    _add_readback_poll_args,
    _not_yet,
    _resolve_readback,
    _resolve_target_url,
)
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
