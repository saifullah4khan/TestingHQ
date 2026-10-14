"""`testinghq report --html`: one self-contained HTML file per run artifact.

`render_html` takes the normalised summary that `report --json` prints and,
optionally, the raw artifact it came from. The summary supplies the verdict,
counts, findings and exit code. The raw artifact, when given, supplies what the
summary deliberately leaves out: per-record rows, Barrage buckets and the config
block.

The page carries its CSS and JavaScript inline and a Content-Security-Policy
that forbids loading anything, so it opens from disk with no network and can be
attached to a CI run as a single file. Every value from the artifact is escaped.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from importlib import resources
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import __version__
from ..core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_OK, EXIT_REFUSED, MEANINGS

TEMPLATE_NAME = "template.html"
SCRIPT_NAME = "render.js"

#: Rows shown per table. The rest are counted, not dropped silently.
MAX_ROWS = 500
#: Columns shown in a record table, after flattening.
MAX_COLUMNS = 14
#: Characters shown per cell.
MAX_CELL = 240

#: Count names in display order. Anything else the summary carries follows.
COUNT_ORDER = (
    "sent", "found", "passed", "failed", "missing", "duplicated", "extra",
    "produced", "records", "scenarios", "buckets",
)

#: Keys that identify a record, shown first when present.
LEADING_COLUMNS = ("id", "tag", "scenario", "category", "kind", "outcome")

#: Where each tool keeps its per-record list, and what to call the table.
RECORD_SOURCES = (
    ("records", "Records"),
    ("messages", "Messages"),
    ("scenarios", "Scenarios"),
    ("variants", "Variants"),
)

_TONES = {
    EXIT_OK: "ok",
    EXIT_FINDING: "finding",
    EXIT_DRY_RUN: "dry",
    EXIT_REFUSED: "refused",
}


def script_path() -> Path:
    """Where the packaged render.js lives on disk, for the Node tests."""
    return Path(__file__).with_name(SCRIPT_NAME)


def _resource(name: str) -> str:
    return resources.files(__name__).joinpath(name).read_text(encoding="utf-8")


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _csp_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    if isinstance(value, (list, tuple)):
        if all(not isinstance(v, (dict, list, tuple)) for v in value) and len(value) <= 6:
            return ", ".join(_cell_text(v) for v in value)
        return f"[{len(value)} items]"
    if isinstance(value, dict):
        return "{" + f"{len(value)} keys" + "}"
    text = str(value)
    if len(text) > MAX_CELL:
        text = text[: MAX_CELL - 3] + "..."
    return text


def _flatten(record: Mapping[str, Any], prefix: str = "", depth: int = 0) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and depth < 1 and value:
            out.update(_flatten(value, prefix=f"{name}.", depth=depth + 1))
        else:
            out[name] = value
    return out


def _columns(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    seen: List[str] = []
    for row in rows:
        for key in row:
            if key not in seen and "sha256" not in key:
                seen.append(key)
    leading = [k for k in LEADING_COLUMNS if k in seen]
    rest = [k for k in seen if k not in leading]
    return (leading + rest)[:MAX_COLUMNS]


def _row_is_bad(record: Mapping[str, Any]) -> bool:
    """A row the artifact itself marks as failed. Reads recorded fields only."""
    if record.get("passed") is False:
        return True
    assertion = record.get("assertion")
    if isinstance(assertion, dict) and assertion.get("passed") is False:
        return True
    outcome = record.get("outcome")
    return isinstance(outcome, str) and "fail" in outcome


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _header(normalised: Mapping[str, Any]) -> str:
    meta = []
    tool = normalised.get("tool") or "unknown tool"
    meta.append(f"<span>tool {_esc(tool)}</span>")
    if normalised.get("target"):
        meta.append(f"<span>target {_esc(normalised['target'])}</span>")
    if normalised.get("seed") is not None:
        meta.append(f"<span>seed {_esc(normalised['seed'])}</span>")
    schema = normalised.get("schema_version")
    meta.append(
        f"<span>schema {_esc(schema) if schema is not None else '0 (not recorded)'}</span>"
    )
    return (
        '<header class="top"><h1>TestingHQ report</h1>'
        f'<div class="meta">{"".join(meta)}</div></header>'
    )


def _verdict(normalised: Mapping[str, Any]) -> str:
    verdict = normalised.get("verdict") or "UNKNOWN"
    code = _as_int(normalised.get("exit_code"))
    tone = _TONES.get(code, "unknown") if code is not None else "unknown"
    source = normalised.get("verdict_source")
    code_source = normalised.get("exit_code_source")
    meaning = MEANINGS.get(code, "unknown code") if code is not None else "not recorded"
    code_text = "?" if code is None else str(code)

    parts = [
        f'<section class="verdict-panel tone-{tone}" aria-label="Verdict">',
        '<div class="verdict">',
        f'<span class="verdict-word" data-verdict>{_esc(verdict)}</span>',
    ]
    if source:
        parts.append(f'<span class="source">verdict {_esc(source)}</span>')
    parts.append(
        f'<span class="exit" data-exit-code="{_esc(code_text)}">exit code '
        f"{_esc(code_text)}: {_esc(meaning)}"
        + (f" ({_esc(code_source)})" if code_source else "")
        + "</span>"
    )
    parts.append("</div>")
    if normalised.get("dry_run"):
        parts.append(
            '<p class="banner banner-dry">Dry run: nothing was sent. The '
            "counts describe what a real run would send.</p>"
        )
    if normalised.get("aborted"):
        parts.append(
            '<p class="banner banner-aborted">Aborted: the run was interrupted '
            "and these are the results collected before it stopped.</p>"
        )
    parts.append("</section>")
    return "".join(parts)


def _counts(normalised: Mapping[str, Any]) -> str:
    counts = normalised.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    names = [k for k in COUNT_ORDER if k in counts]
    names += sorted(k for k in counts if k not in COUNT_ORDER)
    shown = [(k, counts[k]) for k in names if counts[k]]
    body = ['<section aria-label="Counts"><h2>Counts</h2>']
    if not shown:
        body.append('<p class="empty">Every count is zero.</p>')
    else:
        body.append('<dl class="counts">')
        for name, value in shown:
            body.append(
                f'<div class="count" data-count="{_esc(name)}"><dt>{_esc(name)}</dt>'
                f"<dd>{_esc(value)}</dd></div>"
            )
        body.append("</dl>")
    body.append("</section>")
    return "".join(body)


def _findings(normalised: Mapping[str, Any]) -> str:
    findings = normalised.get("findings")
    findings = findings if isinstance(findings, list) else []
    parts = [
        f'<section aria-label="Findings"><h2>Findings ({len(findings)})</h2>'
    ]
    if not findings:
        parts.append('<p class="empty">No findings.</p>')
    else:
        parts.append(
            '<div class="table-wrap"><table id="findings" data-sortable>'
            '<thead><tr><th><button type="button">kind</button></th>'
            '<th><button type="button">detail</button></th></tr></thead><tbody>'
        )
        for finding in findings[:MAX_ROWS]:
            if isinstance(finding, dict):
                kind, detail = finding.get("kind"), finding.get("detail")
            else:
                kind, detail = "", finding
            parts.append(
                f'<tr><td class="kind">{_esc(kind)}</td>'
                f'<td class="long">{_esc(detail)}</td></tr>'
            )
        parts.append("</tbody></table></div>")
        if len(findings) > MAX_ROWS:
            parts.append(
                f'<p class="note">First {MAX_ROWS} of {len(findings)} findings shown.</p>'
            )
    parts.append("</section>")
    return "".join(parts)


def _table(table_id: str, title: str, records: Sequence[Any]) -> str:
    dicts = [r for r in records if isinstance(r, dict)]
    flat = [_flatten(r) for r in dicts[:MAX_ROWS]]
    columns = _columns(flat)
    parts = [
        f'<section aria-label="{_esc(title)}"><h2>{_esc(title)} ({len(dicts)})</h2>'
    ]
    if not flat or not columns:
        parts.append('<p class="empty">None recorded.</p></section>')
        return "".join(parts)
    parts.append(
        f'<input class="filter" type="search" data-filter="{_esc(table_id)}" '
        f'placeholder="Filter {_esc(title.lower())}" aria-label="Filter '
        f'{_esc(title.lower())}" hidden>'
    )
    parts.append(
        f'<div class="table-wrap"><table id="{_esc(table_id)}" data-sortable><thead><tr>'
    )
    for column in columns:
        parts.append(f'<th><button type="button">{_esc(column)}</button></th>')
    parts.append("</tr></thead><tbody>")
    for original, row in zip(dicts, flat):
        css = ' class="row-bad"' if _row_is_bad(original) else ""
        parts.append(f"<tr{css}>")
        for column in columns:
            text = _cell_text(row.get(column))
            css = ' class="long"' if len(text) > 40 else ""
            parts.append(f"<td{css}>{_esc(text)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    if len(dicts) > MAX_ROWS:
        parts.append(f'<p class="note">First {MAX_ROWS} of {len(dicts)} shown.</p>')
    parts.append("</section>")
    return "".join(parts)


def _records(artifact: Mapping[str, Any]) -> str:
    out = []
    for key, title in RECORD_SOURCES:
        value = artifact.get(key)
        if isinstance(value, list) and value:
            out.append(_table(f"table-{key}", title, value))
    regressions = artifact.get("regressions")
    if isinstance(regressions, dict):
        listed = regressions.get("records")
        if isinstance(listed, list) and listed:
            out.append(_table("table-regressions", "Regressions", listed))
    return "".join(out)


# ---------------------------------------------------------------------------
# Barrage chart
# ---------------------------------------------------------------------------

_X_KEYS = ("start", "start_seconds", "t", "at_seconds")
_RATE_KEYS = ("targeted_rps", "target_rps", "achieved_rps")

_W, _H = 640, 200
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 48, 12, 10, 26


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fmt(value: float) -> str:
    if value >= 100 or value == int(value):
        return str(int(round(value)))
    return f"{value:.1f}"


def _chart(title: str, xs: List[float], series: List[Tuple[str, List[Optional[float]]]],
           knee_at: Optional[float]) -> str:
    values = [v for _name, ys in series for v in ys if v is not None]
    if not values or not xs:
        return ""
    y_max = max(values) or 1.0
    x_min, x_max = min(xs), max(xs)
    x_span = (x_max - x_min) or 1.0
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B

    def px(x: float) -> float:
        return _PAD_L + (x - x_min) / x_span * plot_w

    def py(y: float) -> float:
        return _PAD_T + plot_h - (y / y_max) * plot_h

    parts = [
        f"<h3>{_esc(title)}</h3>",
        f'<svg class="chart" viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-label="{_esc(title)}">',
    ]
    for fraction in (0.0, 0.5, 1.0):
        y = py(y_max * fraction)
        css = "axis" if fraction == 0.0 else "gridline"
        parts.append(
            f'<line class="{css}" x1="{_PAD_L}" x2="{_W - _PAD_R}" '
            f'y1="{y:.1f}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text x="{_PAD_L - 6}" y="{y + 4:.1f}" text-anchor="end">'
            f"{_esc(_fmt(y_max * fraction))}</text>"
        )
    for x, anchor in ((x_min, "start"), (x_max, "end")):
        parts.append(
            f'<text x="{px(x):.1f}" y="{_H - 8}" text-anchor="{anchor}">'
            f"{_esc(_fmt(x))}s</text>"
        )
    if knee_at is not None and x_min <= knee_at <= x_max:
        kx = px(knee_at)
        parts.append(
            f'<line class="knee" x1="{kx:.1f}" x2="{kx:.1f}" y1="{_PAD_T}" '
            f'y2="{_PAD_T + plot_h}"/>'
        )
        parts.append(
            f'<text class="knee-label" x="{kx + 4:.1f}" y="{_PAD_T + 10}">knee</text>'
        )
    for index, (_name, ys) in enumerate(series):
        points = " ".join(
            f"{px(x):.1f},{py(y):.1f}" for x, y in zip(xs, ys) if y is not None
        )
        if points:
            parts.append(
                f'<polyline class="line series-{index % 4}" points="{points}"/>'
            )
    parts.append("</svg>")
    parts.append('<ul class="legend">')
    for index, (name, _ys) in enumerate(series):
        parts.append(f'<li class="series-{index % 4}">{_esc(name)}</li>')
    parts.append("</ul>")
    return "".join(parts)


def _barrage(artifact: Mapping[str, Any]) -> str:
    buckets = artifact.get("buckets")
    if not isinstance(buckets, list):
        return ""
    buckets = [b for b in buckets if isinstance(b, dict)]
    if not buckets:
        return ""

    x_key = next((k for k in _X_KEYS if k in buckets[0]), None)
    xs = [
        (_number(b.get(x_key)) if x_key else None) for b in buckets
    ]
    if any(x is None for x in xs):
        xs = [float(i) for i in range(len(buckets))]

    def series_for(keys: Sequence[str]) -> List[Tuple[str, List[Optional[float]]]]:
        out = []
        for key in keys:
            ys = [_number(b.get(key)) for b in buckets]
            if any(y is not None for y in ys):
                out.append((key, ys))
        return out

    rate_keys = [k for k in _RATE_KEYS if any(k in b for b in buckets)]
    latency_keys: List[str] = []
    for bucket in buckets:
        for key, value in bucket.items():
            if (
                key not in latency_keys
                and key.endswith("_ms")
                and _number(value) is not None
            ):
                latency_keys.append(key)
    latency_keys = latency_keys[:4]

    summary = artifact.get("summary")
    knee = summary.get("knee") if isinstance(summary, dict) else None
    knee_at = _number(knee.get("at_seconds")) if isinstance(knee, dict) else None

    charts = _chart("Throughput (requests per second)", xs, series_for(rate_keys), knee_at)
    charts += _chart("Latency (ms)", xs, series_for(latency_keys), knee_at)
    if not charts:
        return ""
    return (
        '<section class="charts" aria-label="Buckets chart">'
        f"<h2>Buckets ({len(buckets)})</h2>{charts}</section>"
        + _table("table-buckets", "Buckets", buckets)
    )


def _config(artifact: Mapping[str, Any]) -> str:
    config = artifact.get("config")
    if not isinstance(config, dict):
        return ""
    text = json.dumps(config, indent=2, sort_keys=True, default=str)
    return (
        '<section aria-label="Config"><h2>Config</h2>'
        f"<pre>{_esc(text)}</pre></section>"
    )


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def render_html(normalised: Mapping[str, Any],
                artifact: Optional[Mapping[str, Any]] = None) -> str:
    """The whole page as one string.

    `normalised` is the `report --json` shape. Keys added after 1.0 (such as
    `schema_version` and `aborted`) are optional, so an older summary renders.
    `artifact` is the raw file the summary was read from; without it the page
    shows the summary only.
    """
    if not isinstance(normalised, Mapping):
        raise TypeError("render_html needs the report --json summary as a dict")
    raw = artifact if isinstance(artifact, Mapping) else {}

    body = "".join(
        (
            _header(normalised),
            _verdict(normalised),
            _counts(normalised),
            _findings(normalised),
            _barrage(raw),
            _records(raw),
            _config(raw),
            f"<footer>Written by testinghq {_esc(__version__)}. Self-contained: "
            "this file loads nothing from the network.</footer>",
        )
    )

    script = _resource(SCRIPT_NAME)
    if "</script" in script.lower():
        raise ValueError("render.js must not contain a closing script tag")
    template = _resource(TEMPLATE_NAME)
    match = re.search(r"<style>(.*?)</style>", template, re.DOTALL)
    style = match.group(1) if match else ""
    csp = (
        "default-src 'none'; "
        f"style-src {_csp_hash(style)}; "
        f"script-src {_csp_hash(script)}; "
        "img-src data:; base-uri 'none'; form-action 'none'"
    )
    title = f"TestingHQ {normalised.get('tool') or 'report'}: {normalised.get('verdict') or 'UNKNOWN'}"
    return Template(template).substitute(
        csp=_esc(csp),
        version=_esc(__version__),
        title=_esc(title),
        body=body,
        script=script,
    )


def write_html(path: Path, normalised: Mapping[str, Any],
               artifact: Optional[Mapping[str, Any]] = None) -> Path:
    """Render and write the page. Returns the path written."""
    path = Path(path)
    path.write_text(render_html(normalised, artifact), encoding="utf-8")
    return path


__all__ = ["render_html", "script_path", "write_html"]
