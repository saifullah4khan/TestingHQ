"""Loading an adapter, plus the two that cover most systems without any code.

Writing an adapter is deliberately a small job, but "small" still means the user
has to write and import a class, and for the three cases the tools are aimed at
that is a pointless step. So there are built-ins for the two that can be done
with no dependencies:

  `http`     the system has an API. GET a URL with the tag as a query
             parameter, read a JSON body, map fields by name.
  `mailbox`  the system has an outbound mail sink. An append-only JSON Lines
             file, one delivered message per line, matched on the tag stamped
             into the body by `pipeline/messages.py`.

Everything else, including a database, is `--readback module:attribute`: an
import path, optionally followed by a factory to call with the `[readback]`
config table. The target may be a `ReadbackAdapter`, a plain callable taking a
`Probe`, or a callable returning a `ReadbackAdapter`. A one-liner lambda is
therefore a complete adapter, which is what makes the seam cheap enough that
nobody skips verification and goes back to trusting the status code.

THE SAFETY ARGUMENT. A readback URL is a place this tool will connect to and
read from, and on a real deployment that is the ticket store or the mail sink of
a live system, which may hold other people's data. So the URL goes through the
same canonical guardrail as a firing target, and it is checked in exactly one
place: `require_readback_target`. A readback pointing at a public host is
refused unless the operator passes `allow_public_hosts` deliberately, for the
same reason and with the same reasoning as `guardrails.require_configured_target`.
"""
from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..core import guardrails
from ..core.transport import ClientResponse, PreparedRequest, UrllibHttpClient
from .messages import harvest_tag
from .readback import (
    CHECKABLE_FIELDS,
    FunctionAdapter,
    Readback,
    ReadbackAdapter,
    ReadbackError,
    normalize_attachment_names,
    normalize_message_ids,
)

#: The built-in adapter kinds, and the `[readback]` keys each one requires.
#: Kept as data so `load_readback` can validate configuration before it tries to
#: import anything, and so `--help` and the docs can be generated from one list.
BUILTIN_KINDS: Tuple[str, ...] = ("http", "mailbox")

DEFAULT_HTTP_TAG_PARAM = "tag"
DEFAULT_HTTP_TIMEOUT = 10.0
DEFAULT_ITEMS_KEY = "items"

#: How each `Readback` field is read out of a JSON object, when the adapter's
#: config does not say otherwise. Dotted paths address nested objects, because
#: real APIs nest: `{"data": {"attributes": {"subject": "..."}}}` is more common
#: than a flat record, and an operator should not have to flatten their own API.
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
}


class AdapterError(ReadbackError):
    """Raised for a malformed `[readback]` config or an adapter that cannot be
    imported or built."""


def require_readback_target(url: str, *, allow_public_hosts: bool = False) -> str:
    """Gate a readback URL through the canonical guardrail.

    A single call site on purpose. This repository has already paid for a
    second copy of a safety rule that disagreed with the first, and the shape of
    that failure was a check that looked correct while being inert. The check
    here is the same check a firing target gets, applied to a different URL, and
    there is nowhere else in the tree that has to remember to do it.
    """
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise AdapterError(
            f"a readback url must start with http:// or https://, got {url!r}"
        )
    guardrails.require_configured_target(url, (url,), allow_public_hosts=allow_public_hosts)
    return url


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadbackConfig:
    """The `[readback]` table, parsed and validated."""

    kind: str
    url: Optional[str] = None
    path: Optional[str] = None
    tag_param: str = DEFAULT_HTTP_TAG_PARAM
    items_key: str = DEFAULT_ITEMS_KEY
    timeout: float = DEFAULT_HTTP_TIMEOUT
    fields: Dict[str, str] = field(default_factory=dict)
    spec: Optional[str] = None
    allow_public_hosts: bool = False
    list_path: Optional[str] = None
    enumerate_records: bool = True

    def field_map(self) -> Dict[str, str]:
        """The configured field map over the default one, so an operator who
        only renames `subject` does not have to restate the other nine."""
        merged = dict(DEFAULT_FIELD_MAP)
        merged.update(self.fields or {})
        return merged

    def to_json(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "path": self.path,
            "tag_param": self.tag_param,
            "items_key": self.items_key,
            "timeout": self.timeout,
            "field_map": self.field_map(),
            "spec": self.spec,
            "list_path": self.list_path,
            "enumerate": self.enumerate_records,
        }


