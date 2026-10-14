"""Loading an adapter, plus the two that cover most systems without any code.

  `http`     the system has an API. Look a tag up with a GET (or a templated
             POST search), read a JSON body, map fields by name.
  `mailbox`  the system has an outbound mail sink. An append-only JSON Lines
             file, one delivered message per line, matched on the tag stamped
             into the body by `pipeline/messages.py`.

Everything else, including a database, is `--readback module:attribute`: an
import path naming an adapter object, a class, a factory, or a plain fetch
function. A one-line lambda is a complete adapter.

THE SAFETY ARGUMENT. A readback URL is a place this tool connects to and reads
from, and on a real deployment that is a ticket store that may hold other
people's data. Every URL the http adapter will request (the lookup url, the
list url, and every pagination link a server hands back) goes through
`require_readback_target`, and a pagination link on a different origin is
refused outright, because following it would send the configured credentials
to a host nobody configured.

A custom `module:attribute` adapter is NOT gated. TestingHQ cannot see where
arbitrary code connects, so a check on a url it is not actually using would be
a check that looks correct while being inert. The operator who wrote the
adapter is the one deciding where it reads from.
"""
from __future__ import annotations

import copy
import email.utils
import http.client
import importlib
import inspect
import json
import os
import random
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from ..core import guardrails
from ..core import transport
from ..core.transport import ClientResponse, PreparedRequest, UrllibHttpClient
from .messages import harvest_tag
from .readback import (
    CHECKABLE_FIELDS,
    FetchResult,
    FunctionAdapter,
    Readback,
    ReadbackAdapter,
    ReadbackError,
    normalize_attachment_names,
    normalize_message_ids,
)

#: The built-in adapter kinds.
BUILTIN_KINDS: Tuple[str, ...] = ("http", "mailbox")

DEFAULT_HTTP_TAG_PARAM = "tag"
DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_ITEMS_KEY = "items"
DEFAULT_METHOD = "GET"
HTTP_METHODS: Tuple[str, ...] = ("GET", "POST")

#: Retries after the first attempt, for a 429, a 5xx, a timeout or a
#: connection error. Six attempts in all.
DEFAULT_RETRY_MAX = 5
#: The first backoff, doubled per retry, with jitter.
DEFAULT_RETRY_BASE_SECONDS = 0.5
#: Ceiling on one computed backoff. A `Retry-After` from the server is
#: honoured above this, up to `MAX_RETRY_AFTER`.
MAX_BACKOFF_SECONDS = 30.0
#: A server asking for a longer wait than this is treated as a failure, so a
#: `Retry-After: 86400` cannot park a run for a day.
MAX_RETRY_AFTER = 300.0

#: Requests per second across every request one adapter makes: lookups,
#: pages, listings and retries. Polling 20 tags every 0.5 seconds is 2400
#: requests a minute, and vendor limits are 40 to 700 a minute.
DEFAULT_READBACK_RATE = 4.0

#: How many pages one lookup or listing may follow before it stops and marks
#: the result partial.
DEFAULT_MAX_PAGES = 10
#: The page size sent in `page_size_param`. 100 is the maximum Zendesk and
#: Freshdesk accept and a common ceiling elsewhere.
DEFAULT_PAGE_SIZE = 100

#: The tag `preflight` looks up. Not a tag any run produces.
PREFLIGHT_TAG = "testinghq-preflight-probe"

#: The mailbox adapter warns once a sink grows past this many lines.
MAILBOX_WARN_LINES = 50_000

#: The placeholder a `query` or `body` template carries.
TAG_PLACEHOLDER = "{tag}"

#: How each `Readback` field is read out of a JSON object, when the adapter's
#: config does not say otherwise. Dotted paths address nested objects, a
#: numeric segment indexes a list, and one `*` segment collects from every
#: element of a list (`attachments.*.name`).
DEFAULT_FIELD_MAP: Dict[str, str] = {
    "ticket_id": "id",
    "from_addr": "from",
    "subject": "subject",
    "body": "body",
    "attachment_names": "attachments",
    "route": "route",
    "message_id": "message_id",
    "in_reply_to": "in_reply_to",
    "references": "references",
    "tag": "tag",
    "category": "category",
    "priority": "priority",
}

#: Fields that legitimately hold a list. Every other field is a scalar.
_LIST_FIELDS = ("attachment_names", "references")

#: Keys an address object is read through, in order, case-insensitively.
_ADDRESS_KEYS = ("email", "address", "addr")


class AdapterError(ReadbackError):
    """Raised for a malformed `[readback]` config, an adapter that cannot be
    imported or built, or a readback endpoint that failed after retries."""


class BudgetExhausted(AdapterError):
    """Raised when a request or a retry would run past the `--max-wait`
    budget. The poll treats this as "ran out of time", not as a broken run."""


class ReadbackWarning(UserWarning):
    """Something about a readback that is worth saying but is not a refusal."""


def require_readback_target(url: str, *, allow_public_hosts: bool = False) -> str:
    """Gate a readback URL through the canonical guardrail. The one place this
    check lives."""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise AdapterError(
            f"a readback url must start with http:// or https://, got {url!r}"
        )
    guardrails.require_configured_target(url, (url,), allow_public_hosts=allow_public_hosts)
    return url


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


FieldPath = Union[str, bool]


