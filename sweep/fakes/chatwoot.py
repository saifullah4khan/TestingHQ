"""A stand-in for Chatwoot, for running sweep/configs/chatwoot.toml without one.

It takes inbound mail the way a stock Chatwoot (v4.18) does and answers the
readback the recipe makes with the JSON Chatwoot's own views render, so a full
verify round trip runs against it. Standard library only.

Inbound, as Action Mailbox's SendGrid ingress and Chatwoot's mailboxes do it:

- `POST /rails/action_mailbox/sendgrid/inbound_emails` needs HTTP Basic auth,
  user `actionmailbox` and the ingress password: 401 without it, 422 without
  an `email` field or with an `envelope` that is not JSON, 204 when accepted.
- Every envelope recipient is prepended to the raw message as an
  `X-Original-To:` line, and an exact copy of a message already taken in
  (same Message-ID, same bytes) is dropped, as Action Mailbox's unique index
  drops it.
- The message is routed to the email inbox whose address or forwarding
  address appears in To, Cc or X-Original-To; anything else goes to the
  catch-all mailbox, which keeps nothing.
- It joins the conversation of a message named by In-Reply-To or References,
  or the conversation opened for the same In-Reply-To, or opens a new one,
  and stores the text body as the message content. A Message-ID already in
  that conversation is not stored twice.
- The automation rule the setup script creates (conversation created in the
  inbox, add a label) runs on each new conversation that is not an
  auto-reply, and leaves the activity message Chatwoot writes for it.

Readback, with the `api_access_token` header (401 without it):

- `GET /api/v1/accounts/1/conversations?q=...`: the
  conversations with an incoming or outgoing message whose content contains
  `q`, as `{"data": {"meta": ..., "payload": [...]}}`. Without `q`, filtered
  by `status` (default `open`, `all` for every one), which is how the recipe
  enumerates. 25 to a page, `page` from 1.
- `POST /api/v1/accounts/1/conversations/filter`, by `labels`, `inbox_id`,
  `status` or `mail_subject`, as `{"meta": ..., "payload": [...]}`.
- `GET /api/v1/accounts/1/conversations/search?q=...`, in the slimmer search
  shape.
- `GET /api/v1/accounts/1/search/messages?q=...` (the recipe's lookup and
  listing): every incoming or outgoing message whose content contains `q`,
  newest first, as `{"payload": {"messages": [...]}}`, 15 to a page.
- `GET /api` and `GET /health`, for whatever waits on the instance.

Each conversation is rendered as api/v1/conversations/partials/_conversation
renders it: `meta.sender` (the contact), `messages` (the latest message only,
which after the automation rule is its activity message), `additional_attributes.mail_subject`,
`inbox_id`, `labels`, `last_non_activity_message` and the rest.

Run it on its own:

    python sweep/fakes/chatwoot.py --port 3100

or in a test, as a context manager:

    with FakeChatwoot() as fake:
        fake.port, fake.url, fake.conversations
"""
from __future__ import annotations

import argparse
import base64
import email
import email.policy
import hashlib
import html
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlsplit

INGRESS_PATH = "/rails/action_mailbox/sendgrid/inbound_emails"
INGRESS_USER = "actionmailbox"

#: The values the compose file and setup script use, so the fake and a fresh
#: sweep stack agree without any configuration.
DEFAULT_PASSWORD = "thq-sweep-inbound"
DEFAULT_TOKEN = "thq-fake-api-token"
DEFAULT_INBOX_EMAIL = "support@chatwoot.test"
DEFAULT_FORWARD_TO = "desk@inbound.chatwoot.test"
DEFAULT_LABEL = "testinghq"
ACCOUNT_ID = 1
INBOX_ID = 1
PAGE_SIZE = 25
#: Message search pages by 15 (SearchService#filter_messages).
SEARCH_PAGE_SIZE = 15
VERSION = "4.18.0"

MESSAGE_INCOMING = 0
MESSAGE_OUTGOING = 1
MESSAGE_ACTIVITY = 2

_EMAIL_REGEXP = re.compile(r"\A[^@\s]+@[^@\s]+\Z")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Contact:
    id: int
    name: str
    email: str
    created_at: int