def _as_str_table(value: Any, what: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AdapterError(f"[readback].{what} must be a table, got {type(value).__name__}")
    table = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise AdapterError(
                f"[readback].{what}.{key} must be a string, got {type(item).__name__}"
            )
        if key not in CHECKABLE_FIELDS and key not in DEFAULT_FIELD_MAP:
            raise AdapterError(
                f"[readback].{what} names {key!r}, which is not a readback "
                f"field; known fields are {sorted(DEFAULT_FIELD_MAP)}"
            )
        table[key] = item
    return table


def parse_readback_config(raw: Any) -> ReadbackConfig:
    """Build a ReadbackConfig from the raw `[readback]` table, or from a
    `kind` string when the config came from the command line.

    Accepts a bare string meaning "the built-in adapter of that kind with
    default settings", so `testinghq verify fire --readback http` works against
    a conventional API with no config file at all.
    """
    if isinstance(raw, str):
        # A bare string means "the built-in adapter of that kind with default
        # settings", unless it names an import path, in which case it is a spec.
        # One path for both, so `--readback mypkg.mine:build` and a `[readback]`
        # table with the same kind produce the same config.
        return ReadbackConfig(
            kind=raw, spec=raw if ":" in raw else None
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

    # A kind that names an import path is a spec, however it arrived. Deriving
    # `spec` from it here rather than at each call site means the flag form and
    # the config-table form produce the same config, and a custom adapter
    # written against one works with the other.
    spec = raw.get("spec")
    if spec is None and ":" in kind:
        spec = kind

    timeout = raw.get("timeout", DEFAULT_HTTP_TIMEOUT)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise AdapterError(
            f"[readback].timeout must be a number, got {type(timeout).__name__}"
        )

    enumerate_records = raw.get("enumerate", True)
    if not isinstance(enumerate_records, bool):
        raise AdapterError(
            f"[readback].enumerate must be true or false, got "
            f"{type(enumerate_records).__name__}"
        )

    return ReadbackConfig(
        kind=kind,
        url=raw.get("url"),
        path=raw.get("path"),
        tag_param=str(raw.get("tag_param", DEFAULT_HTTP_TAG_PARAM)),
        items_key=str(raw.get("items_key", DEFAULT_ITEMS_KEY)),
        timeout=float(timeout),
        fields=_as_str_table(raw.get("fields"), "fields"),
        spec=spec,
        allow_public_hosts=bool(raw.get("allow_public_hosts", False)),
        list_path=raw.get("list_path"),
        enumerate_records=enumerate_records,
    )


# ---------------------------------------------------------------------------
# Field extraction from a JSON object
# ---------------------------------------------------------------------------


def _dig(data: Any, path: str) -> Any:
    """Follow a dotted path into nested dicts and lists. Returns a
    `_MISSING` sentinel rather than None when the path is absent, so a field
    that is genuinely present and null is distinguishable from a field the
    response did not carry at all."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, (list, tuple)):
            if not part.lstrip("-").isdigit():
                return _MISSING
            index = int(part)
            if not -len(current) <= index < len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"


_MISSING = _Missing()


def readback_from_json(
    data: Dict[str, Any], field_map: Dict[str, str]
) -> Readback:
    """Map one JSON object to a Readback, recording exactly which fields the
    response actually carried.

    The `fields` tuple is the point of this function rather than an
    afterthought. An API that returns `{"id": "...", "subject": "..."}` did not
    tell us anything about the body, and the check for the body has to say so
    instead of comparing against a null it will never match.
    """
    if not isinstance(data, dict):
        raise AdapterError(
            f"a readback record must be a JSON object, got {type(data).__name__}"
        )

    values: Dict[str, Any] = {}
    present: List[str] = []
    for name, path in field_map.items():
        found = _dig(data, path)
        if found is _MISSING:
            continue
        present.append(name)
        values[name] = found

    if "ticket_id" not in present or values.get("ticket_id") in (None, ""):
        raise AdapterError(
            "a readback record must carry an identifier, so that one message "
            "producing two records can be told apart from one producing one. "
            "Say where it lives with [readback].fields, for example "
            'ticket_id = "data.id".'
        )

    references = values.get("references")
    if isinstance(references, str):
        references = references.split()

    return Readback(
        exists=True,
        ticket_id=str(values["ticket_id"]),
        from_addr=_as_optional_str(values.get("from_addr")),
        subject=_as_optional_str(values.get("subject")),
        body=_as_optional_str(values.get("body")),
        attachment_names=normalize_attachment_names(values.get("attachment_names")),
        route=_as_optional_str(values.get("route")),
        message_id=_as_optional_str(values.get("message_id")),
        in_reply_to=_as_optional_str(values.get("in_reply_to")),
        references=normalize_message_ids(references),
        tag=_as_optional_str(values.get("tag")),
        fields=tuple(present),
    )


def _as_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    raise AdapterError(
        f"a readback field must be a string or null, got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# The http adapter
# ---------------------------------------------------------------------------


def _with_query(url: str, tag: str, param: str) -> str:
    scheme, netloc, path, query, fragment = urlsplit(url)
    existing = query + "&" if query else ""
    return urlunsplit((scheme, netloc, path, existing + urlencode({param: tag}), fragment))


class HttpJsonAdapter:
    """Reads a system's output over its own JSON API.

    One GET per probe, with the tag as a query parameter, and every record the
    response carries is returned. A response body may be a bare list of
    records, a single object, or an object wrapping a list under
    `items_key`, because all three are what real APIs do and refusing two of
    them would push users into writing a custom adapter for a shape that is not
    worth custom code.

    The HTTP client is injectable for the same reason it is everywhere else in
    this package: tests must not open sockets. The default is
    `core.transport.UrllibHttpClient`, which is the only place in TestingHQ
    that opens one.
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
    ) -> None:
        if not url:
            raise AdapterError("the http readback adapter needs a url")
        self.url = require_readback_target(url, allow_public_hosts=allow_public_hosts)
        self.tag_param = tag_param
        self.items_key = items_key
        self.timeout = timeout
        self.field_map = field_map or dict(DEFAULT_FIELD_MAP)
        #: Where to ask for every record, when the system has a listing
        #: endpoint. Defaults to the lookup url with no tag, which is the
        #: shape most ticket APIs already have.
        self.list_path = list_path
        self.enumerate_records = enumerate_records
        self._client = client if client is not None else UrllibHttpClient()

    def _get(self, url: str) -> ClientResponse:
        return self._client.send(
            PreparedRequest(
                url=url, method="GET", headers={}, body=b"", timeout=self.timeout
            )
        )

    @staticmethod
    def _records(payload: Any, items_key: str) -> List[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            nested = payload.get(items_key)
            if isinstance(nested, list):
                return nested
            return [payload]
        raise AdapterError(
            "a readback response must be a JSON object or a list, got "
            f"{type(payload).__name__}"
        )

    def _decode(self, response: ClientResponse, context: str) -> List[Readback]:
        if not 200 <= response.status < 300:
            raise AdapterError(
                f"readback endpoint {self.url} returned {response.status} for "
                f"{context}: {response.body[:200]!r}"
            )
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterError(
                f"readback endpoint {self.url} did not return valid JSON: {exc}"
            ) from exc
        return [
            readback_from_json(record, self.field_map)
            for record in self._records(payload, self.items_key)
        ]

    def fetch(self, probe) -> List[Readback]:
        response = self._get(_with_query(self.url, probe.tag, self.tag_param))
        if response.status == 404:
            # A lookup that found nothing is an empty result, not an error: the
            # whole point of the tool is that "not found" is a finding.
            return []
        return self._decode(response, f"tag {probe.tag!r}")

    def list_all(self) -> List[Readback]:
        """Every record the endpoint holds, which is what makes a stray record
        findable.

        Enabled by default, because most ticket APIs already answer their lookup
        url with everything when the tag parameter is absent, and a ledger that
        can never search for strays is a ledger that can never report "extra".
        A system that cannot do this sets `enumerate_records = false`, and the
        tools then say strays were not searched rather than reporting none.
        """
        if not self.enumerate_records:
            raise ReadbackError(
                f"the readback endpoint {self.url} is not configured for "
                "enumeration, so stray records cannot be searched for. Set "
                "[readback].enumerate = true if it can answer a tagless request."
            )
        target = self.list_path or self.url
        if self.list_path is None and self.tag_param in target:
            target = target.split(f"{self.tag_param}=", 1)[0]
        return self._decode(self._get(target), "the full record list")

    def close(self) -> None:
        self._client = None


# ---------------------------------------------------------------------------
# The mailbox adapter
# ---------------------------------------------------------------------------


class MailboxAdapter:
    """Reads an outbound mail sink: an append-only JSON Lines file, one
    delivered message per line.

    The file is re-read on every fetch rather than cached, because a mail sink
    is written by the pipeline while TestingHQ is still running and a cached
    snapshot would report the pipeline as having produced nothing it had
    actually produced. N is a few hundred lines; the cost of correctness here is
    nil and the cost of a stale cache is a false loss report.

    Matching is by the tag stamped into the body, the HTML, or a `tag` field on
    the line, in that order. The last is not a TestingHQ field: many real sinks
    record a delivery id or a provider id instead, and a sink that happens to
    record the tag itself should not have to pretend it did not.
    """

    def __init__(self, path: str) -> None:
        if not path:
            raise AdapterError("the mailbox readback adapter needs a path")
        self.path = Path(path)

    def _lines(self) -> List[Dict[str, Any]]:
        if not self.path.is_file():
            # A sink that does not exist yet is a sink with nothing in it, not a
            # configuration error: the pipeline may not have written its first
            # delivery yet. Treating it as empty is what lets `--settle` do its
            # job.
            return []
        records = []
        for line_no, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
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
            records.append(record)
        return records

    def _to_readback(self, record: Dict[str, Any], tag: str) -> Readback:
        present = tuple(
            name
            for name, key in (
                ("from_addr", "from"),
                ("subject", "subject"),
                ("body", "body"),
                ("attachment_names", "attachments"),
                ("route", "route"),
                ("message_id", "message_id"),
                ("in_reply_to", "in_reply_to"),
                ("references", "references"),
            )
            if key in record
        )
        references = record.get("references")
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
            from_addr=record.get("from"),
            subject=record.get("subject"),
            body=record.get("body"),
            attachment_names=normalize_attachment_names(record.get("attachments")),
            route=record.get("route"),
            message_id=record.get("message_id"),
            in_reply_to=record.get("in_reply_to"),
            references=normalize_message_ids(references),
            tag=tag,
            fields=present,
        )

    def _tag_of(self, record: Dict[str, Any]) -> Optional[str]:
        """The tag a delivered line belongs to.

        A literal `tag` field is preferred, because some real sinks record a
        correlation id of their own and happen to record this one. Failing that,
        the tag is recovered from whatever text the line carries, in the
        documented search order, because a mail sink that has no idea TestingHQ
        put a tag anywhere is the normal case rather than the exotic one.
        """
        literal = record.get("tag")
        if isinstance(literal, str) and literal.strip():
            return literal.strip()
        return harvest_tag(
            record.get("body"),
            record.get("html"),
            record.get("headers"),
        )

    def _matching(self, tag: str) -> List[Dict[str, Any]]:
        return [record for record in self._lines() if self._tag_of(record) == tag]

    def fetch(self, probe) -> List[Readback]:
        return [
            self._to_readback(record, probe.tag) for record in self._matching(probe.tag)
        ]

    def list_all(self) -> List[Readback]:
        return [
            self._to_readback(record, self._tag_of(record) or "")
            for record in self._lines()
        ]

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

    `client` is the injectable HTTP client, threaded only into the http adapter
    so tests never open a socket. `config_dir` resolves a relative `path`
    against the directory holding the config file, because a sink at
    `mailbox.jsonl` almost never means the same thing relative to the current
    working directory as it does relative to the target config that names it.
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
        )

    if config.kind == "mailbox":
        if not config.path:
            raise AdapterError(
                "the mailbox readback adapter needs [readback].path, pointing at "
                "a JSON Lines file of delivered messages"
            )
        path = config.path
        if config_dir and not Path(path).is_absolute():
            path = str(Path(config_dir) / path)
        return MailboxAdapter(path)

    raise AdapterError(
        f"unknown readback kind {config.kind!r}; use one of {list(BUILTIN_KINDS)} "
        "or set 'kind' to a module:attribute import path"
    )