@dataclass(frozen=True)
class ReadbackConfig:
    """The `[readback]` table, parsed and validated."""

    kind: str
    url: Optional[str] = None
    path: Optional[str] = None
    tag_param: str = DEFAULT_HTTP_TAG_PARAM
    items_key: str = DEFAULT_ITEMS_KEY
    timeout: float = DEFAULT_HTTP_TIMEOUT
    #: Overrides over `DEFAULT_FIELD_MAP`. A value of False unmaps a default.
    fields: Dict[str, FieldPath] = field(default_factory=dict)
    spec: Optional[str] = None
    allow_public_hosts: bool = False
    list_path: Optional[str] = None
    enumerate_records: bool = True
    #: Resolved request headers, for an adapter to send. Never serialized.
    headers: Dict[str, str] = field(default_factory=dict)
    query: Optional[str] = None
    method: str = DEFAULT_METHOD
    body: Optional[str] = None
    page_param: Optional[str] = None
    page_size_param: Optional[str] = None
    next_path: Optional[str] = None
    max_pages: int = DEFAULT_MAX_PAGES
    retry_max: int = DEFAULT_RETRY_MAX
    retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS
    #: Requests per second. Set from `--readback-rate`, not from the file.
    rate: float = DEFAULT_READBACK_RATE
    #: The directory of the config file this came from, so a relative mailbox
    #: `path` means the same thing whatever directory the run starts in. Set
    #: by the CLI loader, not from the file, and never serialized.
    config_dir: Optional[str] = None
    #: The table as the file (or the command line) gave it, before parsing,
    #: so a custom adapter is handed its own keys as well as the built-in
    #: ones. Never serialized: `to_json` is the record of a run.
    table: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def field_map(self) -> Dict[str, str]:
        """The configured field map over the default one, minus any field
        unmapped with `false`."""
        merged: Dict[str, Any] = dict(DEFAULT_FIELD_MAP)
        merged.update(self.fields or {})
        return {name: path for name, path in merged.items() if path is not False}

    def unmapped_fields(self) -> List[str]:
        return sorted(name for name, path in (self.fields or {}).items() if path is False)

    def header_names(self) -> List[str]:
        """The header names, sorted. Names and never values, because this is
        what lands in an artifact."""
        return sorted(self.headers)

    def to_json(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "path": self.path,
            "tag_param": self.tag_param,
            "items_key": self.items_key,
            "timeout": self.timeout,
            "field_map": self.field_map(),
            "unmapped_fields": self.unmapped_fields(),
            "spec": self.spec,
            "list_path": self.list_path,
            "enumerate": self.enumerate_records,
            "headers": self.header_names(),
            "query": self.query,
            "method": self.method,
            "body": self.body,
            "page_param": self.page_param,
            "page_size_param": self.page_size_param,
            "next_path": self.next_path,
            "max_pages": self.max_pages,
            "retry_max": self.retry_max,
            "retry_base_seconds": self.retry_base_seconds,
            "rate": self.rate,
        }

    def adapter_table(self) -> Dict[str, Any]:
        """What a custom adapter's class or `config` factory is called with.

        Every key of the table as the operator wrote it, so an adapter's own
        keys (`project_id = "..."`) reach it, overlaid with the built-in keys
        as the loader normalised them, so an adapter written against those
        (`field_map`, `timeout`, `rate`, `headers`) sees what it always saw.

        Two things are deliberately not done. `headers` is the header names,
        never their values: a custom adapter that needs a credential reads the
        environment itself, so no secret is handed to code this tool cannot
        see. And an adapter's own keys are passed through verbatim, so a value
        such as `"env:NAME"` arrives as that string, for the adapter to resolve
        if it means it as a reference. A fresh copy each call, so an adapter
        that edits its table cannot change the config.
        """
        table = copy.deepcopy(self.table)
        table.update(self.to_json())
        return table


#: A `[readback.headers]` value must start with this; the rest is the name of an
#: environment variable holding the value.
ENV_PREFIX = "env:"


def resolve_header_values(
    raw_headers: Any, env: Optional[Mapping[str, str]] = None
) -> Dict[str, str]:
    """Turn `[readback.headers]` into the values to send, refusing literals.

    Every value must be `env:NAME`. A missing variable is an error here rather
    than at connect time, so a command refuses before it sends anything.
    """
    if raw_headers is None:
        return {}
    if not isinstance(raw_headers, dict):
        raise AdapterError(
            f"[readback.headers] must be a table, got {type(raw_headers).__name__}"
        )
    environ = os.environ if env is None else env

    resolved: Dict[str, str] = {}
    for name, value in raw_headers.items():
        if not isinstance(value, str):
            raise AdapterError(
                f"[readback.headers].{name} must be a string such as "
                f"\"{ENV_PREFIX}HQ_READBACK_TOKEN\", got {type(value).__name__}"
            )
        if not value.startswith(ENV_PREFIX):
            raise AdapterError(
                f"[readback.headers].{name} is a literal value. Header values "
                f"must come from the environment as \"{ENV_PREFIX}<VARIABLE>\"; "
                f"a readback config is a file that gets shared, and a secret in "
                f"it is a secret that leaks"
            )
        variable = value[len(ENV_PREFIX) :].strip()
        if not variable:
            raise AdapterError(
                f"[readback.headers].{name} names no environment variable after "
                f"\"{ENV_PREFIX}\""
            )
        if variable not in environ:
            raise AdapterError(
                f"[readback.headers].{name} refers to environment variable "
                f"{variable!r}, which is not set. The refusal is deliberate: a "
                f"run must not start without the credentials it was configured "
                f"to use, and must not discover that mid-run"
            )
        resolved[name] = environ[variable]
    return resolved


def _check_field_path(name: str, path: str, what: str) -> None:
    segments = path.split(".")
    if not path or any(segment == "" for segment in segments):
        raise AdapterError(f"[readback].{what}.{name} = {path!r} has an empty segment")
    wildcards = sum(1 for segment in segments if segment == "*")
    if wildcards > 1:
        raise AdapterError(
            f"[readback].{what}.{name} = {path!r} has {wildcards} '*' segments; "
            "one is allowed"
        )
    if wildcards and name == "ticket_id":
        raise AdapterError(
            f"[readback].{what}.ticket_id = {path!r}: the identifier must be one "
            "value, so it cannot use a '*' segment"
        )


