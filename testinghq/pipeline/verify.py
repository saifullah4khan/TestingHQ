"""verify: check what the pipeline produced, not what it answered.

Blast asks whether the endpoint responded. Verify asks what follows that
question, and the one intake owners have: was a ticket created, is the sender
right, is the subject right, is the body intact, are the attachments there, was
it routed correctly. Each is compared against ground truth the generator already
produced, read back out of the system under test through an adapter.

WHY IT ONLY FIRES CLEAN PAYLOADS. This decision shapes the tool more than
anything else. A garbled payload has no correct parse: `blast/corrupt.py`
deliberately mangles subjects and splices in mojibake so the parser can be
pushed until it breaks. Verifying a mangled payload against its own ground truth
would report a failure every time a mutator did its job, and a tool that fails
on success is worse than no tool. So verify fires the clean corpus only, as
Barrage fires clean payloads and for the same structural reason: Blast owns
messy input, this owns correctness of the result. `verify check` can be pointed
at a mixed blast run, where it verifies the clean records and reports how many it
skipped and why.

The exit code is the answer. 0 means every check that ran passed, 3 that one did
not, 1 a refusal, 2 a dry run. A run that returns 0 has told you the pipeline
parsed correctly, which is the thing no status code could tell you."""
from __future__ import annotations

import json
import random
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..blast.attachments import generate_attachment
from ..blast.generate import generate_corpus
from ..blast.payload import InboundEmail
from ..core import report
from ..core.transport import TransportResult
from . import messages
from .adapters import ReadbackConfig, build_adapter
from .common import (
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    SentMessage,
    close_adapter,
    DEFAULT_MAX_WAIT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUIET_WINDOW,
    ReadbackOutcome,
    read_back_all,
    require_synthetic,
    resolve_target_url,
    send_all,
)
from .expectations import (
    SKIP_NOTHING_TO_CHECK,
    SKIP_NOT_VISIBLE,
    SKIP_NO_RECORD,
    SKIP_REASONS,
    Expectations,
    Verification,
    evaluate_sequence,
)
from .readback import Readback


DEFAULT_RATE = 5.0
DEFAULT_COUNT = 20
DEFAULT_SEED = 0

#: Roughly how many of the verified payloads carry attachments. Not 1.0, because
#: "the attachment check skipped" and "the attachment check passed" are
#: different findings and a corpus where every payload has a file would only
#: ever produce the first kind of evidence. Not 0.0 either, and this is not a
#: cosmetic choice: `blast.generate.generate_corpus` never attaches anything,
#: so without this the attachment check could never run at all in the tool whose
#: whole purpose is to run it.
DEFAULT_ATTACHMENT_RATE = 0.35

#: Cap on how many attachments one verified payload may carry. One is enough to
#: prove the pipeline stores files at all, and more makes a run slow for no
#: extra signal. The attachment catalogue in `blast/attachments.py` spans
#: ordinary files, zero-byte ones, oversized ones and deliberately awkward
#: ones; this picks the ordinary and zero-byte kinds only, because an oversized
#: or path-traversal-shaped file being dropped is the pipeline enforcing a
#: limit, which is a policy question rather than a parse bug.
_VERIFIED_ATTACHMENT_KINDS = ("clean", "clean", "zero_byte")

#: Cap on how many failing payloads are listed in the printed report. A run
#: where every message was misrouted is one finding, not two hundred, and the
#: counts stay exact regardless. Mirrors the rule in `compare/runs.py`: the
#: numbers are always exact, only the detail list truncates, and it says so.
MAX_LISTED = 20


