"""redeliver: what a pipeline does when the provider misbehaves.

Webhook providers are not a clean request/response channel. They retry a
delivery that times out, deliver the same event twice, and deliver out of order
when several were queued at once. A pipeline built against the happy path
handles none of that, and the two failure modes it produces are among the most
common real bugs in intake systems: a duplicate ticket, because the retry
created a second one instead of recognising the first, and a broken thread,
because a reply that arrived before its original was filed as a new
conversation. Both are invisible to a status-code tool. Both were announced with
a 200.

Four scenarios, each a real thing a provider does, each built from the same
seeded generator so a run is reproducible:

  duplicate    the identical message, same Message-ID, twice back to back.
  slow-retry   every message once, one wait of `--retry-after`, then every
               message again.
  reply-first  a reply delivered before the message it answers, with correct
               threading headers, so only the order is wrong.
  references   a three-message thread in order with a growing References chain.
               The last reply's stored References must carry the whole
               ancestry, not just its parent.

`thread_link` and `thread_together` are separate checks because correct headers
on two separate tickets is the outcome that makes an agent open a duplicate
ticket a fortnight later, and the two have different fixes: a header
propagation bug and a deduplication bug.

The four scenarios share payload bytes on purpose, so a parse bug shows up four
ways rather than once plus three unrelated findings. Identity is scoped per
scenario instead, by a tag prefix and a Message-ID prefix, because shared
payloads carry shared Message-IDs and a correct pipeline deduplicates on them:
without the prefixes the slow-retry scenario re-delivered messages the duplicate
scenario had already sent, the pipeline correctly recognised them as
redeliveries, and the scenario reported total loss. Every component was correct;
the test design was not.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..blast.payload import InboundEmail
from ..core.ratelimit import TokenBucket
from ..core.targets import resolve_target
from ..core.transport import post
from . import messages
from .adapters import ReadbackConfig
from .common import (
    ALL_404_NOT_CHECKED,
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    READBACK_FAILED,
    SentMessage,
    close_adapter,
    DEFAULT_MAX_WAIT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_QUIET_WINDOW,
    ReadbackOutcome,
    prepare_readback,
    read_back_all,
    readback_failed,
    readback_verdict,
    require_synthetic,
)
from .expectations import (
    DEFAULT_ATTACHMENT_MATCH,
    DEFAULT_SUBJECT_MATCH,
    Expectations,
    check_thread_link,
    check_thread_together,
    evaluate_sequence,
    match_options,
    validate_match_modes,
)
from .ledger import MAX_ARTIFACT_STRAYS, classify_strays, enumerate_store, minted_by
from .readback import Readback
from .verify import (
    DEFAULT_RATE,
    abort_run,
    artifact_header,
    build_clean_corpus,
    readback_note,
    readback_unsettled,
    write_artifact,
)

#: Seconds before the slow-retry redelivery.
#:
#: Two minutes, and configurable, because deduplication windows vary far too
#: widely for any one number to be right for everyone. A pipeline that
#: deduplicates in-process, or in a cache with a short TTL, survives an
#: immediate duplicate and fails the same duplicate minutes later, so a
#: one-second gap only ever proves the easy half. A pipeline that keeps a
#: dedup record for a day needs `--retry-after` in the thousands, and the
#: tradeoff is simply that a longer window means a longer run.
#:
#: No claim is made here about how that window compares to any particular
#: deployment's. There is no default that is past everybody's cache and short
#: enough to be usable, so this is a starting point to adjust, not a correct
#: answer.
#:
#: WHAT THIS SCENARIO CANNOT DO, and it is worth being blunt about because the
#: name suggests otherwise. A provider retries a delivery because IT believes
#: the delivery failed: a timeout, a reset connection, a 5xx it did not like.
#: This tool's second delivery is a deliberate repeat of a first delivery that
#: SUCCEEDED. So it tests the pipeline's response to a duplicate, which is the
#: common bug, and it does not test anything about how the pipeline behaves when
#: the failure that caused the retry is also present. A pipeline that
#: deduplicates only on a failure signal, and not on Message-ID, will pass this
#: and still duplicate a real retry.
DEFAULT_RETRY_AFTER = 120.0

DEFAULT_MESSAGES = 3
MAX_LISTED = 20

SCENARIO_DUPLICATE = "duplicate"
SCENARIO_SLOW_RETRY = "slow-retry"
SCENARIO_REPLY_FIRST = "reply-first"
SCENARIO_REFERENCES = "references"

SCENARIOS: Tuple[str, ...] = (
    SCENARIO_DUPLICATE,
    SCENARIO_SLOW_RETRY,
    SCENARIO_REPLY_FIRST,
    SCENARIO_REFERENCES,
)

#: The tag suffix and the Message-ID prefix a scenario stamps, one per scenario.
#: Both exist for the same reason and it is worth stating plainly, because the
#: alternative was tried first and failed loudly: the four scenarios share the
#: same generated payloads, and a generated payload carries a Message-ID, and a
#: correct pipeline deduplicates on Message-ID. So the slow-retry scenario
#: delivered messages the duplicate scenario had already delivered, the
#: pipeline correctly recognised them as redeliveries, and the slow-retry
#: scenario then reported that the system held no record of any of its own
#: messages. Nothing was wrong with the pipeline and nothing was wrong with the
#: tool. The scenarios were contaminating each other through the one field
#: they have to share to be the same message within a scenario.
#:
#: A per-scenario Message-ID prefix fixes it without giving up the shared
#: payload: the bytes are the same across scenarios, so a parse bug shows up the
#: same way in all four, and the identity is scoped, so one scenario's
#: deduplication cannot absorb another's deliveries.
SCENARIO_IDENTITY: Dict[str, str] = {
    SCENARIO_DUPLICATE: "dup",
    SCENARIO_SLOW_RETRY: "rty",
    SCENARIO_REPLY_FIRST: "r1st",
    SCENARIO_REFERENCES: "ref",
}

#: A tag suffix per scenario, so the four share payload bytes without sharing
#: identity. See the note above on `SCENARIO_IDENTITY`.
SCENARIO_TAG_SUFFIX: Dict[str, str] = SCENARIO_IDENTITY

#: Matches a subject that already carries a reply or forward prefix, in any of
#: the forms a real client uses. Used so a reply to a reply does not become
#: `Re: Re: `, which would be a subject no client has ever sent and which a
#: parser is right to normalise.
_ALREADY_REPLYING = re.compile(r"^\s*(re|aw|sv|fwd?|fw)\s*(\[\d+\])?\s*:\s*", re.IGNORECASE)


def _bare(message_id: Optional[str]) -> str:
    """A Message-ID without its angle brackets, the form ids are compared in,
    so a stored value with or without brackets matches."""
    return (message_id or "").strip().strip("<>").strip()


def _header_ids(*message_ids: str) -> str:
    """Message-IDs as RFC 5322 writes them in a header: each in angle
    brackets, several separated by a space (a References chain)."""
    return " ".join(f"<{_bare(mid)}>" for mid in message_ids)


def reply_subject(subject: str) -> str:
    """The subject a real client would put on a reply: `Re: ` plus the parent's,
    and not `Re: Re: ` when the parent already had one."""
    if _ALREADY_REPLYING.match(subject or ""):
        return subject
    return f"Re: {subject}"


class ScenarioError(ValueError):
    """Raised for a malformed scenario request. A caller error, never reported
    as a pipeline failure."""


@dataclass(frozen=True)
class Delivery:
    """One POST the tool will make."""

    email: InboundEmail
    tag: str
    record_id: str
    message_id: str
    #: Seconds to wait BEFORE this delivery. The wait is here rather than after
    #: the previous one so the first delivery of a scenario has an explicit
    #: zero and a reader can see the whole schedule in the plan rather than
    #: inferring it.
    delay_before: float = 0.0
    #: The tag of the message this one replies to, or None for a root.
    in_reply_to_tag: Optional[str] = None

    @property
    def is_reply(self) -> bool:
        return self.in_reply_to_tag is not None


@dataclass(frozen=True)
class Scenario:
    """A named sequence of deliveries that should produce one set of records,
    not several."""

    name: str
    deliveries: Tuple[Delivery, ...]
    question: str

    @property
    def tags(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for delivery in self.deliveries:
            if delivery.tag not in seen:
                seen.append(delivery.tag)
        return tuple(seen)

    def delivery_for(self, tag: str) -> Optional[Delivery]:
        """The first delivery carrying `tag`, whatever its role. This is the
        one a content check grades against: a reply's expected subject is the
        reply's own, not its parent's."""
        for delivery in self.deliveries:
            if delivery.tag == tag:
                return delivery
        return None

    def parent_of(self, tag: str) -> Optional[Delivery]:
        """The delivery `tag` replies to, or None if `tag` is a root or names a
        parent this scenario never delivers."""
        delivery = self.delivery_for(tag)
        if delivery is None or not delivery.is_reply:
            return None
        return self.delivery_for(delivery.in_reply_to_tag or "")

    def ancestry_of(self, tag: str) -> Tuple[str, ...]:
        """Message-IDs of every ancestor of `tag`, root first, bare (angle
        brackets stripped) so they compare with a stored value either way."""
        chain: List[str] = []
        seen = {tag}
        parent = self.parent_of(tag)
        while parent is not None and parent.tag not in seen:
            chain.append(_bare(parent.message_id))
            seen.add(parent.tag)
            parent = self.parent_of(parent.tag)
        return tuple(reversed(chain))


# ---------------------------------------------------------------------------
# Building the scenarios
# ---------------------------------------------------------------------------


def _make(
    email: InboundEmail,
    tag: str,
    index: int,
    seed: int,
    *,
    message_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    subject: Optional[str] = None,
    delay_before: float = 0.0,
    in_reply_to_tag: Optional[str] = None,
) -> Delivery:
    """Stamp a payload into a Delivery.

    Every field is pinned explicitly, including the threading headers, so a
    scenario's plan can be read in one place rather than inferred from a
    stamping order that happens to put the parent first. The reply-first
    scenario depends on that: its replies are stamped before its root exists in
    the delivery order, and building the parent's Message-ID by hand is what
    makes that possible.

    The ids are given bare and written to the headers in angle brackets, as
    RFC 5322 requires; `references` is a space-separated chain. The Delivery
    keeps the bare id.
    """
    stamped = messages.stamp(
        email,
        tag,
        message_id=None if message_id is None else _header_ids(message_id),
        in_reply_to=None if in_reply_to is None else _header_ids(in_reply_to),
        references=None if references is None else _header_ids(*references.split()),
        subject=subject,
    )
    return Delivery(
        email=stamped,
        tag=tag,
        record_id=f"clean-{seed}-{index:04d}",
        message_id=messages.message_id_of(stamped),
        delay_before=delay_before,
        in_reply_to_tag=in_reply_to_tag,
    )


def _repeat_twice(
    corpus: Sequence[InboundEmail],
    seed: int,
    tag_prefix: str,
    id_prefix: str,
    gap: float,
) -> Tuple[Delivery, ...]:
    """The identical message delivered twice.

    With no gap, each message's two copies go back to back. With a gap, every
    original goes first, then one wait of `gap`, then every retry: one wait for
    the batch rather than one per message, so a 120s gap costs 120s.

    One tag and one Message-ID across both deliveries, which is what makes the
    two the same message. A second delivery with a different tag or a different
    Message-ID would be a different message, and a pipeline that correctly
    created two tickets for it would be reported as having duplicated one.

    The Message-ID is pinned from `id_prefix` rather than left as the
    generator's, for the cross-scenario reason documented on
    `SCENARIO_IDENTITY`: the four scenarios share payload bytes, and a shared
    Message-ID would make the second scenario's deliveries look like
    redeliveries of the first's.
    """
    originals: List[Delivery] = []
    retries: List[Delivery] = []
    for index, email in enumerate(corpus):
        tag = messages.make_tag(tag_prefix, seed, index)
        message_id = messages.build_chain_message_id(f"{id_prefix}-{seed}-{index:04d}")
        record_id = f"clean-{seed}-{index:04d}"
        originals.append(
            Delivery(
                email=messages.stamp(email, tag, message_id=_header_ids(message_id)),
                tag=tag,
                record_id=record_id,
                message_id=message_id,
            )
        )
        retries.append(
            Delivery(
                # Stamped from the original again rather than from the already
                # stamped payload, so the two deliveries are byte-identical
                # rather than carrying the marker twice.
                email=messages.stamp(email, tag, message_id=_header_ids(message_id)),
                tag=tag,
                record_id=record_id,
                message_id=message_id,
            )
        )
    if gap <= 0:
        return tuple(d for pair in zip(originals, retries) for d in pair)
    if retries:
        first = retries[0]
        retries[0] = Delivery(
            email=first.email, tag=first.tag, record_id=first.record_id,
            message_id=first.message_id, delay_before=gap,
        )
    return tuple(originals + retries)


def build_duplicate(
    corpus: Sequence[InboundEmail], seed: int, tag_prefix: str, id_prefix: str = "dup"
) -> Scenario:
    return Scenario(
        name=SCENARIO_DUPLICATE,
        deliveries=_repeat_twice(corpus, seed, tag_prefix, id_prefix, 0.0),
        question=(
            "the identical message, same Message-ID, delivered twice back to "
            "back: a correct pipeline creates one ticket"
        ),
    )


def build_slow_retry(
    corpus: Sequence[InboundEmail],
    seed: int,
    tag_prefix: str,
    retry_after: float,
    id_prefix: str = "rty",
) -> Scenario:
    if retry_after < 0:
        raise ScenarioError(f"--retry-after must be >= 0, got {retry_after}")
    return Scenario(
        name=SCENARIO_SLOW_RETRY,
        deliveries=_repeat_twice(corpus, seed, tag_prefix, id_prefix, retry_after),
        question=(
            f"every message re-sent after one {retry_after:g}s wait following "
            f"a first delivery that succeeded: one ticket each. This is the "
            f"duplicate half of a provider retry, not a failure-driven one"
        ),
    )


def build_reply_first(
    corpus: Sequence[InboundEmail],
    seed: int,
    tag_prefix: str,
    id_prefix: str = "r1st",
) -> Scenario:
    """A reply delivered before the message it replies to.

    Three payloads: one root and two replies, delivered reply, reply, root. The
    replies carry correct `In-Reply-To` and `References` headers and a `Re:`
    subject, so the only thing wrong with the delivery is its order. A pipeline
    that keys dedup on arrival order rather than on Message-ID files all three
    as separate conversations, and a pipeline that drops a reply whose parent it
    has not seen loses it outright.
    """
    if len(corpus) < 3:
        raise ScenarioError(
            "the reply-first scenario needs 3 payloads (a root and two replies)"
        )
    root_email, reply_one_email, reply_two_email = corpus[0], corpus[1], corpus[2]
    root_tag = messages.make_tag(tag_prefix, seed, 0)
    reply_one_tag = messages.make_tag(tag_prefix, seed, 1)
    reply_two_tag = messages.make_tag(tag_prefix, seed, 2)
    root_id = messages.build_chain_message_id(f"{id_prefix}-root-{seed}")
    subject = reply_subject(root_email.ground_truth.subject)

    return Scenario(
        name=SCENARIO_REPLY_FIRST,
        deliveries=(
            _make(
                reply_one_email,
                reply_one_tag,
                1,
                seed,
                message_id=messages.build_chain_message_id(f"{id_prefix}-r1-{seed}"),
                in_reply_to=root_id,
                references=root_id,
                in_reply_to_tag=root_tag,
                subject=subject,
            ),
            _make(
                reply_two_email,
                reply_two_tag,
                2,
                seed,
                message_id=messages.build_chain_message_id(f"{id_prefix}-r2-{seed}"),
                in_reply_to=root_id,
                references=root_id,
                in_reply_to_tag=root_tag,
                subject=subject,
            ),
            _make(root_email, root_tag, 0, seed, message_id=root_id),
        ),
        question=(
            "both replies delivered before the message they answer, with correct "
            "threading headers: one conversation, not three"
        ),
    )


def build_references(
    corpus: Sequence[InboundEmail],
    seed: int,
    tag_prefix: str,
    id_prefix: str = "ref",
) -> Scenario:
    """A three-message thread in order, with a growing References chain.

    The chain is the difference from reply-first: a reply answers its immediate
    parent, and its References lists the whole ancestry. A pipeline that only
    reads In-Reply-To threads this correctly by accident; one that only reads
    the first Reference does not.
    """
    if len(corpus) < 3:
        raise ScenarioError(
            "the references scenario needs 3 payloads (a root and two replies)"
        )
    root_email, reply_one_email, reply_two_email = corpus[0], corpus[1], corpus[2]
    root_tag = messages.make_tag(tag_prefix, seed, 0)
    reply_one_tag = messages.make_tag(tag_prefix, seed, 1)
    reply_two_tag = messages.make_tag(tag_prefix, seed, 2)
    root_id = messages.build_chain_message_id(f"{id_prefix}-root-{seed}")
    reply_one_id = messages.build_chain_message_id(f"{id_prefix}-r1-{seed}")
    subject = reply_subject(root_email.ground_truth.subject)

    return Scenario(
        name=SCENARIO_REFERENCES,
        deliveries=(
            _make(root_email, root_tag, 0, seed, message_id=root_id),
            _make(
                reply_one_email,
                reply_one_tag,
                1,
                seed,
                message_id=reply_one_id,
                in_reply_to=root_id,
                references=root_id,
                in_reply_to_tag=root_tag,
                subject=subject,
            ),
            _make(
                reply_two_email,
                reply_two_tag,
                2,
                seed,
                message_id=messages.build_chain_message_id(f"{id_prefix}-r2-{seed}"),
                in_reply_to=reply_one_id,
                references=f"{root_id} {reply_one_id}",
                in_reply_to_tag=reply_one_tag,
                subject=subject,
            ),
        ),
        question=(
            "a three-message thread in order with a growing References chain: one "
            "conversation, and the last reply's References carries the full "
            "ancestry"
        ),
    )


_BUILDERS = {
    SCENARIO_DUPLICATE: build_duplicate,
    SCENARIO_SLOW_RETRY: build_slow_retry,
    SCENARIO_REPLY_FIRST: build_reply_first,
    SCENARIO_REFERENCES: build_references,
}


def build_scenarios(
    seed: int,
    count: int,
    tag_prefix: str,
    retry_after: float,
    selected: Optional[Sequence[str]] = None,
) -> List[Scenario]:
    """Every scenario, or just the named ones.

    The corpus is generated once, from the seed, and the same payloads feed
    every scenario, so the only thing that varies between them is the delivery
    semantics. Each scenario's tag prefix and Message-ID prefix are scoped to
    it, for the reason documented on `SCENARIO_IDENTITY`: sharing the bytes is
    deliberate, sharing the identity would let one scenario's deduplication
    swallow another's deliveries.

    The threaded scenarios need exactly 3 payloads and take what they need; the
    duplicate scenarios take all `count` of them, because "did a redelivery
    duplicate a ticket" is worth asking about every message you sent.
    """
    wanted = list(selected) if selected else list(SCENARIOS)
    unknown = [name for name in wanted if name not in _BUILDERS]
    if unknown:
        raise ScenarioError(
            f"unknown scenario(s) {unknown}; choose from {list(SCENARIOS)}"
        )
    if count <= 0:
        raise ScenarioError(f"count must be > 0, got {count}")

    corpus = build_clean_corpus(seed, max(count, 3))

    scenarios: List[Scenario] = []
    for name in wanted:
        identity = SCENARIO_IDENTITY[name]
        prefix = f"{tag_prefix}-{SCENARIO_TAG_SUFFIX[name]}"
        if name == SCENARIO_SLOW_RETRY:
            built = build_slow_retry(
                corpus[:count], seed, prefix, retry_after, identity
            )
        elif name == SCENARIO_DUPLICATE:
            # `corpus[:count]`, not the whole corpus. The first version passed
            # the full three-payload slice to the duplicate scenario and a
            # two-payload slice to the slow-retry one, so `--count 2` reported
            # "3 messages" for one scenario and "2 messages" for the other, and
            # the two could not be compared. Both duplicate scenarios now cover
            # exactly the messages the operator asked for.
            built = build_duplicate(corpus[:count], seed, prefix, identity)
        else:
            built = _BUILDERS[name](corpus, seed, prefix, identity)
        scenarios.append(built)
    return scenarios


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def send_scenario(
    scenario: Scenario,
    url: str,
    rate: float,
    client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    wire_format: Any = None,
) -> List[SentMessage]:
    """Deliver one scenario in order, honouring each delivery's `delay_before`.

    The `client` and `sleep` seams are the ones every other path in this package
    has, and for the same reason: the gap before a retry is a real `time.sleep`
    in production and a recorded number in a test. Without an injectable sleep,
    a test of the slow-retry scenario would either take five seconds or would be
    testing a scenario with no gap in it, which is the duplicate scenario
    wearing a different name.
    """
    bucket = TokenBucket(
        rate_per_sec=rate, capacity=max(rate, 1.0), sleep=sleep, clock=clock
    )
    sent: List[SentMessage] = []
    for index, delivery in enumerate(scenario.deliveries):
        if delivery.delay_before > 0:
            sleep(delivery.delay_before)
        bucket.acquire()
        if wire_format is not None:
            result = post(delivery.email, url, client=client, wire_format=wire_format)
        else:
            result = post(delivery.email, url, client=client)
        sent.append(
            SentMessage(
                index=index,
                record_id=delivery.record_id,
                tag=delivery.tag,
                email=delivery.email,
                probe=messages.probe_for(
                    delivery.email, delivery.tag, delivery.record_id, ""
                ),
                result=result,
            )
        )
    return sent


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    """What one scenario found. `findings` is a list of failed checks in a
    stable order, so two runs of the same seed produce the same report and a
    diff between them means something."""

    name: str
    question: str
    deliveries: int
    unique_messages: int
    strays_searched: bool = True
    findings: List[str] = field(default_factory=list)
    records: List[Dict[str, Any]] = field(default_factory=list)
    #: Tags delivered and not found. Kept apart from `findings` so an unsettled
    #: readback can be told from a loss.
    missing: List[str] = field(default_factory=list)
    #: Tags delivered and never looked up successfully, because every lookup
    #: of the run was a 404. Neither a finding nor a pass.
    not_checked: List[str] = field(default_factory=list)
    #: Records carrying this scenario's tag prefix that it never delivered.
    strays: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings and not self.not_checked

    def to_json(self) -> Dict[str, Any]:
        return {
            "scenario": self.name,
            "question": self.question,
            "deliveries": self.deliveries,
            "unique_messages": self.unique_messages,
            "strays_searched": self.strays_searched,
            "passed": self.passed,
            "findings": list(self.findings),
            "missing": list(self.missing),
            "not_checked": list(self.not_checked),
            "strays": list(self.strays),
            "messages": self.records,
        }


def judge_scenario(
    scenario: Scenario,
    readbacks: Dict[str, List[Readback]],
    *,
    route: Optional[str] = None,
    body_exact: bool = False,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    readback_failed: bool = False,
) -> ScenarioResult:
    """Grade one scenario's outcome.

    Four independent checks. Which of them apply depends on the scenario, because
    a duplicate scenario has no thread to check:

      content      each message's own sender, subject and body, graded against
                   its own ground truth. A scenario that created the right
                   number of records with the wrong content has not passed, and
                   saying so is the difference between this tool and a counter.
      duplicates   every unique message has exactly one record. This is the
                   headline check, and the one both duplicate scenarios exist to
                   trip.
      thread_link  each reply's In-Reply-To names its parent and its
                   References carries every ancestor.
      thread_together  each reply ended up on the same ticket as its parent.

    Content is graded with the run's match modes and `--expect-route`.
    `readback_failed` (every lookup of the run was a 404) makes a message with
    no record `not_checked` rather than a loss.
    """
    result = ScenarioResult(
        name=scenario.name,
        question=scenario.question,
        deliveries=len(scenario.deliveries),
        unique_messages=len(scenario.tags),
    )

    first_by_tag: Dict[str, Readback] = {}
    for tag in scenario.tags:
        found = readbacks.get(tag, [])
        delivery = scenario.delivery_for(tag)
        if delivery is None:  # pragma: no cover - a tag always has a delivery
            raise ScenarioError(
                f"scenario {scenario.name!r} has no delivery for tag {tag!r}"
            )

        record: Dict[str, Any] = {
            "tag": tag,
            "record_id": delivery.record_id,
            "message_id": delivery.message_id,
            "replies_to": delivery.in_reply_to_tag,
            "tickets": [str(r.ticket_id) for r in found],
            "checks": [],
        }

        if not found and readback_failed:
            result.not_checked.append(tag)
        elif not found:
            result.missing.append(tag)
            result.findings.append(
                f"{tag}: delivered and the system holds no record of it"
            )
        else:
            if len(found) > 1:
                result.findings.append(
                    f"{tag}: {len(found)} tickets for one message "
                    f"({[str(r.ticket_id) for r in found]}); a redelivery created "
                    "a duplicate"
                )
            first_by_tag[tag] = found[0]
            expectations = Expectations.from_email(
                delivery.email,
                route=route,
                body_exact=body_exact,
                subject_match=subject_match,
                attachment_match=attachment_match,
            )
            verification = evaluate_sequence(
                delivery.email,
                found,
                expectations,
                record_id=delivery.record_id,
                tag=tag,
            )
            for mismatch in verification.mismatch_strings():
                result.findings.append(mismatch)
            record["checks"].extend(c.to_json() for c in verification.checks)

        result.records.append(record)

    for tag in scenario.tags:
        delivery = scenario.delivery_for(tag)
        if delivery is None or not delivery.is_reply:
            continue
        parent = scenario.parent_of(tag)
        if parent is None:
            raise ScenarioError(
                f"scenario {scenario.name!r}: delivery {tag!r} claims to reply to "
                f"{delivery.in_reply_to_tag!r}, which this scenario never delivers. "
                "That is a build bug in the scenario, not a pipeline failure, and "
                "it must not be reported as one."
            )
        link = check_thread_link(
            first_by_tag.get(tag), _bare(parent.message_id), scenario.ancestry_of(tag)
        )
        together = check_thread_together(
            first_by_tag.get(tag), first_by_tag.get(parent.tag)
        )
        for check in (link, together):
            if check.passed is False:
                result.findings.append(f"{tag}: {check.detail}")
        for record in result.records:
            if record["tag"] == tag:
                record["checks"].extend([link.to_json(), together.to_json()])
                break

    return result


def scenario_tag_prefix(tag_prefix: str, scenario_name: str) -> str:
    """The tag prefix a scenario stamps under the run's prefix."""
    return f"{tag_prefix}-{SCENARIO_TAG_SUFFIX[scenario_name]}"


