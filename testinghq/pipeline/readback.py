"""The readback seam: how a tool asks a system what it actually produced.

Blast and Barrage both judge a run by the HTTP status. A 200 means the endpoint
accepted the POST, which is the right question for "did it crash" and the wrong
question for everything an intake pipeline gets wrong. This module is the seam
that closes the gap: after a run, an adapter reads the system's own output.

A `ReadbackAdapter` has `fetch(probe) -> Sequence[Readback]`, and the sequence is
the point. Zero is a loss, one is normal, several is a duplicated message, which
is a real bug and not an error condition. `list_all` is optional, because not
every system can be enumerated, and the tools say "not searched" rather than
reporting none found when it is absent. A plain function taking a Probe is a
complete adapter, which is what makes writing one for a one-off system a lambda
rather than a project.

`Readback` is deliberately a flat bag of the generic facts every intake system has
some record of (a ticket id, a sender, a subject, a body, attachment names, a
route) plus the three RFC 5322 threading headers, and nothing else. Nothing here
knows what a ticket is in any particular product, which is what keeps the
checking layer business-free.

THE `fields` CONTRACT, which is not decoration. An adapter that cannot see a
field must be able to say so, and every check consults that before deciding
anything. A checker that treats an absent value as a match is a checker that
manufactures green, and a green result nobody earned is worse than no result.
`fields` names what the readback carried; when it is empty the set is inferred
from which values are present, which is right for a hand-built readback and wrong
for a partial API response, so the built-in adapters always set it.

The normalization rules below each say which difference they excuse. Every one
has a test for the case it exists for AND the case it must not excuse,
because a lenient comparison tested only against the lenient case accepts
anything."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple
from typing import runtime_checkable

#: Every field a `Readback` can carry. The list is closed so an adapter's
#: `fields` entry can be checked against it rather than trusted.
READBACK_FIELDS: Tuple[str, ...] = (
    "ticket_id",
    "from_addr",
    "subject",
    "body",
    "attachment_names",
    "route",
    "message_id",
    "in_reply_to",
    "references",
    "tag",
)

#: The subset a check may ask about. `ticket_id` is handled by its own check
#: and is not optional there, so it is deliberately absent from this list.
CHECKABLE_FIELDS: Tuple[str, ...] = (
    "from_addr",
    "subject",
    "body",
    "attachment_names",
    "route",
    "in_reply_to",
    "references",
)

_WHITESPACE = re.compile(r"\s+")


class ReadbackError(RuntimeError):
    """Raised when a system's output cannot be read, or is not shaped like a
    readback. Fails loud: a verification run that silently saw nothing is worse
    than one that refused to run."""


# ---------------------------------------------------------------------------
# Probes: what to look for
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """Everything an adapter might need to find one message's outcome.

    Deliberately carries no timestamps. A probe is built from the payload Blast
    generated plus the run's own deterministic identity, never from the clock,
    so a verification run is reproducible in the same way a corpus is and a
    probe built today identifies the same thing it identified yesterday.
    """

    record_id: str
    tag: str
    payload_sha256: str
    from_addr: str
    subject: str
    recipient: str
    message_id: str
    attachment_names: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Readbacks: what was found
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readback:
    """One record the system under test holds, as far as the adapter can see.

    `exists` is False for a record the system knows about but considers gone; a
    lookup that found nothing should return an empty sequence from `fetch`
    rather than a Readback with exists=False, so the two situations stay
    distinguishable in a report.

    `fields` names which of `READBACK_FIELDS` this readback actually carried.
    Empty means "infer from the values present", which is convenient for a
    hand-built readback and wrong for a partial API response, so the built-in
    adapters set it explicitly.
    """

    exists: bool
    ticket_id: Optional[str] = None
    from_addr: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    attachment_names: Tuple[str, ...] = field(default_factory=tuple)
    route: Optional[str] = None
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Tuple[str, ...] = field(default_factory=tuple)
    tag: Optional[str] = None
    fields: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        unknown = [name for name in self.fields if name not in READBACK_FIELDS]
        if unknown:
            raise ReadbackError(
                f"readback names fields that do not exist: {unknown}; "
                f"known fields are {list(READBACK_FIELDS)}"
            )
        if self.exists and not self.ticket_id:
            raise ReadbackError(
                "a readback that reports exists=True must carry a ticket_id. "
                "An adapter that cannot name what it found cannot say whether "
                "there was one copy or several, which is the whole question "
                "this tool exists to ask."
            )

    def value_of(self, name: str) -> Any:
        return getattr(self, name)

    def has(self, name: str) -> bool:
        """True if this readback actually carried `name`.

        The distinction this method exists to preserve: "the system recorded the
        wrong subject" and "the adapter could not see the subject" are different
        findings, with opposite remedies, and collapsing them makes a tool that
        cannot see a field look like one that found it correct.
        """
        if self.fields:
            return name in self.fields
        if name == "attachment_names" or name == "references":
            # An empty tuple is a legitimate observed value for these two: a
            # message with no attachments genuinely has none. Inferring "not
            # carried" from emptiness would make every attachment-less payload
            # report as unchecked, which is its own kind of lie.
            return True
        return getattr(self, name) is not None

    def to_json(self) -> Dict[str, Any]:
        """A JSON-ready view for the run artifact. `fields` is included
        because a reader of the artifact has to be able to tell the difference
        between a field that was checked and one that was never visible."""
        payload: Dict[str, Any] = {"exists": self.exists}
        for name in READBACK_FIELDS:
            value = getattr(self, name)
            if isinstance(value, tuple):
                value = list(value)
            payload[name] = value
        payload["fields"] = sorted(self.fields) if self.fields else sorted(
            name for name in CHECKABLE_FIELDS if self.has(name)
        )
        return payload


# ---------------------------------------------------------------------------
# The adapter contract
# ---------------------------------------------------------------------------


@runtime_checkable
class ReadbackAdapter(Protocol):
    """What every adapter must provide.

    `fetch` returns a sequence because "how many" is a first-class answer, not
    an error condition: a webhook provider that delivers twice and an intake
    pipeline that creates two tickets both look exactly like this returning two
    items, and both are what the ledger and redelivery tools are built to find.

    `list_all` is optional. Adapters that cannot enumerate should not define it;
    callers check with `getattr` and the tools then report stray-record
    hunting as unavailable rather than as having found nothing.
    """

    def fetch(self, probe: Probe) -> Sequence[Readback]: ...

    def close(self) -> None: ...


class FunctionAdapter:
    """Adapts a plain callable to the adapter contract.

    Exists so that writing an adapter for a one-off system is a lambda rather
    than a class, and so the tests in this repository can stand up a system
    under test in a few lines. A function may return a single Readback or a
    sequence of them; `None` means not found.
    """

    def __init__(self, fetch_fn: Callable[[Probe], Any]) -> None:
        if not callable(fetch_fn):
            raise ReadbackError(
                f"a readback adapter must be callable, got {type(fetch_fn).__name__}"
            )
        self._fetch_fn = fetch_fn
        self.closed = False

    def fetch(self, probe: Probe) -> List[Readback]:
        result = self._fetch_fn(probe)
        if result is None:
            return []
        if isinstance(result, Readback):
            return [result]
        if isinstance(result, (list, tuple)):
            for item in result:
                if not isinstance(item, Readback):
                    raise ReadbackError(
                        f"a readback adapter returned {type(item).__name__} "
                        "where a Readback was expected"
                    )
            return list(result)
        raise ReadbackError(
            f"a readback adapter must return a Readback, a sequence of them, "
            f"or None; got {type(result).__name__}"
        )

    def close(self) -> None:
        self.closed = True


class MultiAdapter:
    """Queries several adapters and concatenates what they found.

    For a system that is genuinely more than one thing: the ticket store says
    what was created, an outbound mail sink says what was actually delivered
    onward. A duplicate reported by the second is real, not double-counted by
    this class, because a duplicate is several records within ONE adapter's
    answer, not one record from each.

    `list_all` is defined only when every delegate can enumerate, since a
    partial enumeration reported as complete is how strays go missing.
    """

    def __init__(self, adapters: Sequence[ReadbackAdapter]) -> None:
        self._adapters = list(adapters)
        # An attribute rather than a method, so `can_enumerate` can ask without
        # building a MultiAdapter and throwing it away. A MultiAdapter that
        # always claimed to be able to enumerate would let a partial search be
        # reported as complete, which is how stray records go missing.
        self.can_enumerate = all(
            callable(getattr(adapter, "list_all", None)) for adapter in self._adapters
        )

    def fetch(self, probe: Probe) -> List[Readback]:
        found: List[Readback] = []
        for adapter in self._adapters:
            found.extend(adapter.fetch(probe))
        return found

    def list_all(self) -> List[Readback]:
        if not self._adapters:
            return []
        results = []
        for adapter in self._adapters:
            lister = getattr(adapter, "list_all", None)
            if lister is None:
                raise ReadbackError(
                    "MultiAdapter.list_all called but a delegate adapter cannot "
                    "enumerate its records; partial enumeration reported as "
                    "complete would hide stray records"
                )
            results.extend(lister())
        return results

    def close(self) -> None:
        for adapter in self._adapters:
            adapter.close()


def can_enumerate(adapter: ReadbackAdapter) -> bool:
    """True if `adapter` can list every record it holds, not just look one up.

    A declared `can_enumerate` attribute wins over duck-typing on `list_all`,
    because a composite adapter such as `MultiAdapter` has a `list_all` method
    that is only correct when all of its delegates can enumerate. A method's
    mere existence would have answered yes to a partial search.
    """
    declared = getattr(adapter, "can_enumerate", None)
    if isinstance(declared, bool):
        return declared
    return callable(getattr(adapter, "list_all", None))


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
#
# A verification check that compares raw strings will fail on differences nobody
# considers a parse bug and pass on differences everybody does. The rules below
# are the ones chosen, and each is a decision rather than a default.


def normalize_address(value: Any) -> str:
    """Reduce an address-bearing value to a bare, lowercase addr-spec.

    Accepts what a real system stores: a bare address, an RFC 5322 display-name
    form, a header with several recipients. Returns "" when nothing address-like
    can be extracted.

    `email.utils.getaddresses`, never `parseaddr`, for the same reason the
    synthetic-content guardrail uses getaddresses: parseaddr returns at most one
    address, so a multi-recipient header would compare equal to just its first
    recipient and a real routing bug would read as a pass.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        # Never coerce a non-string. A value this guard cannot read is a value
        # it cannot vouch for.
        return ""
    addresses = [address for _name, address in getaddresses([value]) if address]
    if not addresses:
        return ""
    return addresses[0].strip().strip("<>").lower()