def build_clean_corpus(
    seed: int, count: int, attachment_rate: float = DEFAULT_ATTACHMENT_RATE
) -> List[InboundEmail]:
    """The clean corpus, with a deterministic subset of payloads carrying
    attachments.

    Shared by all five pipeline tools, because the attachment check has to be
    live in all of them or it is not a check. `blast.generate.generate_corpus`
    never attaches anything, so this is where the attachment evidence comes
    from, and having one implementation is what stops the tools from
    quietly verifying different corpora.

    Deterministic in (seed, count, attachment_rate) and nothing else. The
    per-payload rng is seeded from (seed, index) rather than drawn from one
    stream, so the payload at index 7 is the same whether it was generated
    alone or as part of a corpus of forty. A shared stream would make a payload
    depend on how many were generated before it, which is the kind of coupling
    that makes a replay stop reproducing a run.
    """
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if not 0.0 <= attachment_rate <= 1.0:
        raise ValueError(
            f"attachment_rate must be between 0 and 1, got {attachment_rate}"
        )

    corpus = generate_corpus(seed, count)
    with_files: List[InboundEmail] = []
    for index, email in enumerate(corpus):
        rng = random.Random(f"testinghq:attachments:{seed}:{index}")
        if rng.random() < attachment_rate:
            kind = rng.choice(_VERIFIED_ATTACHMENT_KINDS)
            with_files.append(
                replace(
                    email,
                    attachments=tuple(
                        generate_attachment(rng, kind)
                        for _ in range(rng.randint(1, 2))
                    ),
                )
            )
        else:
            with_files.append(email)
    return with_files


def build_tagged_corpus(
    seed: int,
    count: int,
    tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
    attachment_rate: float = DEFAULT_ATTACHMENT_RATE,
) -> List[Tuple[InboundEmail, str, str]]:
    """`build_clean_corpus`, each payload stamped with its own tag.

    Deterministic in (seed, count, tag_prefix, attachment_rate) and nothing
    else, so a verify artifact is reproducible: rerunning with the same inputs
    produces the same payload bytes, the same tags, the same attachment bytes,
    and the same payload hashes, which is what lets `verify check` find records
    belonging to a run that finished yesterday.
    """
    return messages.stamp_corpus(
        build_clean_corpus(seed, count, attachment_rate), seed, tag_prefix
    )


def build_items(
    corpus: Sequence[Tuple[InboundEmail, str, str]]
) -> List[Tuple[InboundEmail, str, str, messages.Probe]]:
    """Pair each stamped payload with the probe an adapter will be handed.

    The hash is computed here, once, and carried in the probe, so the payload
    identity the report publishes and the payload identity the adapter was asked
    about cannot be two different calculations of the same thing.
    """
    return [
        (email, tag, record_id, messages.probe_for(email, tag, record_id, report.payload_sha256(email)))
        for email, tag, record_id in corpus
    ]


