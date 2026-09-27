"""One exit-code convention, for every tool.

Every `testinghq` subcommand returns a process exit code, and the code is the
answer a script reads. Before this module there were three definitions of that
answer. Blast, Barrage and the pipeline tools agreed on 0/1/2; `compare` used
0/1/2 for something else entirely.

The collision was not cosmetic. A CI script written to treat `1` as "refused"
would read a `compare` regression, which `compare` reported as `1`, as a
refusal. One means "the tool declined to run", the other means "the tool ran and
found something". A pipeline that gates on that script stops the deploy for the
wrong reason, and the reason is not visible in the exit code.

So `compare` moves onto the shared convention. Its regression becomes
EXIT_FINDING, and its usage error becomes EXIT_REFUSED.

    EXIT_OK       0   ran, and the answer was yes
    EXIT_REFUSED  1   the tool declined: guardrail, bad config, bad usage
    EXIT_DRY_RUN  2   ran, and deliberately sent nothing
    EXIT_FINDING  3   ran, and the answer was no

The distinction that matters to a script is between the three non-zero codes,
because they call for three different responses. A refusal is a bug in the
invocation and nothing was sent, so fix the command. A dry run is a successful
run that was asked to hold back, so there is nothing to investigate. A finding
is a successful run that found something, so look at the report.

One deliberate exception, ARG_PARSE_ERROR. Argparse exits 2 for a usage error,
and 2 means a dry run here. A mistyped flag would therefore read as "nothing was
sent", which is the same class of misreading this module exists to remove.
`testinghq.cli` installs a parser that exits 1 instead, matching EXIT_REFUSED,
because a bad command line is a refusal to do the thing that was asked. Argparse
itself is untouched, so `--help` and `--version` still exit 0 and any other tool
using argparse keeps its standard behaviour.

This module is the only place in the package where these numbers are written
down. `tests/unit/test_exit_codes.py` asserts that, across every module,
including the ones that used to carry their own copies.
"""
from __future__ import annotations

#: The command ran and the answer was yes.
EXIT_OK = 0

#: The command declined. A guardrail refusal, a missing or malformed config, a
#: file that could not be read, a bad command line. Nothing was sent, and the
#: caller has to change something before the command can mean anything.
EXIT_REFUSED = 1

#: The command ran and deliberately sent nothing. A successful run, held back
#: by the absence of --send.
EXIT_DRY_RUN = 2

#: The command ran and the answer was no. A mismatch, a regression, a finding.
#: This is a result, not a failure to run, and a script that treats it as the
#: same thing as a refusal will stop a pipeline for the wrong reason.
EXIT_FINDING = 3

#: What argparse uses for a usage error, kept so the fix in `testinghq.cli` can
#: name the number it is replacing rather than a bare literal.
ARG_PARSE_ERROR = 2


# --- names other modules already import, kept as aliases -------------------
#
# Renaming a constant that half the package imports is churn that buys nothing
# and breaks anyone importing it. These are the same objects, not copies, so
# there is still exactly one number per meaning. `compare` keeps
# EXIT_NO_REGRESSION / EXIT_REGRESSION / EXIT_USAGE as names because they say
# what that tool means by each code, and both now carry the shared value.
#
# EXIT_MISMATCH is the pipeline family's word for EXIT_FINDING.

EXIT_MISMATCH = EXIT_FINDING

EXIT_NO_REGRESSION = EXIT_OK
EXIT_REGRESSION = EXIT_FINDING
EXIT_USAGE = EXIT_REFUSED

#: Every code a tool may return, in the order a reader should think about them.
#: Used by the convention test to check nothing new appeared.
ALL_EXIT_CODES = (EXIT_OK, EXIT_REFUSED, EXIT_DRY_RUN, EXIT_FINDING)

#: Human-readable meaning per code, for --help and error messages.
MEANINGS = {
    EXIT_OK: "ran, answer was yes",
    EXIT_REFUSED: "declined: guardrail, bad config, or bad usage",
    EXIT_DRY_RUN: "ran, sent nothing (no --send)",
    EXIT_FINDING: "ran, answer was no",
}
