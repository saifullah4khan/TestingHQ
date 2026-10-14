"""The check layer: ground truth in, a verdict out.

This is what turns "a 200" into an answer to the question an intake owner
actually asks: did the pipeline produce the right thing? It knows nothing about
tickets, queues, mailboxes or any product. It compares declared expectations
against what a `Readback` reports.

THE THREE-STATE VERDICT. Every check resolves to PASSED, FAILED or SKIPPED, and
the third is the one that matters most. A field the adapter cannot see is
SKIPPED, never PASSED: a tool that reports "subject: ok" for a system it never
managed to read a subject from is believed, and a believed wrong answer is worse
than no answer.

THE THREE SKIP REASONS, kept apart because they mean opposite things. A missing
record is the finding itself, an invisible field is a gap in the integration, and
"nothing to check" (most often a payload with no attachments) says the question
did not apply. Under one heading the report would blame the adapter for every
payload that simply had no files.

THE ONE EXCEPTION. `ticket_created` does not skip. It is built on `exists` and
`ticket_id`, which `Readback` requires whenever it reports a record at all, and a
lookup that found nothing is the most important thing this tool can report.

MATCH MODES. Body matching is containment by default: the payload carries a text
and an HTML part, a pipeline stores whichever it prefers, and `body_core` is the
substantive sentence. A body stored as HTML is compared after tag stripping and
entity unescaping. `body_exact` is opt-in. Subjects default to `contains`, so a
helpdesk prefix such as "[Ticket #123]" is not a failure; `exact` and `suffix`
are stricter. Attachments default to `exact` names; `loose` compares
casefolded alphanumeric stems, for systems that sanitise filenames. Every check
detail names the mode it used.

ROUTING. There is no default route. Without a declared `route` the routing
check is SKIPPED with reason "no expected route"."""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..blast.payload import InboundEmail
from ..core import report
from .readback import (
    Readback,
    ReadbackError,
    normalize_attachment_names,
    normalize_message_ids,
    normalize_route,
    normalize_text,
)

#: The named checks, in the order they are reported. The order is the order a
#: reader triages in: did anything get created at all, then was it the right
#: thing, then were the bits that get dropped attached.
CHECK_TICKET_CREATED = "ticket_created"
CHECK_SENDER = "sender"
CHECK_SUBJECT = "subject"
CHECK_BODY = "body"
CHECK_ATTACHMENTS = "attachments"
CHECK_ROUTING = "routing"

CHECKS: Tuple[str, ...] = (
    CHECK_TICKET_CREATED,
    CHECK_SENDER,
    CHECK_SUBJECT,
    CHECK_BODY,
    CHECK_ATTACHMENTS,
    CHECK_ROUTING,
)

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

#: Why a check did not run. Three causes, and they need to be told apart
#: because they mean opposite things: one is a gap in the integration, one is
#: the finding itself, and one says the question did not apply. A report that
#: files "the adapter cannot see the sender" and "this payload had no
#: attachments" under one heading is making a claim about the adapter that is
#: false half the time.
SKIP_NO_RECORD = "no record found"
SKIP_NOT_VISIBLE = "adapter cannot see field"
SKIP_NOTHING_TO_CHECK = "nothing to check"
#: The routing check had nothing to compare against: no `--expect-route`.
SKIP_NO_EXPECTED_ROUTE = "no expected route"

SKIP_REASONS: Tuple[str, ...] = (
    SKIP_NO_RECORD,
    SKIP_NOT_VISIBLE,
    SKIP_NOTHING_TO_CHECK,
    SKIP_NO_EXPECTED_ROUTE,
)

#: How the subject check compares, after normalisation. `contains` (the
#: default) passes when the expected subject appears anywhere in the stored
#: one; `suffix` when the stored one ends with it; `exact` when they are equal.
SUBJECT_MATCH_EXACT = "exact"
SUBJECT_MATCH_CONTAINS = "contains"
SUBJECT_MATCH_SUFFIX = "suffix"
SUBJECT_MATCH_MODES: Tuple[str, ...] = (
    SUBJECT_MATCH_EXACT,
    SUBJECT_MATCH_CONTAINS,
    SUBJECT_MATCH_SUFFIX,
)
DEFAULT_SUBJECT_MATCH = SUBJECT_MATCH_CONTAINS