#: Parameter names that decide what a `module:attribute` spec resolved to.
#: Named rather than guessed from arity, because arity cannot tell a factory
#: taking a config table from a fetch function taking a Probe: both take one
#: argument, and calling a fetch function with a config dict raises whatever
#: the function happens to do with a dict, which is a message about the
#: function's internals rather than about the caller's mistake.
_CONFIG_PARAM = "config"
_PROBE_PARAM = "probe"

# What a spec target turned out to be.
_FACTORY_NO_ARGS = "factory-no-args"
_FACTORY_WITH_CONFIG = "factory-with-config"
_FETCH_FUNCTION = "fetch-function"


def _required_positional(target: Callable[..., Any]) -> List[str]:
    """The names of the positional parameters `target` requires, ignoring
    `*args`, `**kwargs` and anything with a default."""
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):  # pragma: no cover - builtins without sigs
        return [_CONFIG_PARAM]
    return [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
        and parameter.default is parameter.empty
    ]


def _classify_spec_target(target: Callable[..., Any], spec: str) -> str:
    """Work out whether a spec target is a factory or a fetch function."""
    required = _required_positional(target)
    if not required:
        return _FACTORY_NO_ARGS
    if len(required) > 1:
        # More than one required parameter cannot be a factory taking a single
        # config table, so it is a fetch function of some richer signature.
        return _FETCH_FUNCTION
    if required[0] == _PROBE_PARAM:
        return _FETCH_FUNCTION
    if required[0] == _CONFIG_PARAM:
        return _FACTORY_WITH_CONFIG
    raise AdapterError(
        f"--readback spec {spec!r}: cannot tell what {required[0]!r} is for. Name "
        f"the parameter 'probe' if it takes a Probe (a fetch function), or "
        f"'config' if it takes the [readback] config table (a factory), or take "
        "no arguments at all."
    )


