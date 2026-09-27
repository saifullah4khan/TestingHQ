"""The pipeline tools: what the system under test actually produced.

Blast and Barrage judge a run by the HTTP status, which is the right question
for "did it crash" and the wrong question for everything an intake pipeline
actually gets wrong. This package holds the three tools that ask the rest of
them, all built on one shared seam so they agree about what a system said:

  verify     after a run, check each payload against the ground truth the
             generator already produced: was a ticket created, is the sender
             right, is the subject right, is the body intact, are the
             attachments there, was it routed correctly.
  ledger     send N uniquely tagged messages and count what the pipeline
             produced: missing (lost emails), extra (duplicate or stray
             tickets), and wrong. "Did we lose any customer emails during the
             spike?" is the question every intake owner has.
  redeliver  test delivery semantics: a provider retrying, delivering the same
             message twice, and delivering a reply before its original. Checks
             for duplicate tickets and correct threading.

MODULE MAP, and the dependency direction. `readback` and `messages` know nothing
about the other two layers and are imported by everything. `expectations` is
pure comparison logic over those two. `adapters` turns configuration into a
live adapter. `common` holds the send/read phases and the exit codes. `verify`,
`ledger` and `redeliver` are the three tools, and they never import each other
except that they share the corpus builders in `verify`, which is where the
clean-corpus construction lives because it is the thing all three need and the
thing that must stay identical between them.

Two rules run through all of it, and they are the difference between this
package finding bugs and manufacturing green:

  A check that could not run reports SKIPPED, never PASSED. An adapter that
  cannot see a field says so, and the report says so, and a run that verified
  nothing says so too.
  Ground truth is Blast's, and it is only ever applied to payloads that have a
  correct parse to grade. A deliberately mangled payload is Blast's job; these
  tools grade results.
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
)
from .common import (
    EXIT_DRY_RUN,
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    SentMessage,
    read_back_all,
    require_synthetic,
    resolve_target_url,
    send_all,
)
from .expectations import (
    CHECKS,
    Expectations,
    GroundTruthMatcher,
    Verification,
    check_thread_link,
    check_thread_together,
    evaluate,
    evaluate_sequence,
)
from .messages import make_tag, stamp, stamp_corpus, tag_marker
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
    "SentMessage",
    "Verification",
    "build_adapter",
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
    "resolve_target_url",
    "send_all",
    "stamp",
    "stamp_corpus",
    "tag_marker",
]
