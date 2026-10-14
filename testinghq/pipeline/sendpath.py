"""Target resolution, readback setup and per-tool config tables for loop,
steady and sigcheck.

`resolve_send_target` calls `core.targets.resolve_target` when that module is
present and falls back to the pre-1.0 resolution otherwise. `open_readback`
calls `common.prepare_readback` (build, then preflight) when it exists, else
builds the adapter and calls its `preflight()` if it has one. `send_items`
passes the target's wire format to `common.send_all` when `send_all` accepts
one. Each fallback can be deleted once the shared version is in the tree.
"""
from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

from ..core.config import load_config
from ..core.formats import WireFormat, get_format
from ..core.transport import DEFAULT_BOUNDARY
from . import common
from .adapters import ReadbackConfig, build_adapter

ENV_PREFIX = "env:"


class ToolConfigError(ValueError):
    """A malformed tool table in the target config. A caller error."""


def read_tool_table(path: str, section: str) -> Dict[str, Any]:
    """The `[section]` table of the TOML file at `path`, or {} when absent.

    A missing or malformed file is refused. Nested tables (`[loop.outbound]`)
    come back inside their parent.
    """
    if tomllib is None:  # pragma: no cover - only on a build with no TOML
        raise ToolConfigError("no TOML reader is available (install tomli)")
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ToolConfigError(f"no such config file: {path!r}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ToolConfigError(f"{path} is not valid TOML: {exc}") from exc
    table = raw.get(section)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ToolConfigError(
            f"[{section}] must be a table, got {type(table).__name__}"
        )
    return table


def read_tool_table_if_present(path: Optional[str], section: str) -> Dict[str, Any]:
    """Like `read_tool_table`, but a file that does not exist reads as {}.
    For dry runs, which need no config file."""
    if not path or not os.path.exists(path):
        return {}
    return read_tool_table(path, section)


@dataclass(frozen=True)
class SendTarget:
    """Where to send, and in what wire format."""

    name: str
    url: str
    wire_format: WireFormat
    format_name: str
    public: bool = False

    def artifact_config(self) -> Dict[str, Any]:
        """The keys every artifact's `config` records about its target."""
        return {"format": self.format_name, "allow_public_target": self.public}


def resolve_send_target(
    target_name: Optional[str],
    config_path: str,
    *,
    allow_public_target: bool = False,
) -> SendTarget:
    """Resolve `target_name` through the shared resolver, refusing as it does."""
    try:
        from ..core.targets import resolve_target  # type: ignore[attr-defined]
    except ImportError:
        resolve_target = None

    if resolve_target is not None:
        resolved = resolve_target(
            target_name, config_path, allow_public_target=allow_public_target
        )
        return SendTarget(
            name=resolved.name,
            url=resolved.url,
            wire_format=resolved.wire_format,
            format_name=resolved.format_name,
            public=bool(resolved.public),
        )

    url = common.resolve_target_url(target_name, config_path)
    format_name = getattr(load_config(config_path).get(target_name), "wire_format", "sendgrid")
    return SendTarget(
        name=str(target_name),
        url=url,
        wire_format=get_format(format_name, DEFAULT_BOUNDARY),
        format_name=format_name,
        public=False,
    )


def send_items(items, target: SendTarget, rate: float, **kwargs):
    """`common.send_all` with the target's wire format when it takes one."""
    try:
        accepts = "wire_format" in inspect.signature(common.send_all).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        accepts = False
    if accepts:
        kwargs["wire_format"] = target.wire_format
    return common.send_all(items, target.url, rate, **kwargs)


def open_readback(readback: Any, *, client: Any = None, config_path: Optional[str] = None) -> Any:
    """A ready adapter for `readback`, preflighted, before anything is sent.

    `readback` is a `ReadbackConfig`, or an already-built adapter (anything with
    `fetch`), which is used as it is. Raises what building or preflight raises.
    """
    if not isinstance(readback, ReadbackConfig) and callable(getattr(readback, "fetch", None)):
        check = getattr(readback, "preflight", None)
        if callable(check):
            check()
        return readback
    prepare = getattr(common, "prepare_readback", None)
    if callable(prepare):
        return prepare(readback, client=client, config_path=config_path)
    config_dir = str(Path(config_path).resolve().parent) if config_path else None
    adapter = build_adapter(readback, client=client, config_dir=config_dir)
    check = getattr(adapter, "preflight", None)
    if callable(check):
        try:
            check()
        except Exception:
            common.close_adapter(adapter)
            raise
    return adapter


def readback_json(readback: Any) -> Dict[str, Any]:
    """What an artifact records about a readback: its config, or that an
    adapter object was handed in directly."""
    if isinstance(readback, ReadbackConfig):
        return readback.to_json()
    return {"kind": "adapter", "type": type(readback).__name__}


def is_unsettled(outcome: Any) -> bool:
    """True when the readback ran out of `--max-wait` before it settled."""
    flag = getattr(outcome, "unsettled", None)
    if isinstance(flag, bool):
        return flag
    return getattr(outcome, "stable", True) is False


def resolve_env_secret(
    value: Any, what: str, env: Optional[Mapping[str, str]] = None
) -> str:
    """The value of an `env:NAME` reference. A literal is refused."""
    environ = os.environ if env is None else env
    if not isinstance(value, str) or not value.startswith(ENV_PREFIX):
        raise ToolConfigError(
            f"{what} must be an {ENV_PREFIX}NAME reference to an environment "
            "variable, never a literal value"
        )
    name = value[len(ENV_PREFIX):].strip()
    if not name:
        raise ToolConfigError(f"{what} names no environment variable after {ENV_PREFIX!r}")
    if name not in environ:
        raise ToolConfigError(
            f"{what} refers to environment variable {name!r}, which is not set"
        )
    return environ[name]
