"""Configuration loading for TestingHQ.

Targets are declared explicitly in a TOML config file, one `[targets.<name>]`
table per target. An HTTP target (the default `transport`) has a required
`url`, an optional wire `format` (default SendGrid Inbound Parse), an
optional `[targets.<name>.headers]` table of `env:` references and an optional
`envelope_to`. An SMTP target
(`transport = "smtp"`) has a required `host` and the SMTP keys instead, and
always sends the `mime` format. Both take an optional `public` flag (default
false, read only by Barrage). Environment variables may override a declared
HTTP target's url (but never invent a new one), so a target config can be
checked in while the real URL stays out of source control.

Loading fails loud: a missing file, unparsable TOML, a malformed `targets`
table, a target missing its `url` or `host`, an unknown `format` or
`transport`, a literal header value, a url carrying credentials or a
non-boolean `public` all raise ConfigError. Nothing falls back to a default
for a bad value.

Secrets are never read at load. A header value or an SMTP username or password
is an `env:NAME` reference here, checked for its shape; the variable itself is
read by `resolve_env_references` when a send path resolves the target, so a
command that sends nothing (`blast generate`, a dry run) does not need it set.

A key or table the schema in `config_schema.py` does not declare is refused
too, with the closest known name when one is close. Ignoring it would be the
worst failure a config file can have: `formt = "mailgun"` would load, look
fine, and send every message as SendGrid.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from . import config_schema
from .formats import format_names
from .transport import (
    DEFAULT_FORMAT,
    DEFAULT_SMTP_PORT,
    DEFAULT_TRANSPORT,
    TRANSPORT_HTTP,
    TRANSPORT_SMTP,
    TRANSPORTS,
)

#: The only format an SMTP target sends: the message itself.
SMTP_FORMAT = "mime"

#: Keys that only an SMTP target reads.
SMTP_KEYS = (
    "host", "port", "starttls", "username", "password", "envelope_from", "envelope_to",
)

#: The one envelope key an HTTP target reads as well: the envelope recipients
#: a format that carries an envelope puts on the wire. A receiver that routes
#: on the envelope, as Action Mailbox's SendGrid ingress does, needs it to name
#: a real mailbox.
HTTP_ENVELOPE_KEYS = ("envelope_to",)

#: The HTTP formats that carry the envelope (SendGrid's `envelope` field), and
#: so the only ones an HTTP target's `envelope_to` changes anything in.
ENVELOPE_FORMATS = ("sendgrid", "sendgrid-raw")


try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed. Config loading
    fails loud; it never silently produces an empty or partial Config."""


class UnknownKeyError(ConfigError):
    """A config file holds a key or table the schema does not declare.

    Its own class because a caller that tolerates a config it cannot load
    (a dry run with `--readback` given as a flag, say) must still refuse
    this one: the file is there and says something, just not what was meant.
    """


#: A secret-bearing value must start with this; the rest is the name of an
#: environment variable holding the value.
ENV_PREFIX = "env:"


def env_reference_problem(where: str, value: Any) -> Optional[str]:
    """Why `value` is not a usable `env:NAME` reference, or None when it is.
    `where` names the key in the message (`[readback.headers].Authorization`).

    The shape half of the rule `resolve_env_references` applies; config loading
    calls it alone so a literal is refused before anything else happens.
    """
    if not isinstance(value, str):
        return (
            f"{where} must be a string such as "
            f"\"{ENV_PREFIX}HQ_READBACK_TOKEN\", got {type(value).__name__}"
        )
    if not value.startswith(ENV_PREFIX):
        return (
            f"{where} is a literal value. Header values must come from the "
            f"environment as \"{ENV_PREFIX}<VARIABLE>\"; a config file is a "
            f"file that gets shared, and a secret in it is a secret that leaks"
        )
    if not value[len(ENV_PREFIX):].strip():
        return f"{where} names no environment variable after \"{ENV_PREFIX}\""
    return None