@dataclass
class Message:
    id: int
    content: str
    message_type: int
    content_type: str
    created_at: int
    conversation_display_id: int
    source_id: Optional[str] = None
    sender: Optional[Contact] = None
    content_attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    id: int
    display_id: int
    inbox_id: int
    contact: Contact
    additional_attributes: Dict[str, Any]
    created_at: int
    uuid: str
    labels: List[str] = field(default_factory=list)
    messages: List[Message] = field(default_factory=list)
    status: str = "open"


@dataclass
class Inbound:
    """One POST at the ingress, as the fake saw it."""

    status: int
    authorized: bool
    routed: bool = False


class _State:
    """Everything the fake holds, behind one lock: the handler threads of a
    ThreadingHTTPServer share it."""

    def __init__(self, inbox_email: str, forward_to: str, label: str) -> None:
        self.lock = threading.Lock()
        self.inbox_email = inbox_email.lower()
        self.forward_to = forward_to.lower()
        self.label = label
        self.contacts: Dict[str, Contact] = {}
        self.conversations: List[Conversation] = []
        self.inbound: List[Inbound] = []
        self.dropped: List[str] = []
        self.raw_seen: set = set()
        self._next = {"contact": 1, "conversation": 1, "message": 1}

    def next_id(self, kind: str) -> int:
        value = self._next[kind]
        self._next[kind] = value + 1
        return value


# ---------------------------------------------------------------------------
# Inbound: Action Mailbox's SendGrid ingress, then Chatwoot's mailboxes
# ---------------------------------------------------------------------------


def parse_form_data(content_type: str, body: bytes) -> Dict[str, bytes]:
    """multipart/form-data fields as name -> bytes, the first of each name."""
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    parsed = email.message_from_bytes(head + body, policy=email.policy.compat32)
    fields: Dict[str, bytes] = {}
    if not parsed.is_multipart():
        return fields
    for part in parsed.get_payload():
        name = part.get_param("name", header="content-disposition")
        if not name or name in fields:
            continue
        payload = part.get_payload(decode=True)
        fields[name] = payload if payload is not None else b""
    return fields


def _normalize(address: str) -> str:
    """Chatwoot's plus-addressing normalisation: `a+b@x` finds `a@x`."""
    local, _, domain = address.strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}" if domain else local


def _addresses(message: EmailMessage, name: str) -> List[str]:
    values = message.get_all(name) or []
    return [addr for _n, addr in getaddresses([str(v) for v in values]) if addr]


def _message_ids(value: Optional[str]) -> List[str]:
    return re.findall(r"<([^<>\s]+)>", value or "") or (
        [value.strip()] if value and value.strip() else []
    )


def _text_content(message: EmailMessage) -> str:
    """What Chatwoot stores as the message content: the decoded text part, or
    the HTML part's text when there is no text part."""
    part = message.get_body(preferencelist=("plain",))
    if part is not None:
        try:
            return part.get_content()
        except (LookupError, UnicodeError):
            return part.get_payload(decode=True).decode("utf-8", "replace")
    part = message.get_body(preferencelist=("html",))
    if part is not None:
        try:
            markup = part.get_content()
        except (LookupError, UnicodeError):
            markup = part.get_payload(decode=True).decode("utf-8", "replace")
        return html.unescape(_TAG_RE.sub("", markup)).strip()
    return ""


def _auto_reply(message: EmailMessage) -> bool:
    submitted = message.get("Auto-Submitted")
    if submitted is not None and str(submitted).strip().lower() != "no":
        return True
    return str(message.get("X-Autoreply", "")).strip().lower() == "yes"


