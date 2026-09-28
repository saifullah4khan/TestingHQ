"""A configurable intake pipeline to point the verification tools at.

WHY THIS EXISTS. Every test in this repository so far has proved that payloads
arrive and that statuses come back. None of them has proved the thing the whole
pipeline package exists for: that a parser which is quietly wrong gets caught.
The gap is not that the tools are untested, it is that there was no system
under test to catch. This module is one.

It is a real intake pipeline in the sense that matters: it accepts the actual
multipart body the transport builds, decodes it, decides what a ticket should
look like, and can be given the specific defects that real intake systems have.
Each defect is a named keyword argument rather than a mode string, so a test
reads as the claim it is making:

    PipelineUnderTest(drop_attachments=True)
    PipelineUnderTest(duplicate_on_redelivery=True)
    PipelineUnderTest(drop_every_nth=3)

The defaults describe a correct pipeline. That is the important part: a test
that starts from a correct system and breaks exactly one thing proves that one
thing, and a test that starts from a broken one and fixes it proves nothing
about the tool that found it.

THE RECORD MODEL, and why a message and a ticket are separate things. One
delivered message becomes one `MessageRecord`, holding what the pipeline parsed
out of it. The record also carries the id of the ticket it was FILED ON, and
those are not the same thing once threading is involved: a reply that is
correctly threaded onto its parent adds a message record without adding a
ticket. Modelling a ticket as the record collapses those two cases into one,
and then "the reply landed on the wrong ticket" and "the reply was not parsed"
become indistinguishable, which is precisely the distinction
`expectations.check_thread_together` exists to preserve.

WHERE THE LINE BETWEEN CORRECT AND BROKEN IS, and why it is here and not in the
tests. The `verify` checker deliberately normalises aggressively: it casefolds
subjects, folds whitespace, compares bodies by substring, and accepts an
address in either bare or display-name form. Every one of those decisions is a
claim that a given difference is not a parse bug. This module is where those
claims get their counterweight, because a pipeline that is merely untidy must
PASS and a pipeline that is genuinely wrong must FAIL. If the two were not
distinguishable here, the normalisation in `expectations.py` would be untested
preference rather than tested judgement.

It also serves as an `HttpClient` rather than as a `FakeSink`, so the tests go
through `core.transport.post` and the real multipart encoder on the way in. A
verification test that skipped the wire format would be testing a payload the
system under test never saw.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fake_sink import decode_multipart

from testinghq.core.transport import ClientResponse, PreparedRequest
from testinghq.pipeline.readback import FunctionAdapter, Readback

_BOUNDARY_RE = re.compile(r"boundary=([^\s;]+)")
_SUBJECT_PREFIX_RE = re.compile(r"^(re|fwd|aw|sv)\s*:\s*", re.IGNORECASE)

#: The route a correctly routed message reports: the mailbox it was addressed
#: to, which is also what `Expectations.route_for` expects by default.
UNROUTED = "queue:unrouted"


@dataclass
class MessageRecord:
    """One delivered message, as this pipeline parsed it, plus the ticket it
    was filed on."""

    message_id: str
    tag: Optional[str]
    from_addr: Optional[str]
    subject: Optional[str]
    body: Optional[str]
    attachment_names: Tuple[str, ...]
    route: Optional[str]
    in_reply_to: Optional[str]
    references: Tuple[str, ...]
    ticket_id: str
    deliveries: int = 1

    def to_readback(self) -> Readback:
        return Readback(
            exists=True,
            ticket_id=self.ticket_id,
            from_addr=self.from_addr,
            subject=self.subject,
            body=self.body,
            attachment_names=self.attachment_names,
            route=self.route,
            message_id=self.message_id,
            in_reply_to=self.in_reply_to,
            references=self.references,
            tag=self.tag,
            fields=(
                "from_addr",
                "subject",
                "body",
                "attachment_names",
                "route",
                "message_id",
                "in_reply_to",
                "references",
            ),
        )


def _bare_message_id(value: Optional[str]) -> str:
    """A Message-ID with its angle brackets stripped, the one form used for
    identity. Deduplicating on the raw header value instead would miss every
    redelivery, because the header is stored with the brackets and the lookup
    strips them."""
    return (value or "").strip().strip("<>").strip()


def _header(headers_text: str, name: str) -> Optional[str]:
    for line in (headers_text or "").split("\r\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == name.lower():
            return value.strip()
    return None


def _headers_of(fields: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in (fields.get("headers") or "").split("\r\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def _tag_of(fields: Dict[str, Any]) -> Optional[str]:
    """Recover the TestingHQ tag from wherever the payload carries it.

    Scans the header blob, then the text, then the HTML, in that order, which
    mirrors the documented search priority. A real pipeline would not know
    about the tag at all; this one knows it only because a real pipeline also
    has to be able to correlate its own records, and pretending otherwise would
    make the adapter untestable.
    """
    from testinghq.pipeline.messages import harvest_tag

    return harvest_tag(fields.get("headers"), fields.get("text"), fields.get("html"))


class PipelineUnderTest:
    """An intake pipeline, correct by default, breakable one defect at a time.

    Every defect keyword below is a bug that real intake systems have. The ones
    that a status-code tool cannot see are the reason this exists: a 200 with
    the attachments gone, a 200 that filed the message in the wrong queue, a 200
    for a message that was never turned into a ticket at all, a 200 for each of
    two copies of the same delivery.
    """

    def __init__(
        self,
        *,
        # Content defects: the 200s that lie about the parse.
        drop_attachments: bool = False,
        truncate_body_at: Optional[int] = None,
        mangle_sender: bool = False,
        mangle_subject: bool = False,
        misroute: bool = False,
        # Loss and duplication defects.
        drop_every_nth: Optional[int] = None,
        drop_message_ids: Sequence[str] = (),
        duplicate_on_redelivery: bool = False,
        # Threading defects.
        drop_threading_headers: bool = False,
        split_thread: bool = False,
        drop_late_parent: bool = False,
        # Machine-mail defects, for `loop`. The defaults are the correct
        # behaviour: a pipeline suppresses machine mail and never answers it.
        ticket_machine_mail: bool = False,
        auto_reply_machine_mail: bool = False,
        answer_loop_bait: bool = False,
        #: Where to append outbound auto-replies, as JSON Lines. None means the
        #: pipeline emits nothing observable, which is the same as no defect and
        #: is what a correct pipeline does.
        outbound_sink: Optional[str] = None,
        # Shape the response, not the record.
        status: int = 200,
    ) -> None:
        self.drop_attachments = drop_attachments
        self.truncate_body_at = truncate_body_at
        self.mangle_sender = mangle_sender
        self.mangle_subject = mangle_subject
        self.misroute = misroute
        self.drop_every_nth = drop_every_nth
        self.drop_message_ids = frozenset(drop_message_ids)
        self.duplicate_on_redelivery = duplicate_on_redelivery
        self.drop_threading_headers = drop_threading_headers
        self.split_thread = split_thread
        self.drop_late_parent = drop_late_parent
        self.ticket_machine_mail = ticket_machine_mail
        self.auto_reply_machine_mail = auto_reply_machine_mail
        self.answer_loop_bait = answer_loop_bait
        self.outbound_sink = outbound_sink
        self.status = status

        self.messages: List[MessageRecord] = []
        self.received: List[Dict[str, Any]] = []
        #: Every outbound message this pipeline emitted, in order. The sink file
        #: is the same list written out, so a test can assert on either.
        self.outbound: List[Dict[str, Any]] = []
        self._by_message_id: Dict[str, MessageRecord] = {}
        #: Message-IDs whose parent had not arrived when they did. The
        #: out-of-order case: a reply that lands first is filed provisionally
        #: and re-filed onto its parent when the parent finally shows up.
        self._orphaned: List[MessageRecord] = []
        self._delivery_count = 0
        self._next_ticket = 1

    # -- the wire side ----------------------------------------------------

    def send(self, request: PreparedRequest) -> ClientResponse:
        """The `HttpClient` seam. Takes the real prepared request, decodes the
        real multipart body, and answers."""
        content_type = request.headers.get("Content-Type", "")
        boundary = _BOUNDARY_RE.search(content_type)
        if boundary is None:
            raise ValueError(f"no multipart boundary in {content_type!r}")
        fields = decode_multipart(request.body, content_type)
        self._receive(fields)
        payload = json.dumps(
            {"ok": self.status < 400, "messages": len(self.messages)}
        ).encode("utf-8")
        return ClientResponse(status=self.status, body=payload)

    def _receive(self, fields: Dict[str, Any]) -> Optional[MessageRecord]:
        self._delivery_count += 1
        self.received.append(fields)

        if self.status >= 400:
            return None

        if self.drop_every_nth and self._delivery_count % self.drop_every_nth == 0:
            return None

        headers = _headers_of(fields)
        message_id = _bare_message_id(headers.get("Message-ID"))
        if message_id and message_id in self.drop_message_ids:
            return None

        if self._act_on_machine_mail(fields):
            return None

        if message_id and message_id in self._by_message_id:
            existing = self._by_message_id[message_id]
            existing.deliveries += 1
            if self.duplicate_on_redelivery:
                # The bug: a redelivery is treated as a new message, so the
                # customer gets two tickets and neither is obviously wrong.
                return self._create(fields, headers, message_id=message_id)
            return existing

        return self._create(fields, headers, message_id=message_id)

    def _next_ticket_id(self) -> str:
        ticket = f"T{self._next_ticket:05d}"
        self._next_ticket += 1
        return ticket

    def _act_on_machine_mail(self, fields: Dict[str, Any]) -> bool:
        """The machine-mail policy. Returns True to stop here, meaning the
        message was recognised as machine mail and not ticketed.

        Replying and ticketing are two independent defects, so they are decided
        separately. An earlier version returned "not will_reply" here, which made
        a pipeline that auto-replied stop ticketing as well and hid half the bug:
        the ticket check passed and only the reply check fired, so a pipeline
        that both replied and ticketed looked like it only replied.
        """
        from testinghq.pipeline.loop import machine_mail_marker

        headers = _headers_of(fields)
        marker = machine_mail_marker(
            headers, fields.get("subject") or "", fields.get("from") or ""
        )
        if marker is None:
            return False

        is_bait = fields.get("to", "").strip() == fields.get("from", "").strip()
        if self.auto_reply_machine_mail or (self.answer_loop_bait and is_bait):
            self._emit(fields, marker, is_bait)
        return not self.ticket_machine_mail

    def _emit(self, fields: Dict[str, Any], marker: str, is_bait: bool) -> None:
        """Append one outbound auto-reply, to the list and to the sink file."""
        headers = _headers_of(fields)
        line = {
            "id": f"OUT{len(self.outbound) + 1:04d}",
            "tag": _tag_of(fields),
            "to": fields.get("to") or "",
            "from": fields.get("from") or "",
            "subject": f"Re: {fields.get('subject') or ''}",
            "body": "This is an automatic reply.",
            "attachments": [],
            "message_id": f"outbound-{len(self.outbound) + 1}@example.test",
            "auto_submitted": "auto-replied",
            "triggered_by": marker,
            "in_reply_to": headers.get("In-Reply-To", ""),
        }
        self.outbound.append(line)
        if self.outbound_sink:
            with open(self.outbound_sink, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")

    def _create(
        self,
        fields: Dict[str, Any],
        headers: Dict[str, str],
        message_id: str = "",
    ) -> MessageRecord:
        in_reply_to = (
            None
            if self.drop_threading_headers
            else _bare_message_id(headers.get("In-Reply-To"))
        )
        references = (
            ()
            if self.drop_threading_headers
            else tuple(_bare_message_id(r) for r in (headers.get("References") or "").split())
        )

        # Threading: file on the parent's ticket when the parent is known, and
        # provisionally otherwise, re-filing when the parent arrives.
        parent = (
            self._by_message_id.get(in_reply_to)
            if in_reply_to and not self.split_thread
            else None
        )
        record = MessageRecord(
            message_id=message_id or f"anon-{self._next_ticket}",
            tag=_tag_of(fields),
            from_addr=self._sender(fields),
            subject=self._subject(fields),
            body=self._body(fields),
            attachment_names=() if self.drop_attachments else self._attachments(fields),
            route=self._route(fields),
            in_reply_to=in_reply_to or None,
            references=references,
            ticket_id=parent.ticket_id if parent else self._next_ticket_id(),
        )

        self.messages.append(record)
        if record.message_id:
            self._by_message_id.setdefault(record.message_id, record)

        if in_reply_to and parent is None and not self.split_thread:
            # An orphan: its parent has not been seen yet. A correct pipeline
            # holds it provisionally and re-files it when the parent arrives,
            # rather than opening a new conversation for it.
            self._orphaned.append(record)

        self._reconcile_orphans(record)
        return record

    def _reconcile_orphans(self, arrived: MessageRecord) -> None:
        """Re-file any message that was waiting for `arrived` as its parent,
        transitively. This is the whole reply-first scenario: the replies land
        first, provisionally, and when the original finally arrives they are
        moved onto its ticket, together with anything that was waiting on
        them."""
        if self.split_thread:
            return
        moved = True
        while moved:
            moved = False
            for orphan in list(self._orphaned):
                parent = self._by_message_id.get(orphan.in_reply_to or "")
                if parent is None or parent.ticket_id == orphan.ticket_id:
                    continue
                orphan.ticket_id = parent.ticket_id
                self._orphaned.remove(orphan)
                moved = True

    # -- the parse, defect by defect -------------------------------------

    def _sender(self, fields: Dict[str, Any]) -> Optional[str]:
        raw = fields.get("from") or ""
        if not self.mangle_sender or not raw:
            return raw
        # The classic parse bug: keep the display name, lose or truncate the
        # addr-spec, so the address a ticket is filed under is not the one the
        # message came from.
        return raw.split("<", 1)[0].strip() or "unknown"

    def _subject(self, fields: Dict[str, Any]) -> Optional[str]:
        raw = fields.get("subject") or ""
        if not self.mangle_subject or not raw:
            return raw
        return _SUBJECT_PREFIX_RE.sub("", raw)[:8] or "(none)"

    def _body(self, fields: Dict[str, Any]) -> Optional[str]:
        raw = fields.get("text") or ""
        if self.truncate_body_at is not None:
            return raw[: self.truncate_body_at]
        return raw

    def _attachments(self, fields: Dict[str, Any]) -> Tuple[str, ...]:
        names = []
        index = 1
        while f"attachment{index}" in fields:
            part = fields[f"attachment{index}"]
            names.append(getattr(part, "filename", ""))
            index += 1
        return tuple(names)

    def _route(self, fields: Dict[str, Any]) -> Optional[str]:
        try:
            envelope = json.loads(fields.get("envelope") or "{}")
        except json.JSONDecodeError:
            return None
        recipients = envelope.get("to") or []
        destination = recipients[0] if recipients else "nowhere"
        return UNROUTED if self.misroute else destination

    # -- the read side ----------------------------------------------------

    def fetch(self, probe) -> List[Readback]:
        """The readback side. Indexed per call, never snapshotted: an adapter
        that indexed once reported a pipeline that had produced nothing, because
        it is built before the first message is sent."""
        index: Dict[str, List[MessageRecord]] = {}
        for record in self.messages:
            if record.tag:
                index.setdefault(record.tag, []).append(record)
        return [r.to_readback() for r in index.get(probe.tag, [])]

    def adapter(self, *, can_enumerate: bool = True):
        """This pipeline as a `ReadbackAdapter`.

        `can_enumerate=False` produces an adapter that can only look records up
        by tag, which is the honest shape of most real integrations and the one
        that makes a "strays not searched" report reachable. Both shapes are
        built from the same `fetch`, so the "could not search" path is exercised
        by the same code as the "searched" path.
        """
        if not can_enumerate:
            return FunctionAdapter(self.fetch)
        return _EnumerableAdapter(self)


    def list_all(self) -> List[Readback]:
        return [m.to_readback() for m in self.messages]

    def close(self) -> None:
        """No resources to release. Present because this object is handed to the
        tools as a readback adapter as well as a client, and `close_adapter`
        calls whatever close the object has."""
        return None

    def records_for(self, tag: str) -> List[MessageRecord]:
        return [m for m in self.messages if m.tag == tag]

    @property
    def tickets(self) -> List[MessageRecord]:
        """Alias for `messages`, kept because a test reading "the pipeline made
        N tickets" means the record count and reaches for the obvious name."""
        return self.messages


class _EnumerableAdapter:
    """A readback adapter that can both look up and enumerate.

    A small named class rather than a lambda wrapper because `list_all` has to
    exist on the object for `readback.can_enumerate` to see it, and attaching a
    function to a `FunctionAdapter` after construction would make the ability to
    enumerate look incidental when it is a deliberate capability.
    """

    def __init__(self, pipeline: "PipelineUnderTest") -> None:
        self._pipeline = pipeline
        self.closed = False

    def fetch(self, probe) -> List[Readback]:
        return self._pipeline.fetch(probe)

    def list_all(self) -> List[Readback]:
        return self._pipeline.list_all()

    def close(self) -> None:
        self.closed = True