def _as_str_table(value: Any, what: str) -> Dict[str, FieldPath]:
    """Validate `[readback.fields]`: known field names, string paths or
    `false` to unmap a default."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AdapterError(f"[readback].{what} must be a table, got {type(value).__name__}")
    table: Dict[str, FieldPath] = {}
    for key, item in value.items():
        if key not in CHECKABLE_FIELDS and key not in DEFAULT_FIELD_MAP:
            raise AdapterError(
                f"[readback].{what} names {key!r}, which is not a readback "
                f"field; known fields are {sorted(DEFAULT_FIELD_MAP)}"
            )
        if item is False:
            if key == "ticket_id":
                raise AdapterError(
                    f"[readback].{what}.ticket_id cannot be unmapped: a record "
                    "without an identifier cannot be counted"
                )
            table[key] = False
            continue
        if not isinstance(item, str):
            raise AdapterError(
                f"[readback].{what}.{key} must be a string path, or false to "
                f"unmap it, got {type(item).__name__}"
            )
        _check_field_path(key, item, what)
        table[key] = item
    return table


def _optional_str(raw: Mapping[str, Any], key: str) -> Optional[str]:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AdapterError(f"[readback].{key} must be a non-empty string")
    return value


def _number(raw: Mapping[str, Any], key: str, default: float, *, minimum: float) -> float:
    value = raw.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AdapterError(
            f"[readback].{key} must be a number, got {type(value).__name__}"
        )
    if value < minimum:
        raise AdapterError(f"[readback].{key} must be >= {minimum}, got {value}")
    return float(value)


def _integer(raw: Mapping[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AdapterError(
            f"[readback].{key} must be an integer, got {type(value).__name__}"
        )
    if value < minimum:
        raise AdapterError(f"[readback].{key} must be >= {minimum}, got {value}")
    return value


def parse_readback_config(
    raw: Any, env: Optional[Mapping[str, str]] = None
) -> ReadbackConfig:
    """Build a ReadbackConfig from a raw `[readback]` table, or from a `kind`
    string when the config came from the command line.

    `env` is the environment to read `env:NAME` header values from, and
    defaults to the process environment.
    """
    if isinstance(raw, str):
        return ReadbackConfig(
            kind=raw, spec=raw if ":" in raw else None, table={"kind": raw}
        )
    if raw is None:
        raise AdapterError(
            "no readback adapter was configured. Verification needs to be able "
            "to ask the system what it produced; pass --readback http, "
            "--readback mailbox, or --readback module:attribute to point at it."
        )
    if not isinstance(raw, dict):
        raise AdapterError(
            f"[readback] must be a table or a kind string, got {type(raw).__name__}"
        )

    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        raise AdapterError("[readback] must declare a string 'kind'")

    spec = raw.get("spec")
    if spec is None and ":" in kind:
        spec = kind

    timeout = _number(raw, "timeout", DEFAULT_HTTP_TIMEOUT, minimum=0.0)

    enumerate_records = raw.get("enumerate", True)
    if not isinstance(enumerate_records, bool):
        raise AdapterError(
            f"[readback].enumerate must be true or false, got "
            f"{type(enumerate_records).__name__}"
        )

    method = raw.get("method", DEFAULT_METHOD)
    if not isinstance(method, str) or method.upper() not in HTTP_METHODS:
        raise AdapterError(
            f"[readback].method must be one of {list(HTTP_METHODS)}, got {method!r}"
        )
    method = method.upper()

    query = _optional_str(raw, "query")
    if query is not None and TAG_PLACEHOLDER not in query:
        raise AdapterError(
            f"[readback].query = {query!r} has no {TAG_PLACEHOLDER} placeholder, "
            "so every lookup would ask the same question"
        )
    body = _optional_str(raw, "body")
    if body is not None:
        if method != "POST":
            raise AdapterError("[readback].body is only sent with method = \"POST\"")
        if TAG_PLACEHOLDER not in body:
            raise AdapterError(
                f"[readback].body has no {TAG_PLACEHOLDER} placeholder, so every "
                "lookup would ask the same question"
            )

    page_param = _optional_str(raw, "page_param")
    page_size_param = _optional_str(raw, "page_size_param")
    next_path = _optional_str(raw, "next_path")
    if page_size_param and not page_param:
        raise AdapterError("[readback].page_size_param needs page_param as well")
    if next_path and page_param:
        raise AdapterError(
            "[readback] sets both next_path and page_param; an API paginates one "
            "way, so pick the one it uses"
        )

    allow_public = raw.get("allow_public_hosts", False)
    if not isinstance(allow_public, bool):
        raise AdapterError("[readback].allow_public_hosts must be true or false")

    return ReadbackConfig(
        kind=kind,
        url=raw.get("url"),
        path=raw.get("path"),
        tag_param=str(raw.get("tag_param", DEFAULT_HTTP_TAG_PARAM)),
        items_key=str(raw.get("items_key", DEFAULT_ITEMS_KEY)),
        timeout=timeout,
        fields=_as_str_table(raw.get("fields"), "fields"),
        spec=spec,
        allow_public_hosts=allow_public,
        list_path=raw.get("list_path"),
        enumerate_records=enumerate_records,
        headers=resolve_header_values(raw.get("headers"), env),
        query=query,
        method=method,
        body=body,
        page_param=page_param,
        page_size_param=page_size_param,
        next_path=next_path,
        max_pages=_integer(raw, "max_pages", DEFAULT_MAX_PAGES, minimum=1),
        retry_max=_integer(raw, "retry_max", DEFAULT_RETRY_MAX, minimum=0),
        retry_base_seconds=_number(
            raw, "retry_base_seconds", DEFAULT_RETRY_BASE_SECONDS, minimum=0.0
        ),
        table=copy.deepcopy(dict(raw)),
    )


# ---------------------------------------------------------------------------
# Field extraction from a JSON object
# ---------------------------------------------------------------------------


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()


def _step(current: Any, part: str) -> Any:
    if isinstance(current, dict):
        return current.get(part, _MISSING)
    if isinstance(current, (list, tuple)):
        if not part.lstrip("-").isdigit():
            return _MISSING
        index = int(part)
        if not -len(current) <= index < len(current):
            return _MISSING
        return current[index]
    return _MISSING


def _dig(data: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts and lists.

    Returns `_MISSING` rather than None when the path is absent, so a field
    that is present and null stays distinguishable from one the response did
    not carry. A `*` segment collects the rest of the path from every element
    of a list and returns the list of what it found: `[]` for an empty list,
    `_MISSING` when the list had elements and none carried the rest.
    """
    parts = path.split(".")
    current = data
    for position, part in enumerate(parts):
        if part == "*":
            if isinstance(current, dict):
                elements = list(current.values())
            elif isinstance(current, (list, tuple)):
                elements = list(current)
            else:
                return _MISSING
            rest = ".".join(parts[position + 1 :])
            if not rest:
                return elements
            found = [_dig(element, rest) for element in elements]
            kept = [value for value in found if value is not _MISSING]
            if elements and not kept:
                return _MISSING
            return kept
        current = _step(current, part)
        if current is _MISSING:
            return _MISSING
    return current


def _shape(value: Any) -> str:
    """A short description of a JSON value's shape, for a field note."""
    if isinstance(value, dict):
        keys = sorted(value)[:6]
        return f"an object with keys {keys}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "an empty list"
        first = value[0]
        if isinstance(first, dict):
            return f"a list of objects with keys {sorted(first)[:6]}"
        return f"a list of {type(first).__name__}"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return f"a number ({value})"
    return type(value).__name__


