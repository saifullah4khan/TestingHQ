from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..blast.attachments import ATTACHMENT_MODES
from ..blast.corpus import (
    DEFAULT_ATTACHMENTS,
    DEFAULT_COUNT,
    CorpusError,
    CorpusPlan,
    parse_catalog,
    parse_mix,
)
from ..blast.payload import InboundEmail
from ..core import guardrails, report
from ..core.config import ConfigError, load_config
from ..core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_OK, EXIT_REFUSED
from ..core.formats import get_format
from ..core.ratelimit import TokenBucket
from ..core.targets import resolve_target, wire_format_for
from ..core.transport import DEFAULT_BOUNDARY, DEFAULT_FORMAT, post
from .common import (
    DEFAULT_RATE,
    DEFAULT_TARGET_CONFIG,
    add_send_args,
    resolve_for_send,
)


def _add_corpus_args(parser) -> None:
    """`--count`, `--seed`, `--mix`, `--catalog` and `--attachments`."""
    parser.add_argument(
        "--count", type=int, default=None,
        help=f"payloads to build (default {DEFAULT_COUNT})",
    )
    parser.add_argument("--seed", type=int, default=0, help="corpus seed (default 0)")
    parser.add_argument(
        "--mix",
        help="category percentages adding up to 100, e.g. "
        "clean=20,messy=40,multilingual=20,malformed=15,degenerate=5",
    )
    parser.add_argument(
        "--catalog", nargs="?", const="all", default=None, metavar="NAMES",
        help="build the named edge cases instead of the mix: every case, or a "
        "comma-separated list of case names",
    )
    parser.add_argument(
        "--attachments", choices=ATTACHMENT_MODES, default=None,
        help=f"attachments on about a third of payloads: none, seeded (clean "
        f"files) or adversarial (hostile names, zero-byte, oversized); "
        f"default {DEFAULT_ATTACHMENTS}",
    )


def add_blast_parser(sub):
    """`blast generate`, `blast fire` and `blast replay`."""
    blast = sub.add_parser("blast", help="generate and fire inbound-email payloads")
    blast_sub = blast.add_subparsers(dest="command", required=True)

    gen = blast_sub.add_parser("generate", help="generate a corpus to disk, no network")
    _add_corpus_args(gen)
    gen.add_argument("--out", default="corpus")
    gen.add_argument(
        "--target", help="write files in this configured target's wire format"
    )
    gen.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )

    fire = blast_sub.add_parser("fire", help="generate and send to a configured target")
    fire.add_argument("--target")
    _add_corpus_args(fire)
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
    fire.add_argument(
        "--fail-on-body", metavar="REGEX",
        help="flag a 2xx whose response body matches this regular expression",
    )
    add_send_args(fire)

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
    replay.add_argument(
        "--fail-on-body", metavar="REGEX",
        help="flag a 2xx whose body matches this (default: the saved run's pattern)",
    )
    add_send_args(replay)
    return blast


def _plan_from_args(args) -> CorpusPlan:
    """The corpus plan the command line asks for. Raises CorpusError."""
    if args.catalog is not None:
        clashes = [
            flag
            for flag, value in (
                ("--count", args.count), ("--mix", args.mix), ("--attachments", args.attachments),
            )
            if value is not None
        ]
        if clashes:
            raise CorpusError(
                "--catalog builds each named case exactly once and cannot be "
                f"combined with {', '.join(clashes)}"
            )
        return CorpusPlan(seed=args.seed, catalog=parse_catalog(args.catalog))
    return CorpusPlan(
        seed=args.seed,
        count=DEFAULT_COUNT if args.count is None else args.count,
        mix=parse_mix(args.mix) if args.mix is not None else None,
        attachments=args.attachments or DEFAULT_ATTACHMENTS,
    )