def normalize_text(value: Any) -> str:
    """Collapse a text value to a comparable form: NFKC, then whitespace runs
    folded to single spaces, then stripped and casefolded.

    NFKC because an intake system that extracts the HTML part will hand back a
    different but equivalent form (non-breaking spaces, ligatures, full-width
    punctuation) for a message that parsed correctly, and reporting that as a
    body mismatch teaches people to ignore body mismatches.

    Whitespace folded because a real mail path reflows. A body that was
    truncated or garbled still differs after folding, so folding costs the
    check nothing real.

    Casefolded because subject and body case is not what anyone is debugging.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()


def normalize_route(value: Any) -> str:
    """Reduce a routing value to a comparable form: trimmed, whitespace folded,
    lowercased. No NFKC here, unlike the text rules, because a queue or mailbox
    name is an identifier rather than prose and folding its compatibility
    characters could make two genuinely different destinations compare equal."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return _WHITESPACE.sub(" ", value).strip().lower()


def normalize_attachment_names(values: Any) -> Tuple[str, ...]:
    """Reduce attachment names to a sorted, lowercased tuple.

    Sorted because order is not a property any intake system preserves, and
    compared as a set because a pipeline that reversed two attachments has not
    lost anything. Lowercased for the same reason as everything else here.
    Bare filenames, no directory component: a system that stored the full path
    still stored the name.
    """
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ReadbackError(
            f"attachment names must be a sequence of strings, got "
            f"{type(values).__name__}"
        )
    names = set()
    for value in values:
        if not isinstance(value, str):
            raise ReadbackError(
                f"attachment names must be strings, got {type(value).__name__}"
            )
        cleaned = value.replace("\\", "/").rsplit("/", 1)[-1]
        if cleaned:
            names.add(cleaned.strip().casefold())
    return tuple(sorted(names))


def normalize_message_ids(values: Any) -> Tuple[str, ...]:
    """Reduce one Message-ID or a References chain to a sorted tuple of bare
    tokens, so a chain that arrived in a different order, with the angle
    brackets stripped, or as a single space-separated string instead of a list,
    still compares equal to the chain that was sent.

    A string containing whitespace is treated as a chain, not as one id. That is
    the case worth getting right: RFC 5322 says `References` is a space-
    separated list, so a system that stores the header verbatim hands back one
    string, and stripping only the outer brackets would have produced the
    nonsense token `a@b> <c@d` that compares equal to nothing at all.
    """
    if values is None:
        return ()
    if isinstance(values, str):
        values = values.split()
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ReadbackError(
            f"message ids must be a string or a sequence of strings, got "
            f"{type(values).__name__}"
        )
    tokens = set()
    for value in values:
        if not isinstance(value, str):
            raise ReadbackError(
                f"message ids must be strings, got {type(value).__name__}"
            )
        for part in value.split():
            cleaned = part.strip().strip("<>").strip()
            if cleaned:
                tokens.add(cleaned)
    return tuple(sorted(tokens))