def resolve_env_references(
    table: str,
    raw: Any,
    env: Optional[Mapping[str, str]] = None,
    *,
    error: Callable[[str], Exception] = None,
) -> Dict[str, str]:
    """Turn a table of `env:NAME` references into the values to use.

    `table` is how the table is named in messages (`[readback.headers]`,
    `[targets.chatwoot.headers]`). Every value must be `env:NAME`: a literal
    is refused, not warned about, and so is a variable that is not set, here
    rather than at connect time, so a command refuses before it sends
    anything. `error` builds the exception raised (ConfigError by default).
    One implementation for every table that holds secrets, so the rule and
    its wording are the same wherever an operator meets them.
    """
    make = error or ConfigError
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise make(f"{table} must be a table, got {type(raw).__name__}")
    environ = os.environ if env is None else env

    resolved: Dict[str, str] = {}
    for name, value in raw.items():
        problem = env_reference_problem(f"{table}.{name}", value)
        if problem is not None:
            raise make(problem)
        variable = value[len(ENV_PREFIX):].strip()
        if variable not in environ:
            raise make(
                f"{table}.{name} refers to environment variable "
                f"{variable!r}, which is not set. The refusal is deliberate: a "
                f"run must not start without the credentials it was configured "
                f"to use, and must not discover that mid-run"
            )
        resolved[name] = environ[variable]
    return resolved


@dataclass(frozen=True)
class SmtpSettings:
    """The SMTP keys of a `transport = "smtp"` target, as the file wrote them.

    `username` and `password` are `env:NAME` references, never values; they
    are read when the target is resolved for a send. `envelope_from` and
    `envelope_to` are None and () when the file does not set them, meaning the
    payload's own envelope is used.
    """

    host: str
    port: int = DEFAULT_SMTP_PORT
    starttls: bool = False
    username: Optional[str] = None
    password: Optional[str] = None
    envelope_from: Optional[str] = None
    envelope_to: Tuple[str, ...] = ()

    def secret_references(self) -> Dict[str, str]:
        """{"username": "env:...", "password": "env:..."} for the ones set."""
        return {
            key: value for key, value in
            (("username", self.username), ("password", self.password))
            if value is not None
        }