def _address_from_object(value: Dict[str, Any]) -> Optional[str]:
    lowered = {str(key).lower(): item for key, item in value.items()}
    for key in _ADDRESS_KEYS:
        candidate = lowered.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _coerce(name: str, path: str, value: Any) -> Tuple[bool, Any, Optional[str]]:
    """Turn a raw JSON value into what the Readback field holds.

    Returns (readable, value, note). An unreadable value is not an error: the
    field is left out of `fields`, its check is SKIPPED, and the note says what
    shape was there and how to point at the part that would work.
    """
    if value is None:
        return True, None, None

    if name in _LIST_FIELDS:
        if isinstance(value, str):
            return True, value, None
        if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            return True, list(value), None
        hint = ""
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
            keys = sorted(value[0])
            pick = next(
                (k for k in keys if k.lower() in ("name", "filename", "file_name")),
                keys[0] if keys else "name",
            )
            hint = f"; map {name} = \"{path}.*.{pick}\" to read one key from each"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            hint = "; a count is not a list of names"
        return False, None, f"{path} is {_shape(value)}, not a list of strings{hint}"

    if name == "from_addr" and isinstance(value, dict):
        address = _address_from_object(value)
        if address is not None:
            return True, address, None
        return False, None, (
            f"{path} is {_shape(value)} with no email or address key; map "
            f"from_addr to the key that holds the address"
        )

    if isinstance(value, str):
        return True, value, None
    if isinstance(value, (int, float, bool)):
        return True, str(value), None
    if isinstance(value, (list, tuple)) and len(value) == 1 and isinstance(value[0], str):
        # A wildcard that matched exactly one string is that string.
        return True, value[0], None
    return False, None, f"{path} is {_shape(value)}, not a single value"


def readback_from_json(data: Dict[str, Any], field_map: Dict[str, str]) -> Readback:
    """Map one JSON object to a Readback, recording exactly which fields the
    response carried, and a note for each field it carried in a shape the
    checks cannot read."""
    if not isinstance(data, dict):
        raise AdapterError(
            f"a readback record must be a JSON object, got {type(data).__name__}"
        )

    values: Dict[str, Any] = {}
    present: List[str] = []
    notes: List[Tuple[str, str]] = []
    for name, path in field_map.items():
        found = _dig(data, path)
        if found is _MISSING:
            continue
        if name == "ticket_id":
            values[name] = found
            continue
        readable, value, note = _coerce(name, path, found)
        if readable:
            present.append(name)
            values[name] = value
        elif note:
            notes.append((name, note))

    ticket_id = values.get("ticket_id")
    if ticket_id in (None, "") or isinstance(ticket_id, (dict, list, tuple, bool)):
        path = field_map.get("ticket_id", "id")
        raise AdapterError(
            "a readback record must carry an identifier, so that one message "
            "producing two records can be told apart from one producing one. "
            f"Nothing usable was at {path!r} in a record with keys "
            f"{sorted(data)[:8]}. Say where it lives with [readback].fields, for "
            'example ticket_id = "data.id", or check [readback].items_key if '
            "this object is a wrapper around the records."
        )
    present.insert(0, "ticket_id")

    references = values.get("references")
    if isinstance(references, str):
        references = references.split()

    return Readback(
        exists=True,
        ticket_id=str(ticket_id),
        from_addr=values.get("from_addr"),
        subject=values.get("subject"),
        body=values.get("body"),
        attachment_names=normalize_attachment_names(values.get("attachment_names")),
        route=values.get("route"),
        message_id=values.get("message_id"),
        in_reply_to=values.get("in_reply_to"),
        references=normalize_message_ids(references),
        tag=values.get("tag"),
        category=values.get("category"),
        priority=values.get("priority"),
        fields=tuple(present),
        field_notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# URL and template helpers
# ---------------------------------------------------------------------------


def _with_params(url: str, params: Mapping[str, Any], *, drop: Tuple[str, ...] = ()) -> str:
    """`url` with `params` set in its query string, replacing any existing
    value for the same names, and with the names in `drop` removed. Every
    other parameter, before or after, is kept."""
    scheme, netloc, path, query, fragment = urlsplit(url)
    replaced = set(params) | set(drop)
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k not in replaced]
    kept.extend((k, str(v)) for k, v in params.items())
    return urlunsplit((scheme, netloc, path, urlencode(kept), fragment))