def receive(state: _State, raw: bytes) -> bool:
    """Route one raw message (already carrying its X-Original-To lines) the
    way ApplicationMailbox and ReplyMailbox do. True when it was stored."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    recipients = (
        _addresses(message, "To") + _addresses(message, "Cc")
        + _addresses(message, "X-Original-To")
    )
    wanted = {state.inbox_email, state.forward_to}
    if not any(_normalize(addr) in wanted for addr in recipients):
        state.dropped.append(str(message.get("Message-ID", "")))
        return False

    sender_header = message.get("Reply-To") or message.get("From") or ""
    sender_name, sender = parseaddr(str(sender_header))
    sender = sender.lower()
    if not _EMAIL_REGEXP.match(sender):
        state.dropped.append(str(message.get("Message-ID", "")))
        return False

    message_id = (_message_ids(str(message.get("Message-ID", ""))) or [""])[0]
    in_reply_to = (_message_ids(str(message.get("In-Reply-To", ""))) or [None])[0]
    references = _message_ids(str(message.get("References", "")))
    subject = str(message.get("Subject", ""))
    now = int(time.time())

    conversation = _find_conversation(state, in_reply_to, references)
    created = conversation is None
    if conversation is None:
        contact = state.contacts.get(sender)
        if contact is None:
            contact = Contact(
                id=state.next_id("contact"),
                name=sender_name or sender.split("@", 1)[0],
                email=sender,
                created_at=now,
            )
            state.contacts[sender] = contact
        display_id = state.next_id("conversation")
        conversation = Conversation(
            id=display_id,
            display_id=display_id,
            inbox_id=INBOX_ID,
            contact=contact,
            additional_attributes={
                "in_reply_to": in_reply_to,
                "source": "email",
                "auto_reply": _auto_reply(message),
                "mail_subject": subject,
                "initiated_at": {"timestamp": time.strftime(
                    "%a %b %d %Y %H:%M:%S GMT+0000", time.gmtime(now))},
            },
            created_at=now,
            uuid=str(uuid.uuid4()),
        )
        state.conversations.append(conversation)

    if message_id and any(m.source_id == message_id for m in conversation.messages):
        return True
    content = _text_content(message)
    conversation.messages.append(Message(
        id=state.next_id("message"),
        content=content,
        message_type=MESSAGE_INCOMING,
        content_type="incoming_email",
        created_at=now,
        conversation_display_id=conversation.display_id,
        source_id=message_id or None,
        sender=conversation.contact,
        content_attributes={
            "email": {
                "bcc": None,
                "cc": _addresses(message, "Cc") or None,
                "content_type": message.get_content_type(),
                "date": str(message.get("Date", "")),
                "from": [sender],
                "html_content": {},
                "in_reply_to": in_reply_to,
                "message_id": message_id,
                "multipart": message.is_multipart(),
                "number_of_attachments": sum(1 for _ in message.iter_attachments()),
                "references": references,
                "subject": subject,
                "text_content": {"full": content, "reply": content, "quoted": content},
                "to": _addresses(message, "To"),
                "auto_reply": _auto_reply(message),
            },
            "cc_email": None,
            "bcc_email": None,
        },
    ))
    if created and not conversation.additional_attributes["auto_reply"]:
        _run_label_rule(state, conversation, now)
    return True


def _find_conversation(
    state: _State, in_reply_to: Optional[str], references: List[str]
) -> Optional[Conversation]:
    """InReplyToStrategy, ReferencesStrategy, then NewConversationStrategy's
    lookup by the conversation's own in_reply_to."""
    wanted = [i for i in [in_reply_to, *references] if i]
    for conversation in state.conversations:
        if any(m.source_id in wanted for m in conversation.messages if m.source_id):
            return conversation
    if in_reply_to:
        for conversation in state.conversations:
            if conversation.additional_attributes.get("in_reply_to") == in_reply_to:
                return conversation
    return None


def _run_label_rule(state: _State, conversation: Conversation, now: int) -> None:
    """The automation rule the setup script creates: on a conversation
    created in the inbox, add the label. Chatwoot records the change as an
    activity message, which becomes the conversation's latest message."""
    if state.label in conversation.labels:
        return
    conversation.labels.append(state.label)
    conversation.messages.append(Message(
        id=state.next_id("message"),
        content=f"Automation System added {state.label}",
        message_type=MESSAGE_ACTIVITY,
        content_type="text",
        created_at=now,
        conversation_display_id=conversation.display_id,
    ))


# ---------------------------------------------------------------------------
# Rendering, as Chatwoot's jbuilder views render it
# ---------------------------------------------------------------------------


def _contact_json(contact: Contact) -> Dict[str, Any]:
    return {
        "additional_attributes": {"source_id": "email"},
        "availability_status": "offline",
        "email": contact.email,
        "id": contact.id,
        "name": contact.name,
        "phone_number": None,
        "blocked": False,
        "identifier": None,
        "thumbnail": "",
        "custom_attributes": {},
        "last_activity_at": contact.created_at,
        "created_at": contact.created_at,
    }