@dataclass(frozen=True)
class Target:
    """A named, allowed firing target."""

    name: str
    url: str
    #: Which wire format to serialize this target's messages in. Defaults to
    #: SendGrid Inbound Parse, which is what every target was sent as before the
    #: format layer existed, so a config with no `format` key is unchanged.
    #: Validated here rather than at send time so a typo fails on load, with
    #: the list of valid names, instead of after a dry-run looked fine.
    wire_format: str = DEFAULT_FORMAT
    #: Barrage's half of the double opt-in for a public host: a load test at a
    #: public host needs `public = true` here as well as --allow-public-target.
    public: bool = False
    #: "http" (POST to `url`) or "smtp" (one SMTP transaction per message at
    #: `smtp.host`). For an SMTP target `url` is `smtp://host:port`, built by
    #: the loader, so every place that names or guards a target's host reads
    #: one field whatever the transport.
    transport: str = DEFAULT_TRANSPORT
    #: `[targets.<name>.headers]`: header name to `env:NAME` reference, added
    #: to every HTTP request at this target once resolved. Never values.
    headers: Mapping[str, str] = field(default_factory=dict, hash=False)
    #: The SMTP keys, for `transport = "smtp"` only.
    smtp: Optional[SmtpSettings] = None
    #: An HTTP target's `envelope_to`: the envelope recipients every payload is
    #: sent with, in place of its own. () means the payload's own. An SMTP
    #: target keeps its envelope in `smtp`.
    envelope_to: Tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError(f"target name must be a non-empty string, got {self.name!r}")
        if self.transport not in TRANSPORTS:
            raise ConfigError(
                f"target {self.name!r}: unknown transport {self.transport!r}; "
                f"known transports are {list(TRANSPORTS)}"
            )
        if not isinstance(self.url, str) or not self.url:
            raise ConfigError(
                f"target {self.name!r}: url must be a non-empty string, got {self.url!r}"
            )
        if self.transport == TRANSPORT_SMTP:
            if self.smtp is None:
                raise ConfigError(f"target {self.name!r}: an smtp target needs a host")
            if self.wire_format != SMTP_FORMAT:
                raise ConfigError(
                    f"target {self.name!r}: an smtp target sends the message "
                    f"itself, so its format must be {SMTP_FORMAT!r} or absent, "
                    f"got {self.wire_format!r}"
                )
            if self.headers:
                raise ConfigError(
                    f"target {self.name!r}: [targets.{self.name}.headers] is sent "
                    "with HTTP requests and an smtp target makes none; it "
                    "authenticates with username and password"
                )
        elif not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ConfigError(
                f"target {self.name!r}: url must start with http:// or https://, "
                f"got {self.url!r}"
            )
        if self.transport == TRANSPORT_HTTP:
            netloc = urlsplit(self.url).netloc
            if "@" in netloc:
                raise ConfigError(
                    f"target {self.name!r}: url carries credentials "
                    "(user:pass@host), which would be written to every artifact "
                    "and log that names the target. Remove them from the url and "
                    f"put the credential in [targets.{self.name}.headers] as an "
                    "env: reference, for example Authorization = "
                    "\"env:TARGET_AUTH\" with TARGET_AUTH holding "
                    "\"Basic <base64 of user:password>\" "
                    "(`testinghq config basic-auth <user>` prints it)"
                )
        if not isinstance(self.wire_format, str) or not self.wire_format:
            raise ConfigError(
                f"target {self.name!r}: format must be a non-empty string, got "
                f"{self.wire_format!r}"
            )
        if self.wire_format not in format_names():
            raise ConfigError(
                f"target {self.name!r}: unknown format {self.wire_format!r}; "
                f"known formats are {format_names()}"
            )
        if not isinstance(self.public, bool):
            raise ConfigError(
                f"target {self.name!r}: public must be true or false, got "
                f"{self.public!r}"
            )
        if (
            self.transport == TRANSPORT_HTTP and self.envelope_to
            and self.wire_format not in ENVELOPE_FORMATS
        ):
            raise ConfigError(
                f"target {self.name!r}: envelope_to on an http target is sent in "
                f"the format's envelope, and format {self.wire_format!r} carries "
                f"none (only {', '.join(ENVELOPE_FORMATS)} do), so it would "
                "change nothing"
            )


@dataclass(frozen=True)
class Config:
    """Loaded TestingHQ configuration."""

    targets: Dict[str, Target] = field(default_factory=dict)
    readback: Optional[Any] = None

    def target_urls(self):
        return [t.url for t in self.targets.values()]

    def allowed_target_names(self):
        """Target names, suitable as the `allowed_targets` argument to
        guardrails.require_configured_target."""
        return list(self.targets.keys())

    def readback_table(self):
        """The raw `[readback]` table, or None when the file declares none.

        Held raw rather than parsed, and typed `Any`, on purpose: the shape and
        the meaning of that table belong to testinghq/pipeline/adapters.py, and
        parsing it here would make the core config loader know about a tool
        that did not exist when it was written. The cast-free Optional[Any] is
        what lets that stay true without importing the pipeline package into
        core.
        """
        return self.readback

    def get(self, name: str) -> Target:
        try:
            return self.targets[name]
        except KeyError:
            raise ConfigError(
                f"unconfigured target {name!r}; configured targets: "
                f"{sorted(self.targets)}"
            ) from None


def _env_key(name: str) -> str:
    """Map a target name to the environment variable suffix that can
    override its url: non-alphanumeric characters become underscores, and
    the result is upper-cased. e.g. "my-target" -> "MY_TARGET"."""
    return "".join(ch if ch.isalnum() else "_" for ch in name).upper()


def _is_custom_adapter(values: Mapping[str, Any]) -> bool:
    """True when a readback-style table names a `module:attribute` adapter,
    either as its `kind` or as `spec`."""
    kind = values.get("kind")
    if isinstance(kind, str) and ":" in kind:
        return True
    return values.get("spec") is not None


def _unknown(where: str, key: str, known: Sequence[str]) -> str:
    """One refusal line: where, which key, and the closest known name."""
    close = difflib.get_close_matches(key, list(known), n=1)
    if close:
        return f"{where} has unknown key {key!r} (did you mean {close[0]!r}?)"
    return f"{where} has unknown key {key!r} (known keys: {', '.join(known)})"