def run_config(
    seed: int,
    count: int,
    tag_prefix: str,
    target_name: Optional[str],
    readback: ReadbackConfig,
    route: Optional[str],
    body_exact: bool,
    readback_poll: Dict[str, float],
) -> Dict[str, Any]:
    """Everything needed to reproduce the run, and nothing that varies between
    two runs of the same command. No latency, no wall clock, no timestamps."""
    return {
        "tool": "verify",
        "seed": seed,
        "count": count,
        "tag_prefix": tag_prefix,
        "target": target_name,
        "readback": readback.to_json(),
        "expect_route": route,
        "body_exact": body_exact,
        "readback_poll": dict(readback_poll),
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(
    verifications: Sequence[Verification],
    sent: Sequence[SentMessage],
    readback_outcome: Optional[ReadbackOutcome] = None,
) -> Dict[str, Any]:
    """The summary block.

    Skips are grouped by cause rather than by check name, because the three
    causes mean opposite things: a field the adapter cannot see is a gap in the
    integration, a missing record is the finding itself, and a payload with no
    attachments is a question that did not apply. Under one heading the report
    would be making a claim about the adapter that is false every time a
    payload simply had nothing to check.
    """
    found = sum(1 for v in verifications if v.found)
    failed = [v for v in verifications if not v.passed]

    grouped: Dict[str, Dict[str, int]] = {reason: {} for reason in SKIP_REASONS}
    for verification in verifications:
        for reason, checks in verification.skips_by_reason().items():
            for name, count in checks.items():
                grouped.setdefault(reason, {})[name] = (
                    grouped.setdefault(reason, {}).get(name, 0) + count
                )

    by_check: Dict[str, int] = {}
    for verification in verifications:
        for check in verification.checks:
            if check.passed is True:
                by_check[check.name] = by_check.get(check.name, 0) + 1

    unanswered = sum(
        1 for message in sent if message.result.sent and message.result.status is None
    )
    non_2xx = sum(
        1
        for message in sent
        if message.result.sent
        and message.result.status is not None
        and not 200 <= message.result.status < 300
    )

    summary: Dict[str, Any] = {
        "sent": len(sent),
        "found": found,
        "verified": len(verifications) - len(failed),
        "failed": len(failed),
        "transport_unanswered": unanswered,
        "transport_non_2xx": non_2xx,
        "checks_passed_by_name": by_check,
        "checks_skipped_by_reason": grouped,
    }
    if readback_outcome is not None:
        summary["readback"] = readback_outcome.to_json()
    return summary


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    sent: Sequence[SentMessage],
    verifications: Sequence[Verification],
    readbacks: Dict[str, List[Readback]],
    readback_outcome: Optional[ReadbackOutcome] = None,
) -> Dict[str, Any]:
    """The verify artifact: blast's shape, plus what was actually verified.

    Each record carries the same `id`, `category`, `payload_sha256`, `intended`,
    `response` and `assertion` a blast record carries, so a verify run is
    comparable with `testinghq compare` and the on-disk shape is one people
    already know. The `verification` block is additive, which is why
    `core/report.py` needed no change and why the shipped blast fixtures are
    untouched.

    The assertion is the engine's: PASSED only when every check that ran
    passed, and the mismatches are the failing checks' own sentences, so a
    verify artifact read by anything that already knows how to read a blast
    artifact shows a real failure rather than a status code.
    """
    by_tag = {v.tag: v for v in verifications}
    records = []
    for message in sent:
        verification = by_tag.get(message.tag)
        records.append(
            {
                "id": message.record_id,
                "category": report.CLEAN,
                "tag": message.tag,
                "message_id": message.probe.message_id,
                "payload_sha256": report.payload_sha256(message.email),
                "intended": Expectations.from_email(message.email).to_json(),
                "response": message.response_json(),
                "assertion": {
                    "passed": bool(verification and verification.passed),
                    "mismatches": verification.mismatch_strings() if verification else [],
                },
                "readback": [
                    r.to_json() for r in readbacks.get(message.tag, [])
                ],
                "verification": verification.to_json() if verification else None,
            }
        )

    return {
        "seed": seed,
        "config": config,
        "summary": summarize(verifications, sent, readback_outcome),
        "records": records,
    }