def _message_json(message: Message, conversation: Conversation) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "id": message.id,
        "content": message.content,
        "account_id": ACCOUNT_ID,
        "inbox_id": conversation.inbox_id,
        "conversation_id": conversation.display_id,
        "message_type": message.message_type,
        "created_at": message.created_at,
        "updated_at": message.created_at,
        "private": False,
        "status": "sent",
        "source_id": message.source_id,
        "content_type": message.content_type,
        "content_attributes": message.content_attributes,
        "sender_type": "Contact" if message.sender else None,
        "sender_id": message.sender.id if message.sender else None,
        "external_source_ids": {},
        "additional_attributes": {},
        "processed_message_content": message.content,
        "sentiment": {},
        "conversation": {
            "assignee_id": None,
            "unread_count": _unread(conversation),
            "last_activity_at": _last_activity(conversation),
            "contact_inbox": {"source_id": conversation.contact.email},
        },
    }
    if message.sender is not None:
        data["sender"] = {
            "additional_attributes": {},
            "custom_attributes": {},
            "email": message.sender.email,
            "id": message.sender.id,
            "identifier": None,
            "name": message.sender.name,
            "phone_number": None,
            "thumbnail": "",
            "blocked": False,
            "type": "contact",
        }
    return data


def _search_message_json(message: Message, conversation: Conversation) -> Dict[str, Any]:
    """One message as GET /search/messages renders it (SearchService
    filter_messages): the message's own fields and its sender, which for an
    incoming email is the conversation's contact, whoever wrote it."""
    full = _message_json(message, conversation)
    keys = ("id", "content", "inbox_id", "conversation_id", "message_type", "created_at",
            "private", "status", "source_id", "content_type", "content_attributes", "sender")
    return {key: full[key] for key in keys if key in full}


def _unread(conversation: Conversation) -> int:
    return sum(1 for m in conversation.messages if m.message_type == MESSAGE_INCOMING)


def _last_activity(conversation: Conversation) -> int:
    return max([m.created_at for m in conversation.messages] or [conversation.created_at])


def conversation_json(conversation: Conversation) -> Dict[str, Any]:
    """One conversation as api/v1/conversations/partials/_conversation."""
    latest = max(conversation.messages, key=lambda m: (m.created_at, m.id), default=None)
    non_activity = [m for m in conversation.messages if m.message_type != MESSAGE_ACTIVITY]
    last_non_activity = max(non_activity, key=lambda m: (m.created_at, m.id), default=None)
    first = conversation.messages[0].created_at if conversation.messages else conversation.created_at
    return {
        "meta": {
            "sender": _contact_json(conversation.contact),
            "channel": "Channel::Email",
            "hmac_verified": False,
        },
        "id": conversation.display_id,
        "messages": [_message_json(latest, conversation)] if latest else [],
        "account_id": ACCOUNT_ID,
        "uuid": conversation.uuid,
        "additional_attributes": conversation.additional_attributes,
        "agent_last_seen_at": 0,
        "assignee_last_seen_at": 0,
        "can_reply": True,
        "contact_info_request": {"available": False, "reason": None, "delivery_mode": None},
        "contact_last_seen_at": 0,
        "custom_attributes": {},
        "inbox_id": conversation.inbox_id,
        "labels": list(conversation.labels),
        "muted": False,
        "snoozed_until": None,
        "status": conversation.status,
        "created_at": conversation.created_at,
        "updated_at": float(_last_activity(conversation)),
        "timestamp": _last_activity(conversation),
        "first_reply_created_at": 0,
        "unread_count": _unread(conversation),
        "last_non_activity_message": (
            _message_json(last_non_activity, conversation) if last_non_activity else None
        ),
        "last_activity_at": _last_activity(conversation),
        "priority": None,
        "waiting_since": first,
        "sla_policy_id": None,
    }


def search_json(conversation: Conversation, query: str) -> Dict[str, Any]:
    """One conversation as api/v1/models/_conversation (the search view),
    whose messages are the ones the search matched."""
    return {
        "id": conversation.display_id,
        "uuid": conversation.uuid,
        "created_at": conversation.created_at,
        "contact": {"id": conversation.contact.id, "name": conversation.contact.name},
        "inbox": {"id": conversation.inbox_id, "name": "TestingHQ Sweep",
                  "channel_type": "Channel::Email"},
        "messages": [
            {
                "content": m.content,
                "id": m.id,
                "sender_name": m.sender.name if m.sender else None,
                "message_type": m.message_type,
                "created_at": m.created_at,
            }
            for m in conversation.messages if _matches(m, query)
        ],
        "account_id": ACCOUNT_ID,
    }


def _matches(message: Message, query: str) -> bool:
    return (
        message.message_type in (MESSAGE_INCOMING, MESSAGE_OUTGOING)
        and query.casefold() in message.content.casefold()
    )