def judge_all(
    scenarios: Sequence[Scenario],
    readbacks: Dict[str, List[Readback]],
    strays: Optional[Sequence[Readback]] = None,
    **options: Any,
) -> List[ScenarioResult]:
    """`judge_run` without the run-level stray summary."""
    return judge_run(scenarios, readbacks, strays, **options)[0]


def judge_run(
    scenarios: Sequence[Scenario],
    readbacks: Dict[str, List[Readback]],
    strays: Optional[Sequence[Readback]] = None,
    *,
    tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
    route: Optional[str] = None,
    body_exact: bool = False,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    strays_partial: bool = False,
    readback_failed: bool = False,
) -> Tuple[List[ScenarioResult], Dict[str, Any]]:
    """Grade every scenario, then hunt for records nobody sent.

    Returns (results, stray summary). A stray carrying a scenario's tag prefix
    is a finding on that scenario. A stray with this run's prefix and no
    scenario's is a run-level finding. Other runs' tags are listed for
    information and untagged records are ignored. When the adapter cannot
    enumerate, or the listing was truncated (`strays_partial`), every scenario
    records `strays_searched: False`; strays a truncated listing did find are
    still reported.
    """
    results = [
        judge_scenario(
            scenario, readbacks, route=route, body_exact=body_exact,
            subject_match=subject_match, attachment_match=attachment_match,
            readback_failed=readback_failed,
        )
        for scenario in scenarios
    ]
    summary: Dict[str, Any] = {
        "strays_searched": strays is not None and not strays_partial,
        "unattributed": None,
        "other_runs": None,
        "other_runs_count": None,
        "untagged_count": None,
    }
    if strays is None or strays_partial:
        for result in results:
            result.strays_searched = False
    if strays is None:
        return results, summary

    sent_tags = [tag for scenario in scenarios for tag in scenario.tags]
    own_tickets = [str(r.ticket_id) for found in readbacks.values() for r in found]
    # The run's family: its own prefix and every scenario prefix under it.
    by_prefix = {
        scenario_tag_prefix(tag_prefix, name): name for name in SCENARIOS
    }
    family = [tag_prefix, *by_prefix]
    sorted_strays = classify_strays(strays, sent_tags, own_tickets, family)
    results_by_name = {result.name: result for result in results}
    unattributed: List[Dict[str, Any]] = []
    for entry in sorted_strays["extra"]:
        owner = results_by_name.get(by_prefix.get(minted_by(entry["tag"], family) or ""))
        if owner is None:
            unattributed.append(entry)
            continue
        if len(owner.strays) < MAX_ARTIFACT_STRAYS:
            owner.strays.append(entry)
            owner.findings.append(
                f"ticket {entry['ticket']} carries tag {entry['tag']!r}, which "
                "this scenario never delivered"
            )
    summary.update(
        {
            "unattributed": unattributed[:MAX_ARTIFACT_STRAYS],
            "unattributed_count": len(unattributed),
            "other_runs": sorted_strays["other_runs"][:MAX_ARTIFACT_STRAYS],
            "other_runs_count": len(sorted_strays["other_runs"]),
            "untagged_count": sorted_strays["untagged"],
        }
    )
    return results, summary


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def run_config(
    seed: int,
    count: int,
    tag_prefix: str,
    target_name: Optional[str],
    readback: ReadbackConfig,
    retry_after: float,
    selected: Optional[Sequence[str]],
    *,
    route: Optional[str] = None,
    body_exact: bool = False,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    wire_format: Optional[str] = None,
    allow_public_target: bool = False,
) -> Dict[str, Any]:
    config: Dict[str, Any] = {
        "tool": "redeliver",
        "seed": seed,
        "count": count,
        "tag_prefix": tag_prefix,
        "target": target_name,
        "readback": readback.to_json(),
        "retry_after": retry_after,
        "scenarios": list(selected) if selected else list(SCENARIOS),
    }
    config.update(match_options(route, body_exact, subject_match, attachment_match))
    config.update(
        {
            "format": wire_format,
            "allow_public_target": allow_public_target,
            "dry_run": False,
        }
    )
    return config