def _origin(url: str) -> Tuple[str, str]:
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _json_pointer(data: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer (`/links/next`), or a dotted path when
    the value does not start with `/`."""
    if not pointer.startswith("/"):
        return _dig(data, pointer)
    current = data
    for raw in pointer[1:].split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        current = _step(current, part)
        if current is _MISSING:
            return _MISSING
    return current


def _retry_after_seconds(response: Any, now: Callable[[], float] = time.time) -> Optional[float]:
    """The server's `Retry-After`, in seconds, or None when it sent none.
    Accepts both forms RFC 9110 allows: delay-seconds and an HTTP-date."""
    headers = getattr(response, "headers", None) or {}
    value = None
    for name, item in dict(headers).items():
        if str(name).lower() == "retry-after":
            value = str(item).strip()
            break
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now())


def _default_client() -> Any:
    """The connection-reusing client when this build has one, else urllib."""
    persistent = getattr(transport, "PersistentHttpClient", None)
    if persistent is not None:
        try:
            return persistent()
        except TypeError:
            pass
    return UrllibHttpClient()


# ---------------------------------------------------------------------------
# The http adapter
# ---------------------------------------------------------------------------


class HttpJsonAdapter:
    """Reads a system's output over its own JSON API.

    A lookup is a GET with the tag (or the `query` template filled with it) in
    `tag_param`, or a POST of the `body` template filled with it. A response
    may be a bare list of records, a single object, or an object wrapping a
    list under `items_key` (a dotted path).

    Every request is paced to `rate` per second and retried on 429, 5xx,
    timeouts and connection errors with exponential backoff and jitter,
    honouring `Retry-After`. A failure that outlasts `retry_max` retries is an
    `AdapterError`, never a raw socket exception.

    Pagination follows `next_path` (a JSON pointer to the next url), or counts
    `page_param` upwards, at most `max_pages` pages; a result cut short there
    is a `FetchResult` with `partial = True`.
    """

    def __init__(
        self,
        url: str,
        *,
        tag_param: str = DEFAULT_HTTP_TAG_PARAM,
        items_key: str = DEFAULT_ITEMS_KEY,
        timeout: float = DEFAULT_HTTP_TIMEOUT,
        field_map: Optional[Dict[str, str]] = None,
        client: Any = None,
        allow_public_hosts: bool = False,
        list_path: Optional[str] = None,
        enumerate_records: bool = True,
        headers: Optional[Mapping[str, str]] = None,
        query: Optional[str] = None,
        method: str = DEFAULT_METHOD,
        body: Optional[str] = None,
        page_param: Optional[str] = None,
        page_size_param: Optional[str] = None,
        next_path: Optional[str] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        rate: Optional[float] = DEFAULT_READBACK_RATE,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ) -> None:
        if not url:
            raise AdapterError("the http readback adapter needs a url")
        self.allow_public_hosts = allow_public_hosts
        self.url = require_readback_target(url, allow_public_hosts=allow_public_hosts)
        self.tag_param = tag_param
        self.items_key = items_key
        self.timeout = timeout
        self.field_map = field_map or dict(DEFAULT_FIELD_MAP)
        #: Sent on every request. Values came from the environment and are
        #: never logged, reported, or written to an artifact.
        self.headers = dict(headers or {})
        self.list_path = list_path
        self.enumerate_records = enumerate_records
        self.query = query
        self.method = (method or DEFAULT_METHOD).upper()
        self.body = body
        self.page_param = page_param
        self.page_size_param = page_size_param
        self.next_path = next_path
        self.max_pages = max(1, int(max_pages))
        self.retry_max = max(0, int(retry_max))
        self.retry_base_seconds = float(retry_base_seconds)
        self.rate = rate if rate and rate > 0 else None
        self._sleep = sleep
        self._clock = clock
        self._deadline: Optional[float] = None
        self._next_slot: Optional[float] = None
        self._rng = rng or random.Random()
        self._client = client if client is not None else _default_client()
        #: Requests and retries made, for a report that wants to say so.
        self.requests_made = 0
        self.retries_made = 0
        #: A bool rather than duck typing on `list_all`, so `can_enumerate()`
        #: says no before a tool sends anything when listing is not possible.
        self.can_enumerate = bool(
            enumerate_records and (self.method == "GET" or list_path is not None)
        )
        self._list_url = self._build_list_url()

    # -- time and pacing -------------------------------------------------

    def bind_clock(
        self,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        deadline: Optional[float] = None,
    ) -> None:
        """Use the poll's clock and sleep, and stop at `deadline` (in that
        clock's units). `read_back_all` calls this so a request, a retry wait
        and the rate limiter all spend the same `--max-wait` budget. A
        deadline of None removes the bound."""
        if clock is not self._clock:
            self._next_slot = None
        self._clock = clock
        self._sleep = sleep
        self._deadline = deadline

    def _remaining(self) -> Optional[float]:
        if self._deadline is None:
            return None
        return self._deadline - self._clock()

    def _pace(self, context: str) -> None:
        if self.rate is None:
            return
        now = self._clock()
        if self._next_slot is not None and self._next_slot > now:
            wait = self._next_slot - now
            remaining = self._remaining()
            if remaining is not None and wait >= remaining:
                raise BudgetExhausted(
                    f"readback for {context} would wait {wait:.2f}s for the "
                    f"readback rate limit, past the --max-wait budget"
                )
            self._sleep(wait)
            now = self._clock()
        start = max(now, self._next_slot or now)
        self._next_slot = start + 1.0 / self.rate

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> float:
        span = min(self.retry_base_seconds * (2 ** attempt), MAX_BACKOFF_SECONDS)
        delay = span / 2 + self._rng.uniform(0, span / 2)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    # -- one request, with retries ---------------------------------------

    def _request(self, url: str, method: str, body: bytes, context: str) -> ClientResponse:
        attempt = 0
        while True:
            remaining = self._remaining()
            if remaining is not None and remaining <= 0:
                raise BudgetExhausted(
                    f"readback for {context} ran out of the --max-wait budget"
                )
            self._pace(context)
            timeout = self.timeout
            remaining = self._remaining()
            if remaining is not None:
                timeout = max(0.05, min(timeout, remaining))
            headers = dict(self.headers)
            if body and not any(name.lower() == "content-type" for name in headers):
                headers["Content-Type"] = "application/json"
            retry_after: Optional[float] = None
            self.requests_made += 1
            try:
                response = self._client.send(
                    PreparedRequest(
                        url=url, method=method, headers=headers, body=body, timeout=timeout
                    )
                )
            except (OSError, http.client.HTTPException) as exc:
                failure = f"{type(exc).__name__}: {exc}"
            else:
                if response.status == 429 or 500 <= response.status <= 599:
                    failure = f"HTTP {response.status}: {response.body[:200]!r}"
                    retry_after = _retry_after_seconds(response)
                else:
                    return response

            if attempt >= self.retry_max:
                raise AdapterError(
                    f"readback endpoint {self.url} failed for {context} after "
                    f"{attempt + 1} attempt(s): {failure}"
                )
            if retry_after is not None and retry_after > MAX_RETRY_AFTER:
                raise AdapterError(
                    f"readback endpoint {self.url} asked to wait {retry_after:.0f}s "
                    f"before retrying {context} (Retry-After), more than the "
                    f"{MAX_RETRY_AFTER:.0f}s this tool will wait: {failure}"
                )
            delay = self._backoff(attempt, retry_after)
            remaining = self._remaining()
            if remaining is not None and delay >= remaining:
                raise BudgetExhausted(
                    f"readback for {context} would retry after {delay:.2f}s, past "
                    f"the --max-wait budget: {failure}"
                )
            self._sleep(delay)
            attempt += 1
            self.retries_made += 1

    # -- requests a lookup and a listing make ----------------------------

    def _lookup(self, tag: str) -> Tuple[str, str, bytes]:
        value = self.query.replace(TAG_PLACEHOLDER, tag) if self.query else tag
        if self.method == "POST":
            url = self.url if not self.query else _with_params(self.url, {self.tag_param: value})
            filled = (self.body or "").replace(TAG_PLACEHOLDER, json.dumps(tag)[1:-1])
            return url, "POST", filled.encode("utf-8")
        return _with_params(self.url, {self.tag_param: value}), "GET", b""

    def _build_list_url(self) -> str:
        if self.list_path:
            if self.list_path.startswith(("http://", "https://")):
                target = self.list_path
            else:
                target = urljoin(self.url, self.list_path)
            return require_readback_target(
                target, allow_public_hosts=self.allow_public_hosts
            )
        return _with_params(self.url, {}, drop=(self.tag_param,))

    def _decode(self, response: ClientResponse, context: str) -> Any:
        text = response.body.decode("utf-8", errors="strict") if response.body else ""
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(
                f"readback endpoint {self.url} did not return valid JSON for "
                f"{context}: {exc}. First bytes: {response.body[:120]!r}"
            ) from exc

    def _records(self, payload: Any) -> List[Any]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise AdapterError(
                "a readback response must be a JSON object or a list, got "
                f"{type(payload).__name__}"
            )
        nested = _dig(payload, self.items_key)
        if isinstance(nested, list):
            return nested
        if nested is None:
            return []
        if nested is not _MISSING:
            raise AdapterError(
                f"[readback].items_key = {self.items_key!r} names {_shape(nested)}, "
                "not a list of records"
            )
        if _dig(payload, self.field_map.get("ticket_id", "id")) is not _MISSING:
            return [payload]
        lists = sorted(
            key for key, value in payload.items()
            if isinstance(value, list) and all(isinstance(v, dict) for v in value)
        )
        if lists:
            raise AdapterError(
                f"the readback response is an object with no {self.items_key!r} "
                f"key, and it has a list under {lists}. If that is where the "
                f"records are, set [readback].items_key = {lists[0]!r}"
            )
        return [payload]

    def _collect(
        self, url: str, method: str, body: bytes, context: str, *, lookup: bool
    ) -> FetchResult:
        raw: List[Any] = []
        status: Optional[int] = None
        partial = False
        page = 1
        biggest_page = 0
        current = url
        pages = 0
        while True:
            request_url = current
            if self.page_param:
                params: Dict[str, Any] = {self.page_param: page}
                if self.page_size_param:
                    params[self.page_size_param] = DEFAULT_PAGE_SIZE
                request_url = _with_params(current, params)
            response = self._request(request_url, method, body, context)
            pages += 1
            if status is None:
                status = response.status
            if response.status == 404 and (lookup or pages > 1):
                # A lookup that found nothing is an empty answer, not an error,
                # and a page past the end is the end.
                break
            if not 200 <= response.status < 300:
                raise AdapterError(
                    f"readback endpoint {self.url} returned {response.status} for "
                    f"{context}: {response.body[:200]!r}"
                )
            payload = self._decode(response, context)
            records = self._records(payload)
            raw.extend(records)

            next_url = None
            if self.next_path and isinstance(payload, (dict, list)):
                pointed = _json_pointer(payload, self.next_path)
                if isinstance(pointed, str) and pointed.strip():
                    next_url = self._follow(current, pointed.strip(), context)
            elif self.page_param and records:
                full = (
                    len(records) >= DEFAULT_PAGE_SIZE
                    if self.page_size_param
                    else len(records) >= biggest_page
                )
                if full:
                    next_url = current
            biggest_page = max(biggest_page, len(records))
            if next_url is None:
                break
            if pages >= self.max_pages:
                partial = True
                break
            current = next_url
            page += 1

        return FetchResult(
            [readback_from_json(record, self.field_map) for record in raw],
            partial=partial,
            status=status,
        )

    def _follow(self, current: str, pointed: str, context: str) -> str:
        target = urljoin(current, pointed)
        if _origin(target) != _origin(self.url) and _origin(target) != _origin(self._list_url):
            raise AdapterError(
                f"the readback response for {context} points at the next page "
                f"{target!r}, on a different host from {self.url}. Following it "
                "would send the configured credentials somewhere nobody "
                "configured, so the run stops here."
            )
        return require_readback_target(target, allow_public_hosts=self.allow_public_hosts)

    # -- the adapter contract --------------------------------------------

    def fetch(self, probe) -> FetchResult:
        url, method, body = self._lookup(probe.tag)
        return self._collect(url, method, body, f"tag {probe.tag!r}", lookup=True)

    def list_all(self) -> FetchResult:
        """Every record the endpoint holds, which is what makes a stray record
        findable. Raises when this adapter cannot enumerate; ask
        `can_enumerate()` first."""
        if not self.can_enumerate:
            why = (
                "[readback].enumerate is false"
                if not self.enumerate_records
                else "a POST search needs [readback].list_path to list from"
            )
            raise ReadbackError(
                f"the readback endpoint {self.url} cannot enumerate its records "
                f"({why}), so stray records cannot be searched for."
            )
        return self._collect(self._list_url, "GET", b"", "the full record list", lookup=False)

    def preflight(self) -> None:
        """One lookup (and, when listing is on, one request to the list url)
        before anything is sent. A refused credential, a list url that is 404,
        a response that is not JSON, or a field map that cannot find an
        identifier is a refusal now rather than a broken run later."""
        checks = [(self._lookup(PREFLIGHT_TAG), "a preflight lookup", True)]
        if self.can_enumerate:
            checks.append(((self._list_url, "GET", b""), "the list url", False))
        for (url, method, body), what, lookup in checks:
            response = self._request(url, method, body, what)
            if response.status in (401, 403):
                raise AdapterError(
                    f"readback endpoint {self.url} refused {what} with "
                    f"{response.status}: check the [readback.headers] credentials "
                    "before sending anything"
                )
            if response.status == 404:
                if lookup:
                    continue
                raise AdapterError(
                    f"readback {what} {url} returned 404: check [readback].url "
                    "and list_path, or set enumerate = false if this API cannot "
                    "list"
                )
            if not 200 <= response.status < 300:
                raise AdapterError(
                    f"readback endpoint {self.url} returned {response.status} for "
                    f"{what}: {response.body[:200]!r}"
                )
            payload = self._decode(response, what)
            for record in self._records(payload)[:5]:
                readback_from_json(record, self.field_map)

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()
        self._client = None


# ---------------------------------------------------------------------------
# The mailbox adapter
# ---------------------------------------------------------------------------


#: Sink JSON keys for each field, used when the config does not override.
MAILBOX_FIELD_MAP: Dict[str, str] = {
    "from_addr": "from",
    "subject": "subject",
    "body": "body",
    "attachment_names": "attachments",
    "route": "route",
    "message_id": "message_id",
    "in_reply_to": "in_reply_to",
    "references": "references",
    "category": "category",
    "priority": "priority",
}


class MailboxAdapter:
    """Reads an outbound mail sink: an append-only JSON Lines file, one
    delivered message per line.

    The file is read incrementally: each fetch reads only the bytes appended
    since the last one, parses each complete line once, and indexes it by tag,
    so a poll over 50 tags costs one read of the new bytes and 50 dictionary
    lookups. A trailing line with no newline that is not yet valid JSON is a
    write in progress: it is left unread and picked up on the next fetch. A
    file that shrank or was replaced is read again from the start.

    Matching is by a literal `tag` field on the line, then the tag stamped into
    the body, the HTML, or the headers.
    """

    def __init__(self, path: str, *, field_map: Optional[Dict[str, str]] = None) -> None:
        if not path:
            raise AdapterError("the mailbox readback adapter needs a path")
        self.path = Path(path)
        self.field_map = dict(field_map) if field_map is not None else dict(MAILBOX_FIELD_MAP)
        self.field_map.pop("ticket_id", None)
        self.field_map.pop("tag", None)
        self.warnings: List[str] = []
        self._reset()

    def _reset(self) -> None:
        self._offset = 0
        self._line_no = 0
        self._identity: Optional[Tuple[int, int]] = None
        self._mtime_ns: Optional[int] = None
        self._records: List[Tuple[Optional[str], Dict[str, Any]]] = []
        self._by_tag: Dict[str, List[Dict[str, Any]]] = {}
        self._warned = False
        #: True when the last line read had no newline yet, so a newline
        #: arriving next closes that line rather than starting a blank one.
        self._open_line = False

    def preflight(self) -> None:
        """The sink file may not exist yet, but the directory it will be
        written into must, and the path must not be a directory."""
        if self.path.is_dir():
            raise AdapterError(f"mailbox readback path {self.path} is a directory, not a file")
        parent = self.path.parent if str(self.path.parent) else Path(".")
        if not parent.is_dir():
            raise AdapterError(
                f"mailbox readback path {self.path}: the directory {parent} does "
                "not exist, so the pipeline cannot be writing a sink there"
            )
        if self.path.exists() and not os.access(self.path, os.R_OK):
            raise AdapterError(f"mailbox readback path {self.path} is not readable")

    def _refresh(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            # A sink that does not exist yet is a sink with nothing in it.
            if self._offset:
                self._reset()
            return
        identity = (stat.st_dev, stat.st_ino)
        replaced = self._identity is not None and identity != self._identity
        shrank = stat.st_size < self._offset
        rewritten = (
            stat.st_size == self._offset
            and self._mtime_ns is not None
            and stat.st_mtime_ns != self._mtime_ns
            and self._offset > 0
        )
        if replaced or shrank or rewritten:
            self._reset()
        self._identity = identity
        self._mtime_ns = stat.st_mtime_ns
        if stat.st_size == self._offset:
            return
        with self.path.open("rb") as handle:
            handle.seek(self._offset)
            chunk = handle.read(stat.st_size - self._offset)
        self._consume(chunk)

    def _consume(self, chunk: bytes) -> None:
        """Parse every complete line in `chunk`, advancing the offset past
        each. An unterminated last line is parsed if it is already valid JSON
        (a writer that never ends the file with a newline) and otherwise left
        for the next fetch."""
        lines = chunk.split(b"\n")
        for position, line_bytes in enumerate(lines):
            terminated = position < len(lines) - 1
            if not terminated and not line_bytes:
                break
            text = line_bytes.decode("utf-8", errors="replace").strip()
            continued = self._open_line and position == 0
            if not text:
                if terminated:
                    self._offset += len(line_bytes) + 1
                    if not continued:
                        self._line_no += 1
                    self._open_line = False
                continue
            line_no = self._line_no + 1
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                if not terminated:
                    # A write in progress. Leave it for the next fetch.
                    break
                raise AdapterError(
                    f"{self.path}:{line_no} is not valid JSON: {exc}. A partially "
                    "written last line is expected while a run is in flight; a "
                    "corrupt line anywhere else is not."
                ) from exc
            if not isinstance(record, dict):
                raise AdapterError(
                    f"{self.path}:{line_no} must be a JSON object, got "
                    f"{type(record).__name__}"
                )
            self._line_no = line_no
            self._offset += len(line_bytes) + (1 if terminated else 0)
            self._open_line = not terminated
            tag = self._tag_of(record)
            self._records.append((tag, record))
            if tag:
                self._by_tag.setdefault(tag, []).append(record)
        if self._line_no > MAILBOX_WARN_LINES and not self._warned:
            self._warned = True
            message = (
                f"mailbox sink {self.path} holds {self._line_no} lines. Each run "
                "reads all of it; rotate or truncate the sink between runs to "
                "keep readback fast."
            )
            self.warnings.append(message)
            warnings.warn(message, ReadbackWarning, stacklevel=3)

    def _to_readback(self, record: Dict[str, Any], tag: str) -> Readback:
        values: Dict[str, Any] = {}
        present: List[str] = []
        notes: List[Tuple[str, str]] = []
        for name, key in self.field_map.items():
            found = _dig(record, key)
            if found is _MISSING:
                continue
            readable, value, note = _coerce(name, key, found)
            if readable:
                present.append(name)
                values[name] = value
            elif note:
                notes.append((name, note))
        references = values.get("references")
        if isinstance(references, str):
            references = references.split()
        return Readback(
            exists=True,
            ticket_id=str(
                record.get("id")
                or record.get("message_id")
                or record.get("delivery_id")
                or f"{tag}@{self.path.stem}"
            ),
            from_addr=values.get("from_addr"),
            subject=values.get("subject"),
            body=values.get("body"),
            attachment_names=normalize_attachment_names(values.get("attachment_names")),
            route=values.get("route"),
            message_id=values.get("message_id"),
            in_reply_to=values.get("in_reply_to"),
            references=normalize_message_ids(references),
            tag=tag,
            category=values.get("category"),
            priority=values.get("priority"),
            fields=tuple(present),
            field_notes=tuple(notes),
        )

    @staticmethod
    def _tag_of(record: Dict[str, Any]) -> Optional[str]:
        literal = record.get("tag")
        if isinstance(literal, str) and literal.strip():
            return literal.strip()
        headers = record.get("headers")
        if isinstance(headers, dict):
            headers = "".join(f"{k}: {v}\n" for k, v in headers.items())
        body, html = record.get("body"), record.get("html")
        return harvest_tag(
            body if isinstance(body, str) else None,
            html if isinstance(html, str) else None,
            headers if isinstance(headers, str) else None,
        )

    def fetch(self, probe) -> FetchResult:
        self._refresh()
        return FetchResult(
            [self._to_readback(record, probe.tag) for record in self._by_tag.get(probe.tag, [])]
        )

    def list_all(self) -> FetchResult:
        self._refresh()
        return FetchResult(
            [self._to_readback(record, tag or "") for tag, record in self._records]
        )

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Building whatever the operator pointed at
# ---------------------------------------------------------------------------


def build_adapter(
    config: ReadbackConfig,
    *,
    client: Any = None,
    config_dir: Optional[str] = None,
) -> ReadbackAdapter:
    """Turn a parsed `[readback]` config into a live adapter.

    `client` is the injectable HTTP client, threaded only into the http
    adapter so tests never open a socket. `config_dir` resolves a relative
    mailbox `path` against the directory holding the config file; when it is
    None, `config.config_dir` (set by the CLI loader) is used. Production
    callers go through `pipeline.common.prepare_readback`, which passes it.
    """
    if config.spec:
        return _build_from_spec(config)

    if config.kind == "http":
        if not config.url:
            raise AdapterError(
                "the http readback adapter needs [readback].url, for example "
                'url = "http://localhost:8000/tickets"'
            )
        return HttpJsonAdapter(
            config.url,
            tag_param=config.tag_param,
            items_key=config.items_key,
            timeout=config.timeout,
            field_map=config.field_map(),
            client=client,
            allow_public_hosts=config.allow_public_hosts,
            list_path=config.list_path,
            enumerate_records=config.enumerate_records,
            headers=config.headers,
            query=config.query,
            method=config.method,
            body=config.body,
            page_param=config.page_param,
            page_size_param=config.page_size_param,
            next_path=config.next_path,
            max_pages=config.max_pages,
            retry_max=config.retry_max,
            retry_base_seconds=config.retry_base_seconds,
            rate=config.rate,
        )

    if config.kind == "mailbox":
        if not config.path:
            raise AdapterError(
                "the mailbox readback adapter needs [readback].path, pointing at "
                "a JSON Lines file of delivered messages"
            )
        path = config.path
        if config_dir is None:
            config_dir = config.config_dir
        if config_dir and not Path(path).is_absolute():
            path = str(Path(config_dir) / path)
        field_map = dict(MAILBOX_FIELD_MAP)
        field_map.update(config.fields or {})
        return MailboxAdapter(
            path, field_map={k: v for k, v in field_map.items() if v is not False}
        )

    raise AdapterError(
        f"unknown readback kind {config.kind!r}; use one of {list(BUILTIN_KINDS)} "
        "or set 'kind' to a module:attribute import path"
    )


#: A single required parameter with this name marks a factory that takes the
#: `[readback]` table. Any other single parameter, whatever it is called,
#: marks a fetch function.
_CONFIG_PARAM = "config"

# What a spec target turned out to be.
_FACTORY_NO_ARGS = "factory-no-args"
_FACTORY_WITH_CONFIG = "factory-with-config"
_FETCH_FUNCTION = "fetch-function"


def _required_positional(target: Callable[..., Any]) -> List[str]:
    """The names of the positional parameters `target` requires, ignoring
    `*args`, `**kwargs`, keyword-only parameters and anything with a default."""
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):  # pragma: no cover - builtins without sigs
        return ["probe"]
    return [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        and parameter.default is parameter.empty
    ]


def _classify_spec_target(target: Callable[..., Any], spec: str) -> str:
    """Work out whether a spec target is a factory or a fetch function.

    No required parameter: a factory, called with nothing. One required
    parameter named `config`: a factory, called with the `[readback]` table.
    One required parameter of any other name: a fetch function taking a
    Probe, so `lambda p: ...` works. More than one: refused, because the tool
    would call it with one argument and it would fail after sending.
    """
    required = _required_positional(target)
    if not required:
        return _FACTORY_NO_ARGS
    if len(required) == 1:
        if required[0] == _CONFIG_PARAM:
            return _FACTORY_WITH_CONFIG
        return _FETCH_FUNCTION
    raise AdapterError(
        f"--readback spec {spec!r} requires {len(required)} arguments "
        f"({', '.join(required)}). A fetch function takes one Probe, a factory "
        "takes the [readback] table as 'config' or nothing at all."
    )


def _import_target(spec: str) -> Any:
    if ":" not in spec:
        raise AdapterError(
            f"--readback spec {spec!r} must be 'module:attribute', for example "
            "'mypkg.myadapters:build'"
        )
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise AdapterError(
            f"--readback spec {spec!r} must be 'module:attribute', for example "
            "'mypkg.myadapters:build'"
        )
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - any import failure is a refusal
        raise AdapterError(
            f"--readback spec {spec!r} could not import {module_name!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise AdapterError(
            f"--readback spec {spec!r}: {module_name!r} has no {attribute!r}"
        ) from exc


def _instantiate(spec: str, cls: type, config: ReadbackConfig) -> Any:
    required = _required_positional(cls)
    if len(required) > 1:
        raise AdapterError(
            f"--readback spec {spec!r}: {cls.__name__}.__init__ requires "
            f"{len(required)} arguments; it may take the [readback] table or nothing"
        )
    if required:
        return _call_factory(spec, cls, config.adapter_table())
    return _call_factory(spec, cls)


def _build_from_spec(config: ReadbackConfig) -> ReadbackAdapter:
    """Import `module:attribute` and adapt whatever it turned out to be.

    A class is instantiated, with the `[readback]` (or `[loop.outbound]`)
    table when its `__init__` takes one argument, and a factory whose one
    parameter is `config` is called with the same table: the adapter's own
    keys as written plus the built-in ones (`ReadbackConfig.adapter_table`).
    An object with `fetch` is used as is. A callable is a
    factory or a fetch function, as `_classify_spec_target` decides. Anything
    that goes wrong here is an `AdapterError`, raised before anything is sent.

    The target is not run through the readback host guardrail: TestingHQ
    cannot see where arbitrary code connects, so the operator who wrote it is
    responsible for where it reads from.
    """
    spec = config.spec or ""
    target = _import_target(spec)

    if inspect.isclass(target):
        resolved = _instantiate(spec, target, config)
        if hasattr(resolved, "fetch"):
            return resolved
        raise AdapterError(
            f"--readback spec {spec!r}: an instance of {target.__name__} has no "
            "fetch(probe) method"
        )

    if hasattr(target, "fetch"):
        return target

    if not callable(target):
        raise AdapterError(
            f"--readback spec {spec!r} resolved to {type(target).__name__}, "
            "which has no fetch(probe) method and is not callable"
        )

    kind = _classify_spec_target(target, spec)
    if kind == _FETCH_FUNCTION:
        return FunctionAdapter(target)
    if kind == _FACTORY_NO_ARGS:
        resolved = _call_factory(spec, target)
    else:
        resolved = _call_factory(spec, target, config.adapter_table())

    if inspect.isclass(resolved):
        resolved = _instantiate(spec, resolved, config)
    if hasattr(resolved, "fetch"):
        return resolved
    if callable(resolved):
        return FunctionAdapter(resolved)
    raise AdapterError(
        f"--readback spec {spec!r}: the factory returned {type(resolved).__name__}, "
        "which has no fetch(probe) method and is not callable"
    )


def _call_factory(spec: str, factory: Callable[..., Any], *args) -> Any:
    """Call a factory once, reporting anything it raises as a factory failure
    with the original exception chained."""
    try:
        return factory(*args)
    except Exception as exc:  # noqa: BLE001 - any factory failure is reportable
        raise AdapterError(
            f"--readback spec {spec!r}: the factory raised {exc!r}. A readback "
            "factory is called with the [readback] config table, or with no "
            "arguments at all if it takes none."
        ) from exc