def _build_from_spec(config: ReadbackConfig) -> ReadbackAdapter:
    """Import `module:attribute` and adapt whatever it turned out to be.

    The target may be a `ReadbackAdapter`, a factory taking the readback config
    table, a zero-argument factory, or a plain fetch function. The last is the
    cheap one, and the reason writing an adapter for a one-off system is a
    lambda rather than a project:

        # module:fetch, taking a Probe
        --readback mypkg.mine:lookup

        # module:build, taking the [readback] config table
        --readback mypkg.mine:build
    """
    spec = config.spec or ""
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
    except ImportError as exc:
        raise AdapterError(
            f"--readback spec {spec!r} could not import {module_name!r}: {exc}"
        ) from exc
    try:
        target = getattr(module, attribute)
    except AttributeError as exc:
        raise AdapterError(
            f"--readback spec {spec!r}: {module_name!r} has no {attribute!r}"
        ) from exc

    # An object that already answers `fetch` is an adapter, whatever else it
    # is. Checked before any calling, so a factory is never called by accident
    # and an adapter is never wrapped.
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
        resolved = _call_factory(spec, target, config.to_json())

    if hasattr(resolved, "fetch"):
        return resolved
    if callable(resolved):
        return FunctionAdapter(resolved)
    raise AdapterError(
        f"--readback spec {spec!r}: the factory returned {type(resolved).__name__}, "
        "which has no fetch(probe) method and is not callable"
    )


def _call_factory(spec: str, factory: Callable[..., Any], *args) -> Any:
    """Call a factory, reporting anything it raises as a factory failure with
    the original exception chained.

    The first version of this called the target and caught `TypeError`, which
    was wrong twice over: a factory that raised `TypeError` from inside itself
    would be called a second time with no arguments, so a side-effecting
    factory ran twice, and a factory that raised anything else was reported as
    "takes neither the config table nor no arguments", naming a problem the
    caller did not have.
    """
    try:
        return factory(*args)
    except Exception as exc:  # noqa: BLE001 - any factory failure is reportable
        raise AdapterError(
            f"--readback spec {spec!r}: the factory raised {exc!r}. A readback "
            "factory is called with the [readback] config table, or with no "
            "arguments at all if it takes none."
        ) from exc