def verdict_of(
    results: Sequence[ScenarioResult],
    strays: Optional[Dict[str, Any]] = None,
    unsettled: bool = False,
) -> str:
    """DELIVERY-SAFE, DELIVERY-BUGS, UNSETTLED when the only problems are
    messages not yet found by a readback that never settled, or READBACK
    FAILED when messages were never looked up because every lookup was a 404."""
    unattributed = bool((strays or {}).get("unattributed"))
    real = unattributed or any(
        len(r.findings) > len(r.missing) for r in results
    )
    if real:
        return "DELIVERY-BUGS"
    if unsettled:
        return "UNSETTLED"
    if any(r.findings for r in results):
        return "DELIVERY-BUGS"
    if any(r.not_checked for r in results):
        return READBACK_FAILED
    return "DELIVERY-SAFE"


def build_artifact(
    seed: int,
    config: Dict[str, Any],
    results: Sequence[ScenarioResult],
    readback_outcome: Optional["ReadbackOutcome"] = None,
    strays: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    failing = [r for r in results if r.findings]
    strays = strays or {}
    unattributed = strays.get("unattributed") or []
    unsettled = readback_unsettled(readback_outcome)
    summary: Dict[str, Any] = {
        "scenarios": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": len(failing) + (1 if unattributed else 0),
        "not_checked": sum(1 for r in results if r.not_checked),
        "deliveries": sum(r.deliveries for r in results),
        "findings": sum(len(r.findings) for r in results) + len(unattributed),
        "strays_searched": all(r.strays_searched for r in results),
        "unattributed_strays": unattributed,
        "unattributed_strays_count": strays.get("unattributed_count", len(unattributed)),
        "strays_not_searched_reason": strays.get("strays_not_searched_reason"),
        "other_runs": strays.get("other_runs"),
        "other_runs_count": strays.get("other_runs_count"),
        "untagged_count": strays.get("untagged_count"),
        "unsettled": unsettled,
    }
    summary["verdict"], summary["verdict_source"] = readback_verdict(
        verdict_of(results, strays, unsettled), readback_outcome
    )
    if readback_outcome is not None:
        summary["readback"] = readback_outcome.to_json()
    artifact = artifact_header("redeliver")
    artifact.update(
        {
            "seed": seed,
            "config": config,
            "summary": summary,
            "scenarios": [r.to_json() for r in results],
        }
    )
    return artifact


def format_redelivery(artifact: Dict[str, Any]) -> str:
    """Render the report.

    Each scenario's question comes before its verdict, because the question is
    what makes a finding actionable: "a provider retried this and you made two
    tickets" is a report, and "2 findings" is not.
    """
    summary = artifact.get("summary") or {}
    lines = [
        f"redeliver: {summary.get('verdict', 'UNKNOWN')}  "
        f"({summary.get('passed', 0)}/{summary.get('scenarios', 0)} scenarios "
        f"behaved, {summary.get('deliveries', 0)} delivery(ies) sent, "
        f"seed={artifact.get('seed')})"
    ]

    readback = summary.get("readback")
    if readback:
        settled = (
            "settled" if readback.get("stable")
            else "UNSETTLED, still changing at --max-wait"
        )
        lines.append(
            f"  readback: {readback.get('elapsed_s')}s over "
            f"{readback.get('polls')} poll(s), {settled}"
        )
    note = readback_note(summary)
    if note:
        lines.append(f"  {note}")
    unattributed = summary.get("unattributed_strays") or []
    if unattributed:
        lines.append(
            f"  {summary.get('unattributed_strays_count', len(unattributed))} "
            "record(s) carry this run's tag prefix but no scenario's:"
        )
        for entry in unattributed[:MAX_LISTED]:
            lines.append(f"    - ticket {entry['ticket']} (tag {entry['tag']!r})")
    other = summary.get("other_runs_count") or 0
    if other:
        lines.append(
            f"  {other} record(s) carry another run's tag (informational, not a finding)"
        )

    stray_reason = summary.get("strays_not_searched_reason") or "adapter cannot enumerate"
    for scenario in artifact.get("scenarios") or []:
        lines.append("")
        not_checked = scenario.get("not_checked") or []
        mark = (
            "ok  " if scenario["passed"]
            else "FAIL" if scenario["findings"]
            else "n/c "
        )
        lines.append(f"  [{mark}] {scenario['scenario']}: {scenario['question']}")
        lines.append(
            f"          {scenario['unique_messages']} message(s), "
            f"{scenario['deliveries']} delivery(ies)"
            + (
                ""
                if scenario["strays_searched"]
                else f", strays not searched ({stray_reason})"
            )
        )
        if scenario["findings"]:
            for finding in scenario["findings"][:MAX_LISTED]:
                lines.append(f"          - {finding}")
            if len(scenario["findings"]) > MAX_LISTED:
                lines.append(
                    f"          ... and {len(scenario['findings']) - MAX_LISTED} "
                    "more, not shown"
                )
        elif not not_checked:
            lines.append("          no findings")
        if not_checked:
            lines.append(
                f"          - {ALL_404_NOT_CHECKED} ({len(not_checked)} message(s))"
            )

    return "\n".join(lines)


def format_dry_run(
    scenarios: Sequence[Scenario],
    readback: ReadbackConfig,
    tag_prefix: str,
    *,
    route: Optional[str] = None,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
) -> str:
    lines = [
        f"dry-run preview: {len(scenarios)} scenario(s), tag prefix {tag_prefix!r}"
    ]
    for scenario in scenarios:
        schedule = ", ".join(
            f"{d.tag}{f' +{d.delay_before:g}s' if d.delay_before else ''}"
            for d in scenario.deliveries
        )
        lines.append("")
        lines.append(f"  {scenario.name}: {scenario.question}")
        lines.append(f"    deliveries: {len(scenario.deliveries)} ({schedule})")
    lines.append("")
    lines.append(f"  readback adapter: kind {readback.kind!r}")
    if readback.url:
        lines.append(f"  readback url:     {readback.url}")
    if readback.path:
        lines.append(f"  readback path:    {readback.path}")
    lines.append(
        f"  checks:           subject match {subject_match}, attachments "
        f"{attachment_match}, "
        + (f"route {route!r}" if route else "routing skipped (no --expect-route)")
    )
    lines.append("no network calls were made (pass --send to fire for real)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def execute(
    seed: int,
    count: int,
    target_name: Optional[str],
    config_path: str,
    out: Optional[str],
    *,
    readback: ReadbackConfig,
    scenarios: Optional[Sequence[str]] = None,
    tag_prefix: str = messages.DEFAULT_TAG_PREFIX,
    rate: float = DEFAULT_RATE,
    retry_after: float = DEFAULT_RETRY_AFTER,
    route: Optional[str] = None,
    body_exact: bool = False,
    subject_match: str = DEFAULT_SUBJECT_MATCH,
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    allow_public_target: bool = False,
    quiet_window: float = DEFAULT_QUIET_WINDOW,
    max_wait: float = DEFAULT_MAX_WAIT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Run every scenario, read back, report. Never called unless
    `guardrails.evaluate_send` already said yes.

    All guardrails run over every scenario's payloads, and the adapter is
    built and preflighted, before the first delivery.
    """
    built = build_scenarios(seed, count, tag_prefix, retry_after, scenarios)
    every_delivery = [d for s in built for d in s.deliveries]

    try:
        require_synthetic([d.email for d in every_delivery])
        validate_match_modes(subject_match, attachment_match)
        target = resolve_target(
            target_name, config_path, allow_public_target=allow_public_target
        )
    except Exception as exc:
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    try:
        adapter = prepare_readback(
            readback, client=readback_client, config_path=config_path
        )
    except Exception as exc:
        printer(f"refused: the readback adapter could not be prepared: {exc}")
        return EXIT_REFUSED

    config = run_config(
        seed, count, tag_prefix, target_name, readback, retry_after, scenarios,
        route=route, body_exact=body_exact, subject_match=subject_match,
        attachment_match=attachment_match, wire_format=target.format_name,
        allow_public_target=bool(target.public),
    )
    sent: List[SentMessage] = []
    phase = "send"
    try:
        for scenario in built:
            sent.extend(
                send_scenario(
                    scenario, target.url, rate, client=client, sleep=sleep,
                    clock=clock, wire_format=target.wire_format,
                )
            )
        phase = "readback"
        outcome = read_back_all(
            adapter,
            [m.probe for m in sent],
            sleep=sleep,
            clock=clock,
            quiet_window=quiet_window,
            max_wait=max_wait,
            poll_interval=poll_interval,
        )
        readbacks = outcome.readbacks
        phase = "stray search"
        strays, strays_reason, strays_partial = enumerate_store(adapter)
    except KeyboardInterrupt:
        # Whole scenarios already delivered are listed; a scenario cut off
        # mid-delivery is not.
        return abort_run("redeliver", seed, config, phase, sent, out, printer)
    except Exception as exc:
        printer(f"redeliver: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(adapter)

    results, stray_summary = judge_run(
        built, readbacks, strays,
        tag_prefix=tag_prefix, route=route, body_exact=body_exact,
        subject_match=subject_match, attachment_match=attachment_match,
        strays_partial=strays_partial,
        readback_failed=readback_failed(outcome),
    )
    stray_summary["strays_not_searched_reason"] = (
        None if stray_summary["strays_searched"] else strays_reason
    )
    artifact = build_artifact(
        seed,
        config,
        results,
        outcome,
        stray_summary,
    )
    printer(format_redelivery(artifact))
    write_artifact(out, artifact)
    return EXIT_OK if artifact["summary"]["verdict"] == "DELIVERY-SAFE" else EXIT_MISMATCH