#: How attachment names are compared. `exact` compares casefolded basenames as
#: a set; `loose` compares casefolded stems with every non-alphanumeric removed,
#: as a multiset, so "Q3 report.pdf" and "q3_report.PDF" are the same file.
ATTACHMENT_MATCH_EXACT = "exact"
ATTACHMENT_MATCH_LOOSE = "loose"
ATTACHMENT_MATCH_MODES: Tuple[str, ...] = (ATTACHMENT_MATCH_EXACT, ATTACHMENT_MATCH_LOOSE)
DEFAULT_ATTACHMENT_MATCH = ATTACHMENT_MATCH_EXACT


class ExpectationError(ValueError):
    """Raised when an expectation set is malformed, which is a caller bug and
    must not be reported as a pipeline failure."""


def validate_match_modes(subject_match: str, attachment_match: str) -> None:
    """Raise `ExpectationError` for a match mode this module does not know.
    Tools call it before sending, so a typo is a refusal and not a lost run."""
    if subject_match not in SUBJECT_MATCH_MODES:
        raise ExpectationError(
            f"subject match {subject_match!r} is not one of "
            f"{list(SUBJECT_MATCH_MODES)}"
        )
    if attachment_match not in ATTACHMENT_MATCH_MODES:
        raise ExpectationError(
            f"attachment match {attachment_match!r} is not one of "
            f"{list(ATTACHMENT_MATCH_MODES)}"
        )


@dataclass(frozen=True)
class Expectations:
    """What the system under test was supposed to produce for one payload.

    Built from a generated payload's own ground truth, plus an optional
    declared route and the match modes. `route` is None unless the operator
    declared one, and a None route makes the routing check SKIPPED.
    """

    sender: str
    subject: str
    body_core: str
    body_full: str = ""
    attachment_names: Tuple[str, ...] = field(default_factory=tuple)
    route: Optional[str] = None
    body_exact: bool = False
    subject_match: str = DEFAULT_SUBJECT_MATCH
    attachment_match: str = DEFAULT_ATTACHMENT_MATCH

    def __post_init__(self) -> None:
        validate_match_modes(self.subject_match, self.attachment_match)

    @classmethod
    def from_email(
        cls,
        email: InboundEmail,
        route: Optional[str] = None,
        body_exact: bool = False,
        *,
        subject_match: str = DEFAULT_SUBJECT_MATCH,
        attachment_match: str = DEFAULT_ATTACHMENT_MATCH,
    ) -> "Expectations":
        return cls(
            sender=email.ground_truth.from_addr,
            subject=email.ground_truth.subject,
            body_core=email.ground_truth.body_core,
            body_full=email.text,
            attachment_names=tuple(
                attachment.filename for attachment in email.attachments
            ),
            route=route,
            body_exact=body_exact,
            subject_match=subject_match,
            attachment_match=attachment_match,
        )

    @property
    def body_match(self) -> str:
        return "exact" if self.body_exact else "contains"

    def expected_body(self) -> str:
        """The body to grade against, given the mode. Falls back to `body_core`
        when no full body was supplied, so a record-only caller still gets a
        meaningful answer rather than comparing against an empty string."""
        if self.body_exact and self.body_full:
            return self.body_full
        return self.body_core

    def route_for(self, email: Optional[InboundEmail] = None) -> Optional[str]:
        """The declared route, or None. There is no default: a payload's
        recipient is not a routing decision, and a helpdesk that maps routes to
        groups would fail every message against it. `email` is accepted for
        callers that still pass it and is not consulted."""
        return self.route

    def to_json(self) -> Dict[str, Any]:
        return {
            "sender": self.sender,
            "subject": self.subject,
            "body_core": self.body_core,
            "attachments": list(self.attachment_names),
            "route": self.route,
            "body_exact": self.body_exact,
            "subject_match": self.subject_match,
            "attachment_match": self.attachment_match,
        }