def _meta(conversations: List[Conversation]) -> Dict[str, int]:
    return {
        "mine_count": 0,
        "assigned_count": 0,
        "unassigned_count": len(conversations),
        "all_count": len(conversations),
    }


def _page(conversations: List[Conversation], page: int) -> List[Conversation]:
    ordered = sorted(conversations, key=lambda c: (_last_activity(c), c.id), reverse=True)
    start = (max(page, 1) - 1) * PAGE_SIZE
    return ordered[start:start + PAGE_SIZE]


def _filter(conversations: List[Conversation], payload: Any) -> List[Conversation]:
    """The subset of Conversations::FilterService this recipe and the example
    recipe use: labels, inbox_id, status and mail_subject, joined by each
    condition's query_operator."""
    if not isinstance(payload, list) or not payload:
        raise ValueError("payload must be a non-empty list of conditions")
    result: Optional[set] = None
    operator = None
    for condition in payload:
        matched = {c.id for c in conversations if _condition(c, condition)}
        if result is None:
            result = matched
        elif str(operator or "and").lower() == "or":
            result |= matched
        else:
            result &= matched
        operator = condition.get("query_operator")
    return [c for c in conversations if c.id in (result or set())]


def _condition(conversation: Conversation, condition: Dict[str, Any]) -> bool:
    key = condition.get("attribute_key")
    operator = condition.get("filter_operator")
    values = [str(v) for v in condition.get("values") or []]
    if key == "labels":
        found = any(v in conversation.labels for v in values)
    elif key == "inbox_id":
        found = str(conversation.inbox_id) in values
    elif key == "status":
        found = conversation.status in values
    elif key == "mail_subject":
        subject = str(conversation.additional_attributes.get("mail_subject") or "").lower()
        if operator in ("contains", "does_not_contain"):
            found = any(v.lower() in subject for v in values)
        else:
            found = subject in [v.lower() for v in values]
    else:
        raise ValueError(f"Invalid attribute key - [{key}]")
    if operator in ("not_equal_to", "does_not_contain"):
        return not found
    return found