def _build_corpus(
    seed: int, count: int, plan: Optional[CorpusPlan] = None
) -> List[Tuple[InboundEmail, str]]:
    """(email, category label) pairs for `plan`, or for the default plan
    (default mix, seeded attachments) from `seed` and `count`. Generate,
    fire and replay all build through here, so the same plan is
    byte-identical in all three."""
    if plan is None:
        plan = CorpusPlan(seed=seed, count=count)
    return plan.build()


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
    attached = sum(1 for email, _label in pairs if email.attachments)
    print(f"  with attachments: {attached}")
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
    wire_format=None,
    *,
    cases: Optional[Tuple[str, ...]] = None,
    fail_on_body=None,
    records: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Fire every payload in `pairs` at `url`, paced by a token bucket at
    `rate` requests per second, and build one record per payload.

    Records are appended to `records` (a new list by default) as they
    complete, so a caller that catches KeyboardInterrupt keeps what was sent.
    `cases` names the catalog case of each payload; `fail_on_body` is a
    compiled `--fail-on-body` pattern.
    """
    bucket = TokenBucket(rate_per_sec=rate, capacity=max(rate, 1.0))
    if records is None:
        records = []
    for index, (email, label) in enumerate(pairs):
        bucket.acquire()
        result = post(email, url, client=client, wire_format=wire_format)
        response = report.response_from_transport(result, fail_on_body)
        case = cases[index] if cases is not None else None
        records.append(report.build_record(email, label, seed, index, response, case=case))
    return records


def _write_artifact(path: Optional[str], artifact: Dict[str, Any]) -> None:
    if not path:
        return
    Path(path).write_text(json.dumps(artifact, indent=2, sort_keys=False), encoding="utf-8")


def _validate_send_plan(
    pairs: List[Tuple[InboundEmail, str]], args, target_name: Optional[str],
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
        resolve_for_send(args, "blast", target_name=target_name)
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
    *,
    plan: Optional[CorpusPlan] = None,
    fail_on_body: Optional[str] = None,
    allow_public_target: bool = False,
) -> int:
    """The shared send path for `fire` and `replay`. Returns EXIT_FINDING
    when the run raised a flag or was interrupted, EXIT_OK when it is clean,
    EXIT_REFUSED when the send is not permitted.

    `plan` is what built `pairs` (default: the default plan for `seed` and
    `count`) and is recorded in the artifact. `client` is None in real CLI
    use; tests pass a fake one to stay hermetic. Ctrl-C stops the run, and
    the artifact records what was sent with `aborted: true`.
    """
    try:
        _require_synthetic_corpus(pairs)
        target = resolve_target(
            target_name, config_path, allow_public_target=allow_public_target
        )
        pattern = report.compile_body_pattern(fail_on_body)
    except (guardrails.GuardrailError, ConfigError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if plan is None:
        plan = CorpusPlan(seed=seed, count=count)
    records: List[Dict[str, Any]] = []
    aborted = False
    try:
        _fire_corpus(
            pairs, seed, target.url, rate, client=client,
            wire_format=target.wire_format, cases=plan.cases(),
            fail_on_body=pattern, records=records,
        )
    except KeyboardInterrupt:
        aborted = True
    config_dict = {
        **plan.config(),
        "dry_run": False,
        "target": target_name,
        "fail_on_body": fail_on_body,
        **target.artifact_config(),
    }
    artifact = report.build_artifact(seed, config_dict, records, aborted=aborted)
    print(report.format_summary(artifact))
    _write_artifact(out, artifact)
    if aborted or artifact["summary"]["flags"]:
        return EXIT_FINDING
    return EXIT_OK


def _refuse(message: str) -> int:
    print(f"refused: {message}", file=sys.stderr)
    return EXIT_REFUSED


#: File extension per wire format for `blast generate`.
_GENERATE_SUFFIX = {"postmark": ".json", "mime": ".eml"}


def _cmd_generate(args) -> int:
    try:
        plan = _plan_from_args(args)
    except CorpusError as exc:
        return _refuse(str(exc))
    format_name = DEFAULT_FORMAT
    wire_format = get_format(DEFAULT_FORMAT, DEFAULT_BOUNDARY)
    if getattr(args, "target", None):
        try:
            target = load_config(args.config).get(args.target)
        except ConfigError as exc:
            return _refuse(str(exc))
        format_name, wire_format = target.wire_format, wire_format_for(target)
    suffix = _GENERATE_SUFFIX.get(format_name, ".multipart")

    pairs = _build_corpus(plan.seed, plan.size, plan)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    cases = plan.cases()
    manifest_items = []
    for index, (email, label) in enumerate(pairs):
        item_id = f"{label}-{args.seed}-{index:04d}"
        body = wire_format.encode(email).body
        (out_dir / f"{item_id}{suffix}").write_bytes(body)
        item = {
            "id": item_id,
            "category": label,
            "payload_sha256": report.payload_sha256(email),
        }
        if cases is not None:
            item["case"] = cases[index]
        manifest_items.append(item)

    manifest = {
        "schema_version": report.SCHEMA_VERSION,
        "tool": report.TOOL,
        **plan.config(),
        "format": format_name,
        "items": manifest_items,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8"
    )
    print(f"generate: wrote {len(pairs)} payload(s) to {out_dir}")
    return EXIT_OK


def _dry_run_artifact(
    pairs: List[Tuple[InboundEmail, str]], plan: CorpusPlan, fail_on_body: Optional[str]
) -> Dict[str, Any]:
    """A dry run's artifact. Its records carry no response and no verdict:
    the endpoint was never asked."""
    cases = plan.cases()
    records = []
    for index, (email, label) in enumerate(pairs):
        response = {
            "status": None, "latency_ms": None, "body_snippet": "",
            "error": None, "error_kind": None,
        }
        case = cases[index] if cases is not None else None
        record = report.build_record(email, label, plan.seed, index, response, case=case)
        record["assertion"] = {"passed": True, "mismatches": []}
        records.append(record)
    config_dict = {
        **plan.config(),
        "dry_run": True,
        "target": None,
        "fail_on_body": fail_on_body,
    }
    return report.build_dry_run_artifact(plan.seed, config_dict, records)


def _cmd_fire(args) -> int:
    try:
        plan = _plan_from_args(args)
        report.compile_body_pattern(args.fail_on_body)
    except (CorpusError, ValueError) as exc:
        return _refuse(str(exc))
    pairs = _build_corpus(plan.seed, plan.size, plan)

    # The corpus is built first and a send is validated before anything is
    # announced, so a refusal does not follow a line describing what the
    # command was about to do. A dry run does not need the target to resolve.
    if args.send:
        refusal = _validate_send_plan(pairs, args, args.target)
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED

    decision = guardrails.evaluate_send(args.send)
    print(f"fire: {decision.reason}")

    if not decision.will_send:
        _print_dry_run_preview(pairs, args.seed)
        if args.out:
            _write_artifact(args.out, _dry_run_artifact(pairs, plan, args.fail_on_body))
        return EXIT_DRY_RUN

    return _run_fire(
        pairs, args.seed, plan.size, args.target, args.rate, args.out, args.config,
        plan=plan, fail_on_body=args.fail_on_body,
        allow_public_target=args.allow_public_target,
    )


def _cmd_replay(args, client=None) -> int:
    """Replay a saved run: rebuild its corpus from the recorded seed and plan,
    check it is byte-identical to the saved hashes, and fire it again.

    `client` is the injectable HTTP client, threaded into `_run_fire` as the
    fire path does; `main()` passes none, which is the real-network path.
    """
    try:
        data = json.loads(Path(args.run).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"replay: could not read {args.run!r}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except json.JSONDecodeError as exc:
        print(f"replay: {args.run!r} is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if not isinstance(data, dict):
        print(f"replay: {args.run!r} is not a blast run artifact", file=sys.stderr)
        return EXIT_REFUSED

    version = data.get("schema_version", 0)
    if not isinstance(version, int) or version > report.SCHEMA_VERSION:
        print(
            f"replay: {args.run!r} has schema_version {version!r}; this build "
            f"reads blast artifacts up to schema_version {report.SCHEMA_VERSION}",
            file=sys.stderr,
        )
        return EXIT_REFUSED

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
        return EXIT_REFUSED

    try:
        plan = CorpusPlan.from_config(seed, config)
    except CorpusError as exc:
        print(f"replay: {args.run!r} records a corpus that cannot be rebuilt: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    fail_on_body = args.fail_on_body
    if fail_on_body is None:
        fail_on_body = config.get("fail_on_body")
    try:
        report.compile_body_pattern(fail_on_body)
    except ValueError as exc:
        return _refuse(str(exc))
    pairs = _build_corpus(seed, plan.size, plan)

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
        return EXIT_REFUSED

    # Same rule as `fire`: a dry run needs no target, a send is validated
    # before anything is announced. The target comes from the saved artifact.
    if args.send:
        refusal = _validate_send_plan(pairs, args, target_name)
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            return EXIT_REFUSED

    decision = guardrails.evaluate_send(args.send)
    print(f"replay: {decision.reason}")

    if not decision.will_send:
        _print_dry_run_preview(pairs, seed)
        return EXIT_DRY_RUN

    return _run_fire(
        pairs,
        seed,
        plan.size,
        target_name,
        args.rate,
        args.out,
        args.config,
        client=client,
        plan=plan,
        fail_on_body=fail_on_body,
        allow_public_target=args.allow_public_target,
    )