def format_verification(artifact: Dict[str, Any]) -> str:
    """Render the artifact for a terminal.

    Leads with the answer, because the question this tool exists for is binary:
    did the pipeline parse these correctly. Then the numbers that stop a clean
    verdict from being a lie: how many payloads were found at all, how long the
    readback took to settle, and which checks never ran and why.
    """
    summary = artifact.get("summary") or {}
    config = artifact.get("config") or {}
    records = artifact.get("records") or []

    failed = summary.get("failed", 0)
    checked = summary.get("verified", 0) + failed
    verdict = "VERIFIED" if failed == 0 else "MISMATCHED"
    if checked == 0:
        verdict = "NOTHING CHECKED"

    lines = [
        f"verify: {verdict}  ({checked} payload(s) checked, seed="
        f"{artifact.get('seed')}, tag prefix {config.get('tag_prefix')!r})"
    ]
    lines.append("")
    lines.append(f"  sent:              {summary.get('sent', 0)}")
    lines.append(f"  found in system:   {summary.get('found', 0)}")
    lines.append(f"  verified:          {summary.get('verified', 0)}")
    lines.append(f"  mismatched:        {failed}")

    readback = summary.get("readback")
    if readback:
        settled = "settled" if readback.get("stable") else "GAVE UP before settling"
        lines.append(
            f"  readback:          {readback.get('elapsed_s')}s over "
            f"{readback.get('polls')} poll(s), {settled}"
        )
        if readback.get("missing_tags"):
            lines.append(
                f"                    still missing {len(readback['missing_tags'])}"
            )

    unanswered = summary.get("transport_unanswered", 0)
    non_2xx = summary.get("transport_non_2xx", 0)
    if unanswered or non_2xx:
        lines.append(
            f"  transport:         {unanswered} unanswered, {non_2xx} non-2xx "
            "(these do not decide the verdict; the readback does)"
        )

    grouped = summary.get("checks_skipped_by_reason") or {}
    if any(grouped.get(reason) for reason in SKIP_REASONS):
        lines.append("")
        lines.append("  NOT CHECKED, by why:")
        for reason in SKIP_REASONS:
            checks = grouped.get(reason) or {}
            if not checks:
                continue
            total = sum(checks.values())
            detail = ", ".join(f"{name} {count}" for name, count in sorted(checks.items()))
            lines.append(f"    {reason} ({total}): {detail}")

    by_name = summary.get("checks_passed_by_name") or {}
    if by_name:
        lines.append("")
        lines.append("  checks passed:")
        for name, count in sorted(by_name.items()):
            lines.append(f"    {name}: {count}")

    failures = [r for r in records if not r.get("assertion", {}).get("passed", True)]
    if failures:
        lines.append("")
        lines.append(f"  mismatches ({len(failures)}):")
        for record in failures[:MAX_LISTED]:
            detail = "; ".join(record["assertion"]["mismatches"]) or "not verified"
            lines.append(f"    {record['id']}  {detail}")
        if len(failures) > MAX_LISTED:
            lines.append(
                f"    ... and {len(failures) - MAX_LISTED} more, not shown"
            )

    return "\n".join(lines)


def format_dry_run(
    corpus: Sequence[Tuple[InboundEmail, str, str]],
    readback: ReadbackConfig,
    tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
) -> str:
    """What a dry run prints instead of firing. Describes what WOULD be sent
    and, more importantly, what WOULD be asked afterwards, because the adapter
    is the half of this tool an operator is most likely to have got wrong and
    a dry run is the cheapest place to notice."""
    lines = [
        f"dry-run preview: {len(corpus)} clean payload(s), tag prefix {tag_prefix!r}"
    ]
    lines.append(f"  readback adapter:  kind {readback.kind!r}")
    if readback.url:
        lines.append(f"  readback url:      {readback.url}")
    if readback.path:
        lines.append(f"  readback path:     {readback.path}")
    if readback.spec:
        lines.append(f"  readback spec:     {readback.spec}")
    lines.append(f"  first tag:         {corpus[0][1] if corpus else '(none)'}")
    lines.append("  every payload is clean by construction: verify grades results,")
    lines.append("  and a deliberately mangled payload has no correct parse to grade")
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def grade(
    sent: Sequence[SentMessage],
    readbacks: Dict[str, List[Readback]],
    route: Optional[str],
    body_exact: bool,
) -> List[Verification]:
    """Grade every sent payload against what the system reported.

    Carries the payload, not just the record, which is what lets the attachment
    check compare filenames rather than a count. Every field a check needs is
    either in the payload's own ground truth or in the readback; nothing here
    is reconstructed from a summary.
    """
    verifications = []
    for message in sent:
        expectations = Expectations.from_email(message.email, route=route, body_exact=body_exact)
        verifications.append(
            evaluate_sequence(
                message.email,
                readbacks.get(message.tag, []),
                expectations=expectations,
                record_id=message.record_id,
                tag=message.tag,
            )
        )
    return verifications


