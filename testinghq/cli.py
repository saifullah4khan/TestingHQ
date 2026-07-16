"""TestingHQ command line interface.

blast generate: builds a deterministic corpus to disk, no network ever.
blast fire: dry-run by default; sending requires both a configured --target
and an explicit --send. Rate limited, guardrails first-class.
blast replay: re-fires the exact corpus from a saved run artifact's seed and
config, byte-identically, using the same guardrail path as fire.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .blast.corrupt import DEFAULT_MIX, corrupt_corpus
from .blast.generate import generate_corpus
from .blast.payload import InboundEmail
from .blast.serialize import to_multipart_parts
from .core import guardrails, report
from .core.config import ConfigError, load_config
from .core.ratelimit import TokenBucket
from .core.transport import encode_multipart, post

DEFAULT_TARGET_CONFIG = "target.toml"
DEFAULT_RATE = 5.0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="testinghq",
        description="Self-testing tools for intake pipelines.",
    )
    parser.add_argument(
        "--version", action="version", version=f"testinghq {__version__}"
    )
    sub = parser.add_subparsers(dest="tool", required=True)

    blast = sub.add_parser("blast", help="generate and fire inbound-email payloads")
    blast_sub = blast.add_subparsers(dest="command", required=True)

    gen = blast_sub.add_parser("generate", help="generate a corpus to disk, no network")
    gen.add_argument("--count", type=int, default=100)
    gen.add_argument("--seed", type=int, default=0)
    gen.add_argument("--out", default="corpus")

    fire = blast_sub.add_parser("fire", help="generate and send to a configured target")
    fire.add_argument("--target")
    fire.add_argument("--seed", type=int, default=0)
    fire.add_argument("--count", type=int, default=100)
    fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    fire.add_argument(
        "--rate", type=float, default=DEFAULT_RATE, help="max requests per second"
    )
    fire.add_argument("--out", help="path to write the run artifact JSON")
    fire.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )

    replay = blast_sub.add_parser("replay", help="re-fire a saved run exactly")
    replay.add_argument("run", help="path to a previously written run artifact JSON")
    replay.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    replay.add_argument(
        "--rate", type=float, default=DEFAULT_RATE, help="max requests per second"
    )
    replay.add_argument("--out", help="path to write the run artifact JSON")
    replay.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )

    return parser


def _not_yet(command):
    print(
        f"testinghq blast {command}: not implemented yet (M0 skeleton)",
        file=sys.stderr,
    )
    return 2


# ---------------------------------------------------------------------------
# Shared corpus helpers
# ---------------------------------------------------------------------------


def _build_corpus(seed: int, count: int) -> List[Tuple[InboundEmail, str]]:
    """Generate `count` payloads from `seed`, corrupted per DEFAULT_MIX, and
    return (email, schema_category_label) pairs. This is the one place seed
    plus count becomes an actual corpus, for generate, fire, and replay
    alike, so all three stay deterministic and byte-identical for the same
    seed and count."""
    corpus = generate_corpus(seed, count)
    corrupted = corrupt_corpus(corpus, seed, DEFAULT_MIX)
    return [(email, report.category_label(category)) for email, category in corrupted]


def _mix_labels() -> List[str]:
    return [report.category_label(name) for name in DEFAULT_MIX.keys()]


def _category_tally(pairs: List[Tuple[InboundEmail, str]]) -> Dict[str, int]:
    tally = {label: 0 for label in report.CATEGORIES}
    for _email, label in pairs:
        if label in tally:
            tally[label] += 1
    return tally


def _print_dry_run_preview(pairs: List[Tuple[InboundEmail, str]], seed: int) -> None:
    tally = _category_tally(pairs)
    print(f"dry-run preview: {len(pairs)} payload(s), seed={seed}")
    for label in report.CATEGORIES:
        print(f"  {label}: {tally[label]}")
    print("no network calls were made (pass --send to fire for real)")


# ---------------------------------------------------------------------------
# guardrails.require_synthetic_content wiring
# ---------------------------------------------------------------------------


def _address_fields(email: InboundEmail) -> List[str]:
    return [email.to, email.from_addr, email.envelope.from_addr, *email.envelope.to]


def _require_synthetic_corpus(pairs: List[Tuple[InboundEmail, str]]) -> None:
    """Guardrail check before any network call: every address in every
    generated payload must look synthetic. Checked once for the whole
    corpus up front, so a bad payload aborts the run before anything is
    sent, rather than after some prefix of the corpus already fired."""
    fields: List[str] = []
    for email, _label in pairs:
        fields.extend(_address_fields(email))
    guardrails.require_synthetic_content(fields)


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _resolve_target_url(target_name: Optional[str], config_path: str) -> str:
    """Load the target config and resolve `target_name` to a URL, enforcing
    the canonical guardrail twice: once on the configured name (the
    allow-list check) and once on the resolved URL (the public-host check),
    mirroring web/adapter.py's pattern. A bare target name has no dot, so
    the public-host check on the name alone would pass vacuously; checking
    the resolved URL too is what makes that hardening actually bite.
    """
    if not target_name:
        raise guardrails.GuardrailError(
            "refusing to fire: --send requires --target"
        )
    config = load_config(config_path)
    allowed = config.allowed_target_names()
    guardrails.require_configured_target(target_name, allowed)
    url = config.get(target_name).url
    guardrails.require_configured_target(url, (url,))
    return url


# ---------------------------------------------------------------------------
# Firing
# ---------------------------------------------------------------------------


def _fire_corpus(
    pairs: List[Tuple[InboundEmail, str]],
    seed: int,
    url: str,
    rate: float,
    client=None,
) -> List[Dict[str, Any]]:
    """Fire every payload in `pairs` at `url`, paced by a token bucket at
    `rate` requests per second, and build one schema-shaped record per
    payload via report.build_record."""
    bucket = TokenBucket(rate_per_sec=rate, capacity=max(rate, 1.0))
    records = []
    for index, (email, label) in enumerate(pairs):
        bucket.acquire()
        result = post(email, url, client=client)
        response = {
            "status": result.status,
            "latency_ms": result.latency_ms,
            "body_snippet": result.body_snippet,
        }
        records.append(report.build_record(email, label, seed, index, response))
    return records


def _write_artifact(path: Optional[str], artifact: Dict[str, Any]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")


def _run_fire(
    pairs: List[Tuple[InboundEmail, str]],
    seed: int,
    count: int,
    target_name: Optional[str],
    rate: float,
    out: Optional[str],
    config_path: str,
    client=None,
) -> int:
    """The shared send path for `fire` and `replay`. Returns a process exit
    code. Never called unless guardrails.evaluate_send already said yes.
    `client` is None in real CLI use (transport.post then opens a real
    socket via UrllibHttpClient); tests call this directly with a fake
    client to stay hermetic, since main()/build_parser() intentionally
    expose no CLI flag for injecting one."""
    try:
        _require_synthetic_corpus(pairs)
        url = _resolve_target_url(target_name, config_path)
    except (guardrails.GuardrailError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    records = _fire_corpus(pairs, seed, url, rate, client=client)
    config_dict = {
        "mix": _mix_labels(),
        "count": count,
        "seed": seed,
        "dry_run": False,
        "target": target_name,
    }
    artifact = report.build_artifact(seed, config_dict, records)
    print(report.format_summary(artifact))
    _write_artifact(out, artifact)
    return 0


# ---------------------------------------------------------------------------
# blast generate
# ---------------------------------------------------------------------------


def _cmd_generate(args) -> int:
    pairs = _build_corpus(args.seed, args.count)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_items = []
    for index, (email, label) in enumerate(pairs):
        item_id = f"{label}-{args.seed}-{index:04d}"
        body = encode_multipart(to_multipart_parts(email))
        (out_dir / f"{item_id}.multipart").write_bytes(body)
        manifest_items.append(
            {
                "id": item_id,
                "category": label,
                "payload_sha256": report.payload_sha256(email),
            }
        )

    manifest = {
        "seed": args.seed,
        "count": args.count,
        "mix": _mix_labels(),
        "items": manifest_items,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8"
    )
    print(f"generate: wrote {len(pairs)} payload(s) to {out_dir}")
    return 0


# ---------------------------------------------------------------------------
# blast fire
# ---------------------------------------------------------------------------


def _cmd_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)
    print(f"fire: {decision.reason}")

    pairs = _build_corpus(args.seed, args.count)

    if not decision.will_send:
        _print_dry_run_preview(pairs, args.seed)
        if args.out:
            config_dict = {
                "mix": _mix_labels(),
                "count": args.count,
                "seed": args.seed,
                "dry_run": True,
                "target": None,
            }
            records = []
            for index, (email, label) in enumerate(pairs):
                response = {"status": None, "latency_ms": None, "body_snippet": ""}
                record = report.build_record(email, label, args.seed, index, response)
                # A dry run never gets a real response; the category rules
                # (clean must 2xx, degenerate must not 5xx/timeout) would
                # otherwise misread "no response" as a transport failure.
                # Dry-run records carry no assertion verdict at all.
                record["assertion"] = {"passed": True, "mismatches": []}
                records.append(record)
            artifact = {
                "seed": args.seed,
                "config": config_dict,
                "summary": {
                    "by_status_class": {"2xx": 0, "4xx": 0, "5xx": 0, "timeout": 0},
                    "by_category": _category_tally(pairs),
                    "flags": [],
                },
                "records": records,
            }
            _write_artifact(args.out, artifact)
        return 2

    return _run_fire(
        pairs, args.seed, args.count, args.target, args.rate, args.out, args.config
    )


# ---------------------------------------------------------------------------
# blast replay
# ---------------------------------------------------------------------------


def _cmd_replay(args) -> int:
    try:
        data = json.loads(Path(args.run).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"replay: could not read {args.run!r}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"replay: {args.run!r} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    seed = data.get("seed")
    config = data.get("config") or {}
    count = config.get("count")
    target_name = config.get("target")

    if seed is None or count is None:
        print(
            f"replay: {args.run!r} is missing seed or config.count, cannot "
            "reproduce the corpus",
            file=sys.stderr,
        )
        return 1

    pairs = _build_corpus(seed, count)

    original_records = data.get("records") or []
    mismatches = []
    for index, (email, _label) in enumerate(pairs):
        if index >= len(original_records):
            break
        original_hash = original_records[index].get("payload_sha256")
        recomputed_hash = report.payload_sha256(email)
        if original_hash and original_hash != recomputed_hash:
            mismatches.append(original_records[index].get("id", f"index {index}"))
    if mismatches:
        print(
            "replay: regenerated corpus is NOT byte-identical to the saved run "
            f"for record(s): {mismatches}. This means seed+config no longer "
            "reproduces the same payloads (a determinism bug), not that the "
            "target behaved differently.",
            file=sys.stderr,
        )
        return 1

    decision = guardrails.evaluate_send(args.send)
    print(f"replay: {decision.reason}")

    if not decision.will_send:
        _print_dry_run_preview(pairs, seed)
        return 2

    return _run_fire(pairs, seed, count, target_name, args.rate, args.out, args.config)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.tool == "blast":
        if args.command == "generate":
            return _cmd_generate(args)
        if args.command == "fire":
            return _cmd_fire(args)
        if args.command == "replay":
            return _cmd_replay(args)
        return _not_yet(args.command)
    return _not_yet(args.tool)


if __name__ == "__main__":
    raise SystemExit(main())
