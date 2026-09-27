"""The pipeline seam: reading what a system under test actually produced.

Blast and Barrage judge a run by the HTTP status, which is the right question
for "did it crash" and the wrong question for everything an intake pipeline
actually gets wrong. This package is the seam that closes the gap, and the
ground truth the tools built on it will check against.

  readback      the adapter protocol and the `Readback` record it returns
  messages      the per-message tag that makes a sent payload findable again
  expectations  ground truth in, a PASSED/FAILED/SKIPPED verdict out
  adapters      turning `[readback]` configuration into a live adapter
  common        the send and read phases, and the shared exit codes
  corpus        the payloads all three tools send, and their tags

A tool is not in this package. `verify`, `ledger` and `redeliver` live beside
it, one per concern, and none of them imports another.

Two rules run through all of it, and they are the difference between this
finding bugs and manufacturing green.

A check that could not run reports SKIPPED, never PASSED, and says which of the
three causes it was. A field the adapter cannot see is an observation gap, not a
passing result, and the summaries print what was skipped as prominently as what
was found. The most dangerous outcome here is not a wrong verdict, it is a
right-looking verdict for a run that looked at nothing.

Ground truth is only ever applied to payloads that have a correct parse to
grade. A deliberately mangled payload is Blast's job; these tools grade results.
"""
from __future__ import annotations

from .adapters import (
    AdapterError,
    BUILTIN_KINDS,
    HttpJsonAdapter,
    MailboxAdapter,
    ReadbackConfig,
    build_adapter,
    parse_readback_config,
    require_readback_target,
    resolve_header_values,
)
from .common import (
    EXIT_DRY_RUN,
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    ReadbackOutcome,
    SentMessage,
    read_back_all,
    require_synthetic,
    resolve_target_url,
    send_all,
)
from .corpus import (
    DEFAULT_RATE,
    DEFAULT_SEED,
    build_clean_corpus,
    build_items,
    build_tagged_corpus,
)
from .expectations import (
    CHECKS,
    SKIP_NOTHING_TO_CHECK,
    SKIP_NOT_VISIBLE,
    SKIP_NO_RECORD,
    SKIP_REASONS,
    Expectations,
    GroundTruthMatcher,
    Verification,
    check_thread_link,
    check_thread_together,
    evaluate,
    evaluate_sequence,
)
from .messages import DEFAULT_TAG_PREFIX, make_tag, stamp, stamp_corpus, tag_marker
from .readback import (
    FunctionAdapter,
    MultiAdapter,
    Probe,
    Readback,
    ReadbackAdapter,
    ReadbackError,
    can_enumerate,
)

__all__ = [
    "AdapterError",
    "BUILTIN_KINDS",
    "CHECKS",
    "DEFAULT_RATE",
    "DEFAULT_SEED",
    "DEFAULT_TAG_PREFIX",
    "EXIT_DRY_RUN",
    "EXIT_MISMATCH",
    "EXIT_OK",
    "EXIT_REFUSED",
    "Expectations",
    "FunctionAdapter",
    "GroundTruthMatcher",
    "HttpJsonAdapter",
    "MailboxAdapter",
    "MultiAdapter",
    "Probe",
    "Readback",
    "ReadbackAdapter",
    "ReadbackConfig",
    "ReadbackError",
    "ReadbackOutcome",
    "SKIP_NOTHING_TO_CHECK",
    "SKIP_NOT_VISIBLE",
    "SKIP_NO_RECORD",
    "SKIP_REASONS",
    "SentMessage",
    "Verification",
    "build_adapter",
    "build_clean_corpus",
    "build_items",
    "build_tagged_corpus",
    "can_enumerate",
    "check_thread_link",
    "check_thread_together",
    "evaluate",
    "evaluate_sequence",
    "make_tag",
    "parse_readback_config",
    "read_back_all",
    "require_readback_target",
    "require_synthetic",
    "resolve_header_values",
    "resolve_target_url",
    "send_all",
    "stamp",
    "stamp_corpus",
    "tag_marker",
]