def execute(
    seed: int,
    count: int,
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    *,
    readback: ReadbackConfig,
    tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
    rate: float = DEFAULT_RATE,
    route: Optional[str] = None,
    body_exact: bool = False,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Fire the clean corpus, read back what the system made of it, and report.

    Never called unless `guardrails.evaluate_send` already said yes. `client`
    and `readback_client` are the two injectable HTTP clients, and both are
    None in real CLI use; tests inject fakes at both, which is what keeps this
    path testable without a socket. The first version of the replay path in
    this repository had no injectable client at all and the integration test
    that found it spent eighty-one seconds on real connection attempts, so both
    are parameters from the start rather than an oversight waiting to be
    discovered.
    """
    corpus = build_tagged_corpus(seed, count, tag_prefix)
    items = build_items(corpus)

    try:
        require_synthetic([email for email, _tag, _rid in corpus])
        url = resolve_target_url(target_name, config_path)
    except Exception as exc:
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    adapter = None
    try:
        # Building the adapter is inside the try, not before it. An adapter
        # that cannot be built is exactly as much a refusal as a target that
        # cannot be resolved, and leaving this line outside meant an
        # unimportable `--readback module:attr` escaped as a traceback rather
        # than as the clean refusal the CLI is supposed to print.
        adapter = build_adapter(readback, client=readback_client)
        sent = send_all(items, url, rate, client=client, sleep=sleep, clock=clock)
        outcome = read_back_all(
            adapter,
            [probe for _e, _t, _r, probe in items],
            sleep=sleep,
            clock=clock,
            quiet_window=quiet_window,
            max_wait=max_wait,
            poll_interval=poll_interval,
        )
        readbacks = outcome.readbacks
    except Exception as exc:
        printer(f"verify: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(adapter)

    verifications = grade(sent, readbacks, route, body_exact)
    artifact = build_artifact(
        seed,
        run_config(
            seed, count, tag_prefix, target_name, readback, route, body_exact,
            {"quiet_window": quiet_window, "max_wait": max_wait,
             "poll_interval": poll_interval},
        ),
        sent,
        verifications,
        readbacks,
        outcome,
    )
    printer(format_verification(artifact))
    if out:
        Path(out).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")
    return EXIT_OK if all(v.passed for v in verifications) else EXIT_MISMATCH


def check_saved_run(
    run_path: str,
    config_path: str,
    out: Optional[str],
    *,
    readback: ReadbackConfig,
    route: Optional[str] = None,
    body_exact: bool = False,
    tag_prefix: Optional[str] = None,
    readback_client: Any = None,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Verify a blast run that already happened.

    The corpus is rebuilt from the artifact's own seed and count, so no request
    is sent: this path reads only. That is the whole reason it can be a separate
    subcommand from `verify fire` and why it has no `--send` and no dry-run
    mode, in the same way `testinghq compare` has neither.

    Only the records the artifact labels `clean` are verified. Everything else
    is a payload that was deliberately mangled, and grading it against its own
    ground truth would report a failure for the mutator working. The skipped
    count is printed, because a run that verified 3 of 100 and stayed quiet
    about the other 97 is the exact shape of a misleading green.
    """
    try:
        data = json.loads(Path(run_path).read_text(encoding="utf-8"))
    except OSError as exc:
        printer(f"verify check: could not read {run_path!r}: {exc}")
        return EXIT_REFUSED
    except json.JSONDecodeError as exc:
        printer(f"verify check: {run_path!r} is not valid JSON: {exc}")
        return EXIT_REFUSED

    seed = data.get("seed")
    config = data.get("config") or {}
    count = config.get("count")
    if seed is None or count is None:
        printer(
            f"verify check: {run_path!r} is missing seed or config.count, so the "
            "payloads it describes cannot be rebuilt"
        )
        return EXIT_REFUSED

    records = data.get("records") or []
    clean = [r for r in records if r.get("category") == report.CLEAN]
    skipped_categories = len(records) - len(clean)
    if not clean:
        printer(
            f"verify check: {run_path!r} has no clean records, so there is "
            "nothing whose correct parse can be checked. A verification run "
            "over a deliberately garbled corpus has no ground truth to grade "
            "against; that is what `verify fire` is for."
        )
        return EXIT_REFUSED

    prefix = tag_prefix or config.get("tag_prefix") or messages.DEFAULT_TAG_PREFIX
    corpus = build_tagged_corpus(seed, count, prefix)
    if len(corpus) != count:
        printer(
            f"verify check: rebuilt {len(corpus)} payload(s) for seed {seed} but "
            f"the artifact describes {count}. The generator's shape changed "
            "since that run, so the payload hashes will not match and the tags "
            "will not line up. Re-run the tool that produced it instead."
        )
        return EXIT_REFUSED

    by_tag = {tag: (email, rid) for email, tag, rid in corpus}
    wanted = []
    for record in clean:
        tag = record.get("tag")
        if tag is None:
            printer(
                f"verify check: record {record.get('id')!r} carries no tag, so "
                "there is no way to ask the system about that payload. This "
                "artifact was written by a tool that does not stamp tags; run "
                "`verify fire` instead of `verify check`."
            )
            return EXIT_REFUSED
        if tag not in by_tag:
            printer(
                f"verify check: record {record.get('id')!r} has tag {tag!r}, "
                f"which this build's generator does not produce for seed {seed} "
                f"and prefix {prefix!r}. Rebuilding the corpus would look the "
                "payloads up against tags that never existed."
            )
            return EXIT_REFUSED
        email, _rid = by_tag[tag]
        wanted.append((email, tag, record.get("id", "")))

    adapter = build_adapter(readback, client=readback_client)
    try:
        outcome = read_back_all(
            adapter,
            [messages.probe_for(e, t, rid, record.get("payload_sha256", ""))
             for e, t, rid in wanted],
            sleep=sleep,
            clock=clock,
            quiet_window=quiet_window,
            max_wait=max_wait,
            poll_interval=poll_interval,
        )
        readbacks = outcome.readbacks
    except Exception as exc:
        printer(f"verify check: could not read the system back: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(adapter)

    # `sent=False` is the honest description of this path: it made no request.
    # `summarize` keys its transport counters off `sent`, so the read-only path
    # reports zero unanswered requests rather than N, which would read as a
    # total transport failure on a run that never touched the network.
    never_sent = TransportResult(
        status=None, latency_ms=0.0, body_snippet="", error=None, sent=False
    )
    fake_sent = [
        SentMessage(
            index=index,
            record_id=record_id,
            tag=tag,
            email=email,
            probe=messages.probe_for(email, tag, record_id, ""),
            result=never_sent,
        )
        for index, (email, tag, record_id) in enumerate(wanted)
    ]
    verifications = grade(fake_sent, readbacks, route, body_exact)
    artifact = build_artifact(
        seed,
        {
            "tool": "verify",
            "mode": "check",
            "seed": seed,
            "count": count,
            "tag_prefix": prefix,
            "target": config.get("target"),
            "readback": readback.to_json(),
            "expect_route": route,
            "body_exact": body_exact,
            "source_run": run_path,
            "records_skipped_non_clean": skipped_categories,
            "dry_run": False,
        },
        fake_sent,
        verifications,
        readbacks,
        outcome,
    )
    printer(f"verify check: {skipped_categories} non-clean record(s) not verified, "
            "because a mangled payload has no correct parse to grade against")
    printer(format_verification(artifact))
    if out:
        Path(out).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")
    return EXIT_OK if all(v.passed for v in verifications) else EXIT_MISMATCH