def match_options(
    route: Optional[str],
    body_exact: bool,
    subject_match: str,
    attachment_match: str,
) -> Dict[str, Any]:
    """The match settings a run used, as recorded in an artifact's config."""
    return {
        "expect_route": route,
        "body_exact": body_exact,
        "subject_match": subject_match,
        "attachment_match": attachment_match,
    }


@dataclass(frozen=True)
class CheckResult:
    """One field's verdict.

    `passed` is None exactly when the check was SKIPPED, so the three states
    cannot be confused by a caller that forgets to look at `status`. `reason`
    is set only on a skip, and names which of the three causes it was.
    """

    name: str
    passed: Optional[bool]
    detail: str
    expected: Any = None
    actual: Any = None
    reason: Optional[str] = None

    @property
    def status(self) -> str:
        if self.passed is None:
            return SKIPPED
        return PASSED if self.passed else FAILED

    def to_json(self) -> Dict[str, Any]:
        return {
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Verification:
    """The full verdict for one payload."""

    record_id: str
    tag: str
    found: bool
    checks: Tuple[CheckResult, ...]

    @property
    def failures(self) -> Tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.passed is False)

    @property
    def skipped(self) -> Tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.passed is None)

    def skips_by_reason(self) -> Dict[str, Dict[str, int]]:
        """Skipped checks as {reason: {check: count}}, every reason present even
        when it has none.

        A reason with a zero is kept, because a report that only shows the
        causes it hit is a report where a reader has to guess whether the
        missing cause means zero or means nobody looked.
        """
        grouped: Dict[str, Dict[str, int]] = {reason: {} for reason in SKIP_REASONS}
        for check in self.skipped:
            reason = check.reason or SKIP_NOT_VISIBLE
            grouped.setdefault(reason, {})
            grouped[reason][check.name] = grouped[reason].get(check.name, 0) + 1
        return grouped

    @property
    def passed(self) -> bool:
        """True only when every check that ran passed.

        A verdict of "passed" with skipped checks in it is still passed, and
        that is deliberate: the operator decides which checks are meaningful by
        choosing which fields the adapter exposes, and refusing to call a run
        clean because an adapter cannot see a queue name would be a different
        tool. What the tool owes is that SKIPPED is impossible to miss, which
        is what the summary and the exit code are for.
        """
        return not self.failures

    def mismatch_strings(self) -> List[str]:
        """One short human-readable line per failed check, in check order."""
        return [f"{c.name}: {c.detail}" for c in self.failures]

    def to_json(self) -> Dict[str, Any]:
        return {
            "record_id": self.record_id,
            "tag": self.tag,
            "found": self.found,
            "passed": self.passed,
            "checks": [c.to_json() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

_ANGLE_ADDRESS = re.compile(r"<\s*([^<>\s]+@[^<>\s]+)\s*>")
_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
_HTML_DROP = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\b[^>]*>", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def sender_address(value: Any) -> str:
    """The bare, lowercase address in a stored sender, or "".

    An address in angle brackets wins, so "Last, First <a@b.example>" reads as
    a@b.example rather than splitting on the comma. Otherwise the first entry
    `getaddresses` finds that contains an "@". A display name with no address
    yields "".
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    bracketed = _ANGLE_ADDRESS.search(value)
    if bracketed:
        return bracketed.group(1).strip().lower()
    for _name, address in getaddresses([value]):
        if "@" in address:
            return address.strip().strip("<>").lower()
    return ""


def looks_like_html(value: Any) -> bool:
    return isinstance(value, str) and bool(_HTML_TAG.search(value))


def html_to_text(value: str) -> str:
    """Tags removed, block ends turned into line breaks, entities unescaped."""
    text = _HTML_DROP.sub(" ", value)
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_TAG.sub(" ", text)
    return html.unescape(text)


def loose_attachment_key(name: str) -> str:
    """Basename, extension dropped, casefolded, non-alphanumerics removed."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem = base.rsplit(".", 1)[0] if "." in base[1:] else base
    key = _NON_ALNUM.sub("", stem.casefold())
    return key or base.casefold()


# ---------------------------------------------------------------------------
# The individual checks
# ---------------------------------------------------------------------------


def _not_visible(
    name: str, detail: str, readback: Readback, *fields: str
) -> CheckResult:
    """SKIPPED as not visible, with the adapter's note on what shape it found
    in each field, when it has one."""
    note_for = getattr(readback, "note_for", None)
    notes = []
    if callable(note_for):
        for field_name in fields:
            note = note_for(field_name)
            if note:
                notes.append(f"{field_name}: {note}")
    if notes:
        detail = f"{detail} ({'; '.join(notes)})"
    return _skipped(name, detail, SKIP_NOT_VISIBLE)


def _skipped(name: str, detail: str, reason: str) -> CheckResult:
    if reason not in SKIP_REASONS:  # pragma: no cover - guards a typo
        raise ExpectationError(
            f"{reason!r} is not a skip reason; expected one of {list(SKIP_REASONS)}"
        )
    return CheckResult(name=name, passed=None, detail=detail, reason=reason)


def _failed(name: str, detail: str, expected: Any = None, actual: Any = None) -> CheckResult:
    return CheckResult(
        name=name, passed=False, detail=detail, expected=expected, actual=actual
    )


def _passed(name: str, detail: str) -> CheckResult:
    return CheckResult(name=name, passed=True, detail=detail)


def _check_ticket_created(readback: Optional[Readback]) -> CheckResult:
    if readback is None:
        return _failed(
            CHECK_TICKET_CREATED,
            "the system under test holds no record of this message",
        )
    if not readback.exists:
        return _failed(
            CHECK_TICKET_CREATED,
            f"the system holds a record for this message but it is not live "
            f"(ticket {readback.ticket_id})",
            actual=readback.ticket_id,
        )
    return _passed(
        CHECK_TICKET_CREATED, f"created as {readback.ticket_id}"
    )


def _check_sender(
    readback: Optional[Readback], expectations: Expectations
) -> CheckResult:
    if readback is None:
        return _skipped(CHECK_SENDER, "no record to read a sender from", SKIP_NO_RECORD)
    if not readback.has("from_addr"):
        return _not_visible(CHECK_SENDER, "the adapter could not see a sender on this record", readback, "from_addr")
    expected = sender_address(expectations.sender)
    actual = sender_address(readback.from_addr)
    if expected == actual:
        return _passed(CHECK_SENDER, f"sender parsed as {actual or '(empty)'}")
    stored = readback.from_addr
    if not actual and isinstance(stored, str) and stored.strip():
        return _failed(
            CHECK_SENDER,
            f"expected {expected or '(empty)'} but the system stored only a "
            f"display name ({stored.strip()!r}) with no address",
            expected=expectations.sender,
            actual=stored,
        )
    return _failed(
        CHECK_SENDER,
        f"expected {expected or '(empty)'} but the system recorded "
        f"{actual or '(empty)'}",
        expected=expectations.sender,
        actual=readback.from_addr,
    )


def _check_subject(
    readback: Optional[Readback], expectations: Expectations
) -> CheckResult:
    if readback is None:
        return _skipped(CHECK_SUBJECT, "no record to read a subject from", SKIP_NO_RECORD)
    if not readback.has("subject"):
        return _not_visible(CHECK_SUBJECT, "the adapter could not see a subject on this record", readback, "subject")
    mode = expectations.subject_match
    expected = normalize_text(expectations.subject)
    actual = normalize_text(readback.subject)
    if mode == SUBJECT_MATCH_EXACT:
        ok = expected == actual
    elif mode == SUBJECT_MATCH_SUFFIX:
        ok = actual.endswith(expected)
    else:
        ok = expected in actual
    if ok:
        return _passed(CHECK_SUBJECT, f"subject parsed intact (match: {mode})")
    return _failed(
        CHECK_SUBJECT,
        f"expected {expected!r} but the system recorded {actual!r} (match: {mode})",
        expected=expectations.subject,
        actual=readback.subject,
    )


def _check_body(
    readback: Optional[Readback], expectations: Expectations
) -> CheckResult:
    if readback is None:
        return _skipped(CHECK_BODY, "no record to read a body from", SKIP_NO_RECORD)
    if not readback.has("body"):
        return _not_visible(CHECK_BODY, "the adapter could not see a body on this record", readback, "body")
    expected = normalize_text(expectations.expected_body())
    if not expected:
        # SKIPPED, not PASSED: an empty expectation is contained in anything.
        return _skipped(
            CHECK_BODY, "the payload carried no body to check", SKIP_NOTHING_TO_CHECK
        )
    stored = readback.body if isinstance(readback.body, str) else ""
    stripped = looks_like_html(stored)
    actual = normalize_text(html_to_text(stored) if stripped else stored)
    mode = expectations.body_match
    how = f"match: {mode}" + (", after HTML tag stripping" if stripped else "")
    if expectations.body_exact:
        ok = expected == actual
        detail = f"body matched exactly ({how})" if ok else (
            f"expected exactly {expected!r} but the system recorded {actual!r} "
            f"({how})"
        )
    else:
        ok = expected in actual
        detail = f"body survived intact ({how})" if ok else (
            f"the substantive text {expected!r} is not present in what the "
            f"system recorded ({how})"
        )
    if ok:
        return _passed(CHECK_BODY, detail)
    return _failed(
        CHECK_BODY, detail, expected=expectations.expected_body(), actual=readback.body
    )


def _check_attachments(
    readback: Optional[Readback], expectations: Expectations
) -> CheckResult:
    if readback is None:
        return _skipped(CHECK_ATTACHMENTS, "no record to read attachments from", SKIP_NO_RECORD)
    if not readback.has("attachment_names"):
        return _not_visible(
            CHECK_ATTACHMENTS,
            "the adapter could not see attachments on this record",
            readback,
            "attachment_names",
        )
    expected = normalize_attachment_names(expectations.attachment_names)
    if not expected:
        # SKIPPED, not PASSED. The payload genuinely had no attachments, so
        # there is no attachment handling to have got right, and reporting
        # "attachments: ok" would be a check that never ran wearing the
        # costume of one that did.
        return _skipped(
            CHECK_ATTACHMENTS, "the payload carried no attachments to check",
            SKIP_NOTHING_TO_CHECK,
        )
    mode = expectations.attachment_match
    if mode == ATTACHMENT_MATCH_LOOSE:
        return _check_attachments_loose(readback, expectations)
    actual = normalize_attachment_names(readback.attachment_names)
    if expected == actual:
        return _passed(
            CHECK_ATTACHMENTS,
            f"all {len(expected)} attachment(s) present (match: {mode})",
        )
    missing = [name for name in expected if name not in actual]
    extra = [name for name in actual if name not in expected]
    parts = []
    if missing:
        parts.append(f"missing {missing}")
    if extra:
        parts.append(f"unexpected {extra}")
    return _failed(
        CHECK_ATTACHMENTS,
        f"the system stored {len(actual)} attachment(s): " + "; ".join(parts)
        + f" (match: {mode})",
        expected=list(expected),
        actual=list(actual),
    )


def _check_attachments_loose(
    readback: Readback, expectations: Expectations
) -> CheckResult:
    expected_names = [n for n in expectations.attachment_names if isinstance(n, str)]
    actual_names = [n for n in (readback.attachment_names or ()) if isinstance(n, str)]
    expected = Counter(loose_attachment_key(n) for n in expected_names)
    actual = Counter(loose_attachment_key(n) for n in actual_names)
    if expected == actual:
        return _passed(
            CHECK_ATTACHMENTS,
            f"all {sum(expected.values())} attachment(s) present (match: loose)",
        )
    missing = [
        n for n in expected_names if (expected - actual).get(loose_attachment_key(n))
    ]
    extra = [
        n for n in actual_names if (actual - expected).get(loose_attachment_key(n))
    ]
    parts = []
    if missing:
        parts.append(f"missing {missing}")
    if extra:
        parts.append(f"unexpected {extra}")
    return _failed(
        CHECK_ATTACHMENTS,
        f"the system stored {len(actual_names)} attachment(s): " + "; ".join(parts)
        + " (match: loose)",
        expected=expected_names,
        actual=actual_names,
    )


def _check_routing(
    readback: Optional[Readback], expectations: Expectations, email: InboundEmail
) -> CheckResult:
    expected_value = expectations.route_for(email)
    if expected_value is None:
        return _skipped(
            CHECK_ROUTING,
            "no expected route: declare one with --expect-route to check routing",
            SKIP_NO_EXPECTED_ROUTE,
        )
    if readback is None:
        return _skipped(CHECK_ROUTING, "no record to read a route from", SKIP_NO_RECORD)
    if not readback.has("route"):
        return _not_visible(CHECK_ROUTING, "the adapter could not see a route on this record", readback, "route")
    expected = normalize_route(expected_value)
    actual = normalize_route(readback.route)
    if expected == actual:
        return _passed(CHECK_ROUTING, f"routed to {actual or '(empty)'}")
    return _failed(
        CHECK_ROUTING,
        f"expected route {expected or '(empty)'} but the system recorded "
        f"{actual or '(empty)'}",
        expected=expected_value,
        actual=readback.route,
    )


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------


def evaluate(
    email: InboundEmail,
    readback: Optional[Readback],
    expectations: Optional[Expectations] = None,
    record_id: str = "",
    tag: str = "",
) -> Verification:
    """Grade one payload against what the system reported about it.

    `readback` is None when the adapter found nothing at all. That is not a
    skipped verification: it is the most consequential thing this tool reports,
    and it fails `ticket_created` outright.

    The other five checks report SKIPPED in that case, because there is
    genuinely nothing to compare. They do not report PASSED, which is the
    distinction the whole module is built around.
    """
    if expectations is None:
        expectations = Expectations.from_email(email)

    checks = (
        _check_ticket_created(readback),
        _check_sender(readback, expectations),
        _check_subject(readback, expectations),
        _check_body(readback, expectations),
        _check_attachments(readback, expectations),
        _check_routing(readback, expectations, email),
    )
    return Verification(
        record_id=record_id or tag,
        tag=tag,
        found=readback is not None,
        checks=checks,
    )


def evaluate_sequence(
    email: InboundEmail,
    readbacks: Sequence[Readback],
    expectations: Optional[Expectations] = None,
    record_id: str = "",
    tag: str = "",
) -> Verification:
    """Grade one payload when the adapter may have returned several records.

    More than one is a duplicate, and a duplicate is not something to average
    over or pick a favourite of: the first is graded, and the rest are reported
    as a failure naming their ticket ids. Choosing one would make a pipeline
    that files every message twice indistinguishable from a correct one whenever
    the first copy happened to parse well, which is most of the time.
    """
    if expectations is None:
        expectations = Expectations.from_email(email)

    if not readbacks:
        return evaluate(email, None, expectations, record_id, tag)

    if len(readbacks) == 1:
        return evaluate(email, readbacks[0], expectations, record_id, tag)

    first = evaluate(email, readbacks[0], expectations, record_id, tag)
    duplicate_ids = [str(r.ticket_id) for r in readbacks]
    checks = (
        CheckResult(
            name=CHECK_TICKET_CREATED,
            passed=False,
            detail=(
                f"the system holds {len(readbacks)} records for one message "
                f"(tickets {duplicate_ids}); a duplicated message is a real "
                "intake bug, not a rounding difference"
            ),
            expected=1,
            actual=len(readbacks),
        ),
    ) + first.checks[1:]
    return Verification(
        record_id=record_id or tag,
        tag=tag,
        found=True,
        checks=checks,
    )


# ---------------------------------------------------------------------------
# Threading checks, shared by the redelivery tool
# ---------------------------------------------------------------------------


def check_thread_link(
    readback: Optional[Readback],
    parent_message_id: str,
    ancestry: Sequence[str] = (),
) -> CheckResult:
    """Is this reply actually linked to the message it claims to answer?

    Checks `in_reply_to` first and `references` second, independently, because
    a pipeline that threads on one and ignores the other is common and a single
    combined boolean would hide which half is broken. `In-Reply-To` must name
    the immediate parent. `References` must contain the parent and, when
    `ancestry` is given, every message id in it: the full chain, not just the
    last link.

    Only the headers the adapter could see are judged, and a readback that
    exposes neither is SKIPPED rather than failed. Blaming the pipeline for a
    header the integration never read would put a permanent false positive in
    every run against an API that does not expose threading, which is how a
    check gets switched off.
    """
    name = "thread_link"
    if readback is None:
        return _skipped(name, "no record to read threading headers from", SKIP_NO_RECORD)
    if not parent_message_id:
        return _skipped(name, "no parent message id to check against", SKIP_NOTHING_TO_CHECK)

    parent_ids = set(normalize_message_ids(parent_message_id))
    if not parent_ids:
        return _skipped(name, "the parent message id normalizes to nothing", SKIP_NOTHING_TO_CHECK)

    visible = [
        field_name
        for field_name in ("in_reply_to", "references")
        if readback.has(field_name)
    ]
    if not visible:
        return _not_visible(
            name,
            "the adapter could not see an In-Reply-To or a References header",
            readback,
            "in_reply_to",
            "references",
        )

    problems = []
    if "in_reply_to" in visible:
        if set(normalize_message_ids(readback.in_reply_to)) != parent_ids:
            problems.append(
                f"In-Reply-To is {readback.in_reply_to!r}, not {parent_message_id!r}"
            )
    wanted_chain = list(normalize_message_ids(list(ancestry))) if ancestry else []
    if "references" in visible:
        stored = set(normalize_message_ids(readback.references))
        if not parent_ids <= stored:
            problems.append(
                f"References {list(readback.references)} does not contain "
                f"{parent_message_id!r}"
            )
        else:
            lost = [mid for mid in wanted_chain if mid not in stored]
            if lost:
                problems.append(
                    f"References {list(readback.references)} does not carry the "
                    f"full ancestry: missing {lost}"
                )

    if problems:
        return _failed(
            name,
            "; ".join(problems),
            expected=wanted_chain or parent_message_id,
            actual=list(readback.references) if "references" in visible else None,
        )

    unseen = [f for f in ("in_reply_to", "references") if f not in visible]
    detail = f"linked to {parent_message_id}"
    if wanted_chain and "references" in visible:
        detail += f", References carries all {len(wanted_chain)} ancestor(s)"
    if unseen:
        detail += f" ({', '.join(unseen)} not checked: the adapter could not see it)"
    return CheckResult(name=name, passed=True, detail=detail, expected=parent_message_id)


def check_thread_together(
    reply: Optional[Readback], root: Optional[Readback]
) -> CheckResult:
    """Did the reply end up on the same ticket as the message it replies to?

    A separate check from `check_thread_link` on purpose. Correct threading
    headers and two separate tickets is a real and common outcome, and it is
    the one that makes an agent open a duplicate ticket three weeks later. Which
    half of "threaded" is broken is the actionable part, so the two are never
    collapsed into one verdict.

    The ticket ids are read directly rather than through `has`, because
    `Readback` requires an identifier from any adapter that reports a record at
    all. Consulting `fields` here would let a readback carrying a perfectly good
    ticket id declare the field invisible and quietly skip the check.
    """
    name = "thread_together"
    if reply is None or root is None:
        return _skipped(name, "both the reply and its original must be present", SKIP_NO_RECORD)
    if not reply.ticket_id or not root.ticket_id:
        return _skipped(name, "one of the two records carries no ticket id", SKIP_NOTHING_TO_CHECK)
    if reply.ticket_id == root.ticket_id:
        return _passed(name, f"both on ticket {reply.ticket_id}")
    return _failed(
        name,
        f"the reply is on ticket {reply.ticket_id} but its original is on "
        f"{root.ticket_id}",
        expected=root.ticket_id,
        actual=reply.ticket_id,
    )


# ---------------------------------------------------------------------------
# A Matcher for the existing engine
# ---------------------------------------------------------------------------


class GroundTruthMatcher:
    """A `core.report.Matcher` that grades on content rather than on status.

    This is the join between the pipeline tools and everything Blast already
    does. `core.report` defined the Matcher protocol and a `readback` argument
    on `build_record` in M3, with a `StatusOnlyMatcher` as the default, and
    nothing in the tree ever supplied a real readback. This is that readback's
    other end: the same three-argument `match` signature, so it drops into
    `report.build_record(..., matcher=...)` unchanged and a verify artifact is
    a blast artifact with a stricter assertion.

    Content rules only. Whether the transport succeeded is
    `report.StatusOnlyMatcher`'s job and `report.classify_record`'s, and
    duplicating them here would be the exact third-copy failure
    `tests/test_repo_invariants.py` exists to prevent. This matcher only turns
    "the system parsed it wrong" into a failed assertion.
    """

    def __init__(self, expectations: Optional[Expectations] = None) -> None:
        self.expectations = expectations

    def match(
        self, record: Dict[str, Any], response: Dict[str, Any], readback: Any
    ) -> report.MatchResult:
        if readback is None:
            return report.MatchResult(
                passed=False, mismatches=["no record was found for this message"]
            )
        if isinstance(readback, (list, tuple)):
            if len(readback) != 1:
                return report.MatchResult(
                    passed=False,
                    mismatches=[
                        f"the system holds {len(readback)} records for one message"
                    ],
                )
            readback = readback[0]
        if not isinstance(readback, Readback):
            raise ReadbackError(
                "GroundTruthMatcher needs a Readback, or a one-item sequence of "
                f"them; got {type(readback).__name__}"
            )

        expectations = self.expectations or _expectations_from_record(record)
        verification = evaluate_from_record(record, readback, expectations)
        return report.MatchResult(
            passed=verification.passed, mismatches=verification.mismatch_strings()
        )


def _expectations_from_record(record: Dict[str, Any]) -> Expectations:
    """Rebuild expectations from a blast record's `intended` block, so the
    matcher works against any record the engine built without the caller having
    to hold the payload.

    Attachment names are deliberately not reconstructed. A record carries a
    count, and inventing plausible filenames for a count would be a check
    against data nobody produced, so `evaluate_from_record` skips that field
    instead.
    """
    intended = record.get("intended") or {}
    missing = [key for key in ("from", "subject", "body_core") if key not in intended]
    if missing:
        raise ExpectationError(
            f"record {record.get('id')!r} has no {missing} in its intended block, "
            "so it cannot be graded against ground truth"
        )
    return Expectations(
        sender=intended["from"],
        subject=intended["subject"],
        body_core=intended["body_core"],
        route=intended.get("route"),
    )


def evaluate_from_record(
    record: Dict[str, Any], readback: Readback, expectations: Expectations
) -> Verification:
    """Grade using a record that has no payload attached.

    The attachment check is the one that cannot be reconstructed here: a blast
    record stores how many attachments there were, not what they were named, so
    this reports it as SKIPPED rather than guessing. That is the honest answer
    and it is why the tools in this package that care about attachment names
    (`verify`, `ledger`) carry the payload rather than only the record.
    """
    checks = (
        _check_ticket_created(readback),
        _check_sender(readback, expectations),
        _check_subject(readback, expectations),
        _check_body(readback, expectations),
        CheckResult(
            name=CHECK_ATTACHMENTS,
            passed=None,
            detail=(
                "a saved run record stores an attachment count, not filenames, "
                "so attachment names cannot be checked without the payload"
            ),
        ),
        _check_routing(readback, expectations, None),
    )
    return Verification(
        record_id=record.get("id", ""),
        tag=str(readback.tag or ""),
        found=True,
        checks=checks,
    )
