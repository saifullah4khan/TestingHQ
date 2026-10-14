"""Per-tool config tables and `env:` secrets for loop, steady and sigcheck.

`core/config.py` reads `[targets]` and `[readback]`; the `[loop]`, `[steady]`
and `[sigcheck]` tables are read here, by the tools that own them.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]

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
