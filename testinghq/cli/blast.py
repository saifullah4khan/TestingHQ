from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..blast.corrupt import DEFAULT_MIX, corrupt_corpus
from ..blast.generate import generate_corpus
from ..blast.payload import InboundEmail
from ..blast.serialize import to_multipart_parts
from ..core import guardrails, report
from ..core.config import ConfigError
from ..core.ratelimit import TokenBucket
from ..core.transport import encode_multipart, post
from .common import DEFAULT_RATE, DEFAULT_TARGET_CONFIG, _resolve_target_url


def add_blast_parser(sub):
    """`blast generate`, `blast fire` and `blast replay`.

    Split out of `build_parser`, which used to construct this inline while every
    other command used a named builder. A reader looking for blast found it in
    the middle of a function whose name says nothing about it.
    """
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
    return blast


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


def _validate_send_plan(
    pairs: List[Tuple[InboundEmail, str]], target_name: Optional[str],
    config_path: str,
) -> Optional[str]:
    """Check that a send is permitted, returning the refusal or None.

    Callers run this before announcing anything, so that a command which will
    be refused does not first print what it was about to do.

    `_run_fire` repeats the same two checks. That duplication is deliberate:
    `_run_fire` is called directly by tests and by `blast replay`, so it cannot
    assume its caller validated.
    """
    try:
        _require_synthetic_corpus(pairs)
        _resolve_target_url(target_name, config_path)
    except (guardrails.GuardrailError, ConfigError) as exc:
        return str(exc)
    return None


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


def _cmd_fire(args) -> int:
    pairs = _build_corpus(args.seed, args.count)

    # The corpus is built first and a send is validated before anything is
    # announced, so that a refusal does not follow a line describing what the
    # command was about to do.
    #
    # Only a send is validated. A dry run must not need the target to resolve:
    # saying "I would send nothing" is the point of a dry run, and requiring a
    # configured target to say it makes the safe command the inconvenient one.
    if args.send:
        refusal = _validate_send_plan(pairs, args.target, args.config)
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            return 1

    decision = guardrails.evaluate_send(args.send)
    print(f"fire: {decision.reason}")

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


def _cmd_replay(args, client=None) -> int:
    """Replay a saved run. `client` is the injectable HTTP client, threaded
    into `_run_fire` exactly as the fire path does.

    It is here for the same reason it is on `_run_fire`: without it this
    function's send path opens real sockets and there is no way to test
    anything about it hermetically. That gap was found by writing the
    integration test for replay, which spent eighty-one seconds making forty
    real connection attempts before failing. `main()` passes no client, which
    is the real-network path, same as the fire path.
    """
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

    # Same rule as `fire`: a dry run needs no target, a send is validated
    # before anything is announced. The target comes from the saved artifact,
    # and the blast replay parser has no --target flag, so this cannot read
    # `args.target`.
    if args.send:
        refusal = _validate_send_plan(pairs, target_name, args.config)
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            return 1

    decision = guardrails.evaluate_send(args.send)
    print(f"replay: {decision.reason}")

    if not decision.will_send:
        _print_dry_run_preview(pairs, seed)
        return 2

    return _run_fire(
        pairs,
        seed,
        count,
        target_name,
        args.rate,
        args.out,
        args.config,
        client=client,
    )