# ---------------------------------------------------------------------------
# The HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeChatwoot/" + VERSION
    protocol_version = "HTTP/1.1"

    @property
    def fake(self) -> "FakeChatwoot":
        return self.server.fake  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # noqa: D401 - quiet by default
        if self.fake.verbose:
            super().log_message(*args)

    def _send(self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _api_authorized(self) -> bool:
        if self.headers.get("api_access_token") == self.fake.api_token:
            return True
        self._send(401, {"errors": ["You need to sign in or sign up before continuing."]})
        return False

    def _account_path(self, path: str) -> Optional[str]:
        prefix = f"/api/v1/accounts/{ACCOUNT_ID}"
        return path[len(prefix):] if path.startswith(prefix) else None

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path == "/health":
            return self._send(200, {"status": "woot"})
        if parts.path == "/api":
            return self._send(200, {"version": VERSION, "timestamp": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.gmtime()), "queue_services": "ok",
                "data_services": "ok"})
        rest = self._account_path(parts.path)
        if rest is None:
            return self._send(404, {"error": "not found"})
        if not self._api_authorized():
            return None
        page = int((query.get("page") or ["1"])[0] or 1)
        with self.fake.state.lock:
            conversations = list(self.fake.state.conversations)
            if rest == "/conversations":
                q = (query.get("q") or [None])[0]
                if q is not None:
                    chosen = [c for c in conversations
                              if any(_matches(m, q) for m in c.messages)]
                else:
                    status = (query.get("status") or ["open"])[0]
                    chosen = [c for c in conversations
                              if status == "all" or c.status == status]
                body = {"data": {"meta": _meta(chosen),
                                 "payload": [conversation_json(c) for c in _page(chosen, page)]}}
                return self._send(200, body)
            if rest == "/search/messages":
                q = (query.get("q") or [""])[0]
                found = [(m, c) for c in conversations for m in c.messages if _matches(m, q)]
                found.sort(key=lambda pair: (pair[0].created_at, pair[0].id), reverse=True)
                start = (max(page, 1) - 1) * SEARCH_PAGE_SIZE
                return self._send(200, {"payload": {"messages": [
                    _search_message_json(m, c) for m, c in found[start:start + SEARCH_PAGE_SIZE]
                ]}})
            if rest == "/conversations/search":
                q = (query.get("q") or [""])[0]
                chosen = [c for c in conversations if any(_matches(m, q) for m in c.messages)]
                return self._send(200, {"meta": _meta(chosen),
                                        "payload": [search_json(c, q) for c in _page(chosen, page)]})
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        body = self._body()
        if parts.path == INGRESS_PATH:
            return self._ingress(body)
        rest = self._account_path(parts.path)
        if rest != "/conversations/filter":
            return self._send(404, {"error": "not found"})
        if not self._api_authorized():
            return None
        page = int((parse_qs(parts.query).get("page") or ["1"])[0] or 1)
        try:
            request = json.loads(body.decode("utf-8") or "{}")
            with self.fake.state.lock:
                chosen = _filter(list(self.fake.state.conversations), request.get("payload"))
                rendered = [conversation_json(c) for c in _page(chosen, page)]
        except (ValueError, AttributeError) as exc:
            return self._send(422, {"message": str(exc)})
        return self._send(200, {"meta": _meta(chosen), "payload": rendered})

    def _ingress(self, body: bytes) -> None:
        state = self.fake.state
        if not self.fake.authorized(self.headers.get("Authorization")):
            with state.lock:
                state.inbound.append(Inbound(status=401, authorized=False))
            return self._send(401, None, {
                "WWW-Authenticate": 'Basic realm="Action Mailbox"'})
        fields = parse_form_data(self.headers.get("Content-Type", ""), body)
        raw = fields.get("email")
        if raw is None:
            with state.lock:
                state.inbound.append(Inbound(status=422, authorized=True))
            return self._send(422, {"error": "param is missing or the value is empty: email"})
        if "envelope" in fields:
            try:
                envelope = json.loads(fields["envelope"].decode("utf-8"))
                recipients = list(envelope["to"])
            except (ValueError, KeyError, TypeError):
                with state.lock:
                    state.inbound.append(Inbound(status=422, authorized=True))
                return self._send(422, None)
            for recipient in recipients:
                raw = f"X-Original-To: {recipient}\n".encode("utf-8") + raw
        with state.lock:
            fingerprint = hashlib.sha1(raw).hexdigest()
            if fingerprint in state.raw_seen:
                state.inbound.append(Inbound(status=204, authorized=True))
            else:
                state.raw_seen.add(fingerprint)
                routed = receive(state, raw)
                state.inbound.append(Inbound(status=204, authorized=True, routed=routed))
        return self._send(204)


class FakeChatwoot:
    """A fake Chatwoot on a loopback port. Use as a context manager; `.port`
    and `.url` say where it listens."""

    def __init__(
        self,
        port: int = 0,
        *,
        password: str = DEFAULT_PASSWORD,
        api_token: str = DEFAULT_TOKEN,
        inbox_email: str = DEFAULT_INBOX_EMAIL,
        forward_to: str = DEFAULT_FORWARD_TO,
        label: str = DEFAULT_LABEL,
        host: str = "127.0.0.1",
        verbose: bool = False,
    ) -> None:
        self.password = password
        self.api_token = api_token
        self.verbose = verbose
        self.state = _State(inbox_email, forward_to, label)
        self._expected_auth = "Basic " + base64.b64encode(
            f"{INGRESS_USER}:{password}".encode("utf-8")).decode("ascii")
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ingress_auth(self) -> str:
        """The Authorization value the ingress accepts."""
        return self._expected_auth

    @property
    def conversations(self) -> List[Conversation]:
        with self.state.lock:
            return list(self.state.conversations)

    @property
    def inbound(self) -> List[Inbound]:
        with self.state.lock:
            return list(self.state.inbound)

    def authorized(self, header: Optional[str]) -> bool:
        return header is not None and header.strip() == self._expected_auth

    def start(self) -> "FakeChatwoot":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "FakeChatwoot":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A fake Chatwoot for the sweep recipe.")
    parser.add_argument("--port", type=int, default=3100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--password", default=DEFAULT_PASSWORD,
                        help="the Action Mailbox ingress password")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="the API access token")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    fake = FakeChatwoot(args.port, password=args.password, api_token=args.token,
                        host=args.host, verbose=args.verbose)
    print(f"fake Chatwoot on {fake.url}")
    print(f"CHATWOOT_INGRESS_AUTH='{fake.ingress_auth}'")
    print(f"CHATWOOT_API_TOKEN='{fake.api_token}'")
    try:
        fake._server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        fake._server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