def _check_table(
    dotted: str, table: "config_schema.Table", values: Mapping[str, Any],
    problems: List[str],
) -> None:
    """Check one table's key names. `dotted` is its name as the file spells
    it (`targets.local`, `loop.outbound`), for the message."""
    if table.free_form:
        # `[readback.fields]` names readback fields and `[readback.headers]`
        # names request headers: data, not a fixed set. The adapter loader
        # checks field names itself, and header names are the operator's.
        return
    known = config_schema.known_keys(table)
    open_for_adapter = (
        config_schema.accepts_custom_adapter_keys(table) and _is_custom_adapter(values)
    )
    for key, value in values.items():
        child = config_schema.child_table(table, key)
        if child is not None:
            if isinstance(value, dict):
                _check_table(f"{dotted}.{key}", child, value, problems)
            continue
        if key in known or open_for_adapter:
            continue
        problems.append(_unknown(f"[{dotted}]", key, known))


def unknown_key_problems(raw: Mapping[str, Any]) -> List[str]:
    """Every key or table in a parsed config file that the schema does not
    declare, one line each, in file order. Empty for a clean file.

    Only names are checked here. Values, and the shape of a table (a
    `targets` that is not a table, say), are left to the loaders that read
    them, so a malformed value is reported by the code that would run it.
    """
    problems: List[str] = []
    top = config_schema.top_level_names()
    for key, value in raw.items():
        if key not in top:
            close = difflib.get_close_matches(key, top, n=1)
            what = f"unknown table [{key}]" if isinstance(value, dict) else (
                f"unknown top-level key {key!r}"
            )
            hint = (
                f"did you mean [{close[0]}]?" if close
                else f"known tables: {', '.join(f'[{t}]' for t in top)}"
            )
            problems.append(f"{what} ({hint})")
            continue
        if not isinstance(value, dict):
            continue
        if key == config_schema.TARGETS.name:
            for name, entry in value.items():
                if isinstance(entry, dict):
                    _check_table(
                        f"targets.{name}", config_schema.TARGETS, entry, problems
                    )
            continue
        table = config_schema.table_named(key)
        if table is not None:
            _check_table(key, table, value, problems)
    return problems


def check_known_keys(raw: Mapping[str, Any], source: Union[str, Path]) -> None:
    """Refuse a parsed config file holding a key or table the schema does not
    declare. `source` is the file, named in the message.

    Refused rather than warned about: a warning scrolls past, and the run it
    precedes has already sent with the default the typo fell back to. A custom
    readback adapter (`kind = "module:attribute"`) may carry keys of its own,
    which the schema cannot know, so those tables accept extra keys and only
    those.
    """
    problems = unknown_key_problems(raw)
    if not problems:
        return
    raise UnknownKeyError(
        f"config file {source}: " + "; ".join(problems) + ". An unknown key is "
        "refused rather than ignored, because an ignored typo runs with the "
        "default; docs/CONFIG.md lists every key"
    )


