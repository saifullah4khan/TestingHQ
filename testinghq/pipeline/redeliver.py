"""redeliver: test what a pipeline does when the provider misbehaves.

THE REALITY. Webhook providers are not a clean request/response channel. They
retry a delivery that times out, they deliver the same event twice, and they
deliver events out of order when several were queued at once. A pipeline built
against the happy path handles none of that, and the two failure modes it
produces are among the most common real bugs in intake systems:

  - a duplicate ticket, because the retry created a second one instead of
    recognising the first
  - a broken thread, because a reply that arrived before its original was
    filed as a new conversation and the original was then filed as another

Both are invisible to a status-code tool. Both were announced with a 200.

THE FOUR SCENARIOS. Each is a real thing a provider does, and each is built
from the same seeded generator, so a redelivery run is reproducible:

  duplicate       the identical message, same Message-ID, delivered twice back
                  to back. A correct pipeline recognises the second and creates
                  nothing.
  slow-retry      the identical message delivered again after a delay, which is
                  what a provider does when its first attempt timed out from the
                  provider's side even though it landed. The delay is
                  configurable, because the window a provider considers "still
                  worth retrying" varies, and a test that assumes one number
                  only proves the pipeline handles that one.
  reply-first     a reply delivered BEFORE the message it replies to. The
                  threading headers are correct; only the order is wrong.
  references      a three-message thread delivered in order, with a growing
                  References chain, so a pipeline that threads only on
                  In-Reply-To and drops the chain still has a chance to pass,
                  and one that threads on neither does not.

WHY THE SCENARIOS SHARE PAYLOADS ON PURPOSE. Every scenario is built from the
same seeded corpus, so the thing being varied between them is the delivery
semantics and nothing else. If a run goes red in all four, that is one parser
or deduplication bug showing up four ways, not four unrelated findings, and
reading them as four is how a real bug gets argued about. What keeps the
scenarios from reading each other's records is the tag: each gets its own
suffix, so a readback keyed by tag can never see a delivery from a different
scenario even though the payload bytes are identical.

WHY LINK AND TOGETHER ARE SEPARATE CHECKS. Correct `In-Reply-To` and
`References` with the reply on a different ticket from its original is a real
and common outcome, and it is the one that makes an agent open a duplicate
ticket a fortnight later. Collapsing "is threaded" into one boolean would hide
which half is broken, and the two have different fixes: one is a header
propagation bug, the other is a deduplication bug. See
`pipeline/expectations.py` for the checks themselves.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..blast.payload import InboundEmail
from ..core.ratelimit import TokenBucket
from ..core.transport import post
from . import messages
from .adapters import ReadbackConfig, build_adapter
from .common import (
    EXIT_MISMATCH,
    EXIT_OK,
    EXIT_REFUSED,
    SentMessage,
    close_adapter,
    read_back_all,
    require_synthetic,
    resolve_target_url,
)
from .expectations import (
    Expectations,
    check_thread_link,
    check_thread_together,
    evaluate_sequence,
)
from .readback import Readback, can_enumerate
from .verify import DEFAULT_RATE, build_clean_corpus

#: How long a provider waits before deciding a delivery failed. Five seconds is
#: the common floor for webhook retry schedules and is deliberately short, so a
#: run is quick. `--retry-after` exists because a pipeline with a longer
#: deduplication window is being asked a different question, and testing it at
#: five seconds would quietly answer a question nobody asked.
DEFAULT_RETRY_AFTER = 5.0

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
    """
    stamped = messages.stamp(
        email,
        tag,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
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
    """The identical message delivered twice, the second `gap` seconds later.

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
    deliveries: List[Delivery] = []
    for index, email in enumerate(corpus):
        tag = messages.make_tag(tag_prefix, seed, index)
        message_id = messages.build_chain_message_id(f"{id_prefix}-{seed}-{index:04d}")
        record_id = f"clean-{seed}-{index:04d}"
        deliveries.append(
            Delivery(
                email=messages.stamp(email, tag, message_id=message_id),
                tag=tag,
                record_id=record_id,
                message_id=message_id,
            )
        )
        deliveries.append(
            Delivery(
                # Stamped from the original again rather than from the already
                # stamped payload, so the two deliveries are byte-identical
                # rather than carrying the marker twice.
                email=messages.stamp(email, tag, message_id=message_id),
                tag=tag,
                record_id=record_id,
                message_id=message_id,
                delay_before=gap,
            )
        )
    return tuple(deliveries)


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
            f"the identical message re-sent {retry_after:g}s later, as a provider "
            "retrying a delivery it believes timed out: one ticket"
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
            "conversation, with the full ancestry intact"
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

    @property
    def passed(self) -> bool:
        return not self.findings

    def to_json(self) -> Dict[str, Any]:
        return {
            "scenario": self.name,
            "question": self.question,
            "deliveries": self.deliveries,
            "unique_messages": self.unique_messages,
            "strays_searched": self.strays_searched,
            "passed": self.passed,
            "findings": list(self.findings),
            "messages": self.records,
        }


def judge_scenario(
    scenario: Scenario, readbacks: Dict[str, List[Readback]]
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
      thread_link  each reply's In-Reply-To and References name its parent.
      thread_together  each reply ended up on the same ticket as its parent.
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

        if not found:
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
            expectations = Expectations.from_email(delivery.email)
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
        link = check_thread_link(first_by_tag.get(tag), parent.message_id)
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


def judge_all(
    scenarios: Sequence[Scenario],
    readbacks: Dict[str, List[Readback]],
    strays: Optional[Sequence[Readback]] = None,
) -> List[ScenarioResult]:
    """Grade every scenario, then hunt for records nobody sent.

    Strays are reported against the run as a whole rather than per scenario,
    because a ticket that belongs to no scenario is a property of the run and
    duplicating the same finding into four scenario sections would make four
    bugs out of one. When the adapter cannot enumerate, every scenario records
    `strays_searched: False` rather than passing a check that did not run.
    """
    results = [judge_scenario(scenario, readbacks) for scenario in scenarios]

    if strays is None:
        for result in results:
            result.strays_searched = False
        return results

    sent_tags = {tag for scenario in scenarios for tag in scenario.tags}
    unexpected = [r for r in strays if not r.tag or r.tag not in sent_tags]
    if unexpected:
        joined = "; ".join(
            f"ticket {r.ticket_id} (tag {r.tag or 'none'!r})" for r in unexpected
        )
        results[0].findings.append(
            f"the system holds {len(unexpected)} record(s) this run never sent: {joined}"
        )
    return results


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
) -> Dict[str, Any]:
    return {
        "tool": "redeliver",
        "seed": seed,
        "count": count,
        "tag_prefix": tag_prefix,
        "target": target_name,
        "readback": readback.to_json(),
        "retry_after": retry_after,
        "scenarios": list(selected) if selected else list(SCENARIOS),
        "dry_run": False,
    }


def build_artifact(
    seed: int, config: Dict[str, Any], results: Sequence[ScenarioResult]
) -> Dict[str, Any]:
    failing = [r for r in results if not r.passed]
    return {
        "seed": seed,
        "config": config,
        "summary": {
            "scenarios": len(results),
            "passed": len(results) - len(failing),
            "failed": len(failing),
            "deliveries": sum(r.deliveries for r in results),
            "findings": sum(len(r.findings) for r in results),
            "strays_searched": all(r.strays_searched for r in results),
            "verdict": "DELIVERY-SAFE" if not failing else "DELIVERY-BUGS",
        },
        "scenarios": [r.to_json() for r in results],
    }


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

    for scenario in artifact.get("scenarios") or []:
        lines.append("")
        mark = "ok  " if scenario["passed"] else "FAIL"
        lines.append(f"  [{mark}] {scenario['scenario']}: {scenario['question']}")
        lines.append(
            f"          {scenario['unique_messages']} message(s), "
            f"{scenario['deliveries']} delivery(ies)"
            + (
                ""
                if scenario["strays_searched"]
                else ", strays not searched (adapter cannot enumerate)"
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
        else:
            lines.append("          no findings")

    return "\n".join(lines)


def format_dry_run(
    scenarios: Sequence[Scenario], readback: ReadbackConfig, tag_prefix: str
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
    client: Any = None,
    readback_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    printer: Callable[[str], None] = print,
) -> int:
    """Run every scenario, read back, report. Never called unless
    `guardrails.evaluate_send` already said yes.

    All guardrails run over every scenario's payloads before the first
    delivery, not per scenario. A refusal that lands after the duplicate
    scenario has already been delivered has still hit the operator's endpoint
    with part of the test, which is the failure mode the three-phase order in
    `pipeline/common.py` exists to prevent.
    """
    built = build_scenarios(seed, count, tag_prefix, retry_after, scenarios)
    every_delivery = [d for s in built for d in s.deliveries]

    try:
        require_synthetic([d.email for d in every_delivery])
        url = resolve_target_url(target_name, config_path)
    except Exception as exc:
        printer(f"refused: {exc}")
        return EXIT_REFUSED

    adapter = None
    try:
        # Inside the try, not before it: an adapter that cannot be built is as
        # much a refusal as a target that cannot be resolved, and an
        # unimportable spec left out here escaped as a traceback.
        adapter = build_adapter(readback, client=readback_client)
        sent: List[SentMessage] = []
        for scenario in built:
            sent.extend(
                send_scenario(
                    scenario, url, rate, client=client, sleep=sleep, clock=clock
                )
            )
        readbacks = read_back_all(adapter, [m.probe for m in sent], sleep=sleep)
        strays: Optional[List[Readback]] = (
            list(adapter.list_all()) if can_enumerate(adapter) else None
        )
    except Exception as exc:
        printer(f"redeliver: could not complete the run: {exc}")
        return EXIT_REFUSED
    finally:
        close_adapter(adapter)

    results = judge_all(built, readbacks, strays)
    artifact = build_artifact(
        seed,
        run_config(seed, count, tag_prefix, target_name, readback, retry_after, scenarios),
        results,
    )
    printer(format_redelivery(artifact))
    if out:
        Path(out).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")
    return EXIT_OK if all(r.passed for r in results) else EXIT_MISMATCH