def _load_target(
    config_path: Path, name: str, entry: Mapping[str, Any], env: Mapping[str, str]
) -> Target:
    """One `[targets.<name>]` table as a Target, or ConfigError naming the
    file and the key. Type errors are caught here rather than in
    `Target.__post_init__`, because the dataclass would report them without
    the config file or the target name, which are the two things that make
    the message useful."""
    where = f"config file {config_path}: targets.{name}"

    transport = entry.get("transport", DEFAULT_TRANSPORT)
    if not isinstance(transport, str):
        raise ConfigError(f"{where}.transport must be a string, got {type(transport).__name__}")
    if transport not in TRANSPORTS:
        raise ConfigError(
            f"{where}: unknown transport {transport!r}; known transports are "
            f"{list(TRANSPORTS)}"
        )

    public = entry.get("public", False)
    if not isinstance(public, bool):
        raise ConfigError(
            f"{where}.public must be true or false, got {type(public).__name__}"
        )

    # Absent means the transport's default, checked in `Target.__post_init__`
    # against the live registry.
    default_format = SMTP_FORMAT if transport == TRANSPORT_SMTP else DEFAULT_FORMAT
    wire_format = entry.get("format", default_format)
    if not isinstance(wire_format, str):
        raise ConfigError(
            f"{where}.format must be a string, got {type(wire_format).__name__}"
        )

    env_key = f"TESTINGHQ_TARGET_{_env_key(name)}_URL"

    if transport == TRANSPORT_SMTP:
        if "url" in entry:
            raise ConfigError(
                f"{where}: url is for an http target; an smtp target takes "
                "host and port"
            )
        if env_key in env:
            raise ConfigError(
                f"{where}: {env_key} is set, but it replaces the url of an http "
                "target and this one is an smtp target. Unset it, or change "
                "host and port in the file"
            )
        if "headers" in entry:
            raise ConfigError(
                f"{where}: [targets.{name}.headers] is sent with HTTP requests "
                "and an smtp target makes none; it authenticates with username "
                "and password"
            )
        smtp = _load_smtp(where, entry)
        return Target(
            name=name, url=f"smtp://{_host_for_url(smtp.host)}:{smtp.port}",
            wire_format=wire_format, public=public, transport=transport, smtp=smtp,
        )

    stray = [key for key in SMTP_KEYS if key in entry and key not in HTTP_ENVELOPE_KEYS]
    if stray:
        raise ConfigError(
            f"{where}: {', '.join(stray)} {'is an smtp key' if len(stray) == 1 else 'are smtp keys'}"
            f", and this target's transport is http. Add transport = \"smtp\" "
            "or remove them"
        )
    if "url" not in entry:
        raise ConfigError(f"{where} is missing required key 'url'")
    url = entry["url"]
    if not isinstance(url, str):
        raise ConfigError(f"{where}.url must be a string, got {type(url).__name__}")
    if env_key in env:
        url = env[env_key]

    headers = _load_header_references(f"[targets.{name}.headers]", entry.get("headers"))
    return Target(
        name=name, url=url, wire_format=wire_format, public=public,
        transport=transport, headers=headers,
        envelope_to=_load_envelope_to(where, entry),
    )


#: Headers the transport computes from the body. A target may not set them.
_COMPUTED_HEADERS = ("content-type", "content-length")


def _load_header_references(table: str, raw: Any) -> Dict[str, str]:
    """`[targets.<name>.headers]`, checked for shape: every value an `env:`
    reference, with the same rule and wording as `[readback.headers]`. The
    variables are read later, by `resolve_env_references`."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{table} must be a table, got {type(raw).__name__}")
    for name, value in raw.items():
        problem = env_reference_problem(f"{table}.{name}", value)
        if problem is not None:
            raise ConfigError(problem)
        if name.casefold() in _COMPUTED_HEADERS:
            raise ConfigError(
                f"{table}.{name}: the transport computes {name} from the body, "
                "so a target may not set it"
            )
        if not name or any(ch in name for ch in ":\r\n \t"):
            raise ConfigError(f"{table}: {name!r} is not a header name")
    return dict(raw)


def _host_for_url(host: str) -> str:
    """An IPv6 literal in brackets, so `smtp://[::1]:25` parses back."""
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _load_smtp(where: str, entry: Mapping[str, Any]) -> SmtpSettings:
    host = entry.get("host")
    if host is None:
        raise ConfigError(f"{where} is missing required key 'host' (transport = \"smtp\")")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f"{where}.host must be a non-empty string, got {host!r}")
    host = host.strip()
    if "/" in host or "@" in host or " " in host:
        raise ConfigError(
            f"{where}.host must be a bare host name or address, such as "
            f"\"mail.example.test\" or \"127.0.0.1\", got {host!r}"
        )

    port = entry.get("port", DEFAULT_SMTP_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError(f"{where}.port must be an integer from 1 to 65535, got {port!r}")

    starttls = entry.get("starttls", False)
    if not isinstance(starttls, bool):
        raise ConfigError(
            f"{where}.starttls must be true or false, got {type(starttls).__name__}"
        )

    references: Dict[str, Optional[str]] = {}
    for key in ("username", "password"):
        value = entry.get(key)
        if value is not None:
            problem = env_reference_problem(f"{where}.{key}", value)
            if problem is not None:
                raise ConfigError(problem.replace("Header values", "Credentials"))
        references[key] = value
    if references["password"] is not None and references["username"] is None:
        raise ConfigError(f"{where}: password is set without username")

    envelope_from = entry.get("envelope_from")
    if envelope_from is not None and not isinstance(envelope_from, str):
        raise ConfigError(
            f"{where}.envelope_from must be a string, got {type(envelope_from).__name__}"
        )
    return SmtpSettings(
        host=host, port=port, starttls=starttls,
        username=references["username"], password=references["password"],
        envelope_from=envelope_from, envelope_to=_load_envelope_to(where, entry),
    )


def _load_envelope_to(where: str, entry: Mapping[str, Any]) -> Tuple[str, ...]:
    """`envelope_to` as a tuple: one address or a list of them, () when absent."""
    envelope_to_raw = entry.get("envelope_to")
    if envelope_to_raw is None:
        return ()
    if isinstance(envelope_to_raw, str):
        envelope_to: Tuple[str, ...] = (envelope_to_raw,)
    elif isinstance(envelope_to_raw, list) and all(
        isinstance(item, str) for item in envelope_to_raw
    ):
        envelope_to = tuple(envelope_to_raw)
    else:
        raise ConfigError(
            f"{where}.envelope_to must be an address or a list of addresses, "
            f"got {envelope_to_raw!r}"
        )
    if not all(item.strip() for item in envelope_to):
        raise ConfigError(f"{where}.envelope_to holds an empty address")
    return envelope_to


def load_config(
    path: Union[str, Path], env: Optional[Mapping[str, str]] = None
) -> Config:
    """Load targets from a TOML config file, with environment overrides.

    File shape::

        [targets.local]
        url = "http://localhost:8000/inbound"

        [targets.staging]
        url = "https://staging.example.test/inbound"

    Environment overrides: `TESTINGHQ_TARGET_<NAME>_URL` replaces the url of
    the file-declared target `<name>` (see `_env_key` for the name mapping).
    `env` defaults to `os.environ`; pass an explicit mapping in tests so
    loading never depends on the real process environment.

    An optional `[readback]` table is accepted and carried through untouched as
    `Config.readback`. It describes how the pipeline tools read a system's
    output, which only those tools interpret, so this loader validates only
    that it is a table and leaves its contents to
    `testinghq/pipeline/adapters.py`.

    Raises ConfigError for: a missing file, unparsable TOML, a key or table
    the schema does not declare (see `check_known_keys`), a `targets` key
    that is not a table, a target entry that is not a table, a target
    entry missing `url` (or `host`, for an smtp target), a bad value for any
    target key (see `_load_target`), or a `readback` key that is not a table.
    `Target.__post_init__` further validates each url's shape.
    """
    if tomllib is None:  # pragma: no cover - depends on interpreter version
        raise ConfigError(
            "no TOML parser available: Python < 3.11 requires the 'tomli' package"
        )
    if env is None:
        env = os.environ

    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"config file {config_path} is not valid TOML: {exc}") from exc

    # First, so `[target.local]` (a typo for `targets`) is reported as the
    # typo it is, not as "no targets declared".
    check_known_keys(raw, config_path)

    targets_raw = raw.get("targets", {})
    if not isinstance(targets_raw, dict):
        raise ConfigError(
            f"config file {config_path}: 'targets' must be a table, "
            f"got {type(targets_raw).__name__}"
        )
    if not targets_raw:
        raise ConfigError(f"config file {config_path}: no targets declared under [targets]")

    targets: Dict[str, Target] = {}
    for name, entry in targets_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(
                f"config file {config_path}: targets.{name} must be a table, "
                f"got {type(entry).__name__}"
            )
        targets[name] = _load_target(config_path, name, entry, env)

    readback = raw.get("readback")
    if readback is not None and not isinstance(readback, dict):
        raise ConfigError(
            f"config file {config_path}: 'readback' must be a table, "
            f"got {type(readback).__name__}"
        )

    return Config(targets=targets, readback=readback)
