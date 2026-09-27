"""TestingHQ command line interface.

blast generate: builds a deterministic corpus to disk, no network ever.
blast fire: dry-run by default; sending requires both a configured --target
and an explicit --send. Rate limited, guardrails first-class.
blast replay: re-fires the exact corpus from a saved run artifact's seed and
config, byte-identically, using the same guardrail path as fire.

barrage fire: load-tests a configured target by firing clean,
provider-shaped payloads at a high but controlled rate, then reports
throughput, latency percentiles, error rate over time, and the knee.
Barrage is a load tester against infrastructure the operator controls. It
is NOT an email sender, NOT a flooding tool, and NOT for endpoints you do
not own. Dry-run by default, configured targets only, and a hard
rate-and-duration ceiling that requires --allow-high-rate to raise.
barrage replay: re-runs a saved barrage run from its seed and config.

compare --baseline <run.json> --candidate <run.json>: reads two saved run
artifacts and reports only what changed between them, so a change to a parser
can be judged as an improvement or a regression rather than eyeballed at. It
reads two JSON files and prints. It never resolves a target, never opens a
socket, and has no --send, so there is nothing to gate. Exits non-zero when
the candidate introduced a regression, which makes it usable as a CI step.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .barrage import fire as barrage_fire
from .barrage.runner import RateCeilingError
from .blast.corrupt import DEFAULT_MIX, corrupt_corpus
from .blast.generate import generate_corpus
from .blast.payload import InboundEmail
from .blast.serialize import to_multipart_parts
from .compare import runs as compare_runs
from .core import guardrails, report
from .core.config import ConfigError, load_config
from .core.ratelimit import TokenBucket
from .core.transport import encode_multipart, post
from .pipeline import adapters as pipeline_adapters
from .pipeline import common
from .pipeline import ledger as pipeline_ledger
from .pipeline import messages as pipeline_messages
from .pipeline import redeliver as pipeline_redeliver
from .pipeline import verify as pipeline_verify
from .pipeline.adapters import AdapterError
from .pipeline.common import EXIT_DRY_RUN, EXIT_REFUSED
from .core.exit_codes import MEANINGS as _EXIT_MEANINGS
from .core.exit_codes import EXIT_FINDING as _EXIT_FINDING
from .core.exit_codes import EXIT_OK as _EXIT_OK

DEFAULT_TARGET_CONFIG = "target.toml"
DEFAULT_RATE = 5.0

#: Shown by every subcommand's --help, because the exit code is the answer a
#: script reads and there are seven tools. Generated from the shared table so it
#: cannot describe a convention the code no longer uses.
_EXIT_CODE_HELP = (
    "exit codes: "
    + "; ".join(
        f"{code} {text}" for code, text in sorted(_EXIT_MEANINGS.items())
    )
)


class _Parser(argparse.ArgumentParser):
    """Argparse, with one change: a usage error exits 1, not 2.

    Argparse exits 2 for a bad command line, and in this package 2 is a dry
    run. So `testinghq blast fire --typo` would report "ran, sent nothing" to
    any script checking whether the tool did something, which is exactly the
    misreading the shared exit-code convention exists to prevent. A command
    line the tool cannot parse is a refusal to do what was asked.

    Argparse itself is not modified: `--help` and `--version` still exit 0, and
    anything else in the ecosystem that uses argparse keeps its standard
    behaviour. Only this parser's usage errors change.
    """

    def error(self, message):  # pragma: no cover - exercised via parse_args
        self.print_usage(sys.stderr)
        self.exit(
            EXIT_REFUSED,
            f"{self.prog}: error: {message}\n"
            f"({_EXIT_CODE_HELP})\n",
        )


def build_parser():
    parser = _Parser(
        prog="testinghq",
        description="Self-testing tools for intake pipelines.",
        epilog=_EXIT_CODE_HELP,
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

    _add_barrage_parser(sub)
    _add_verify_parser(sub)
    _add_ledger_parser(sub)
    _add_redeliver_parser(sub)

    return parser


def _add_verify_parser(sub) -> None:
    """The `verify` subcommand, in two forms.

    `fire` sends and reads back. `check` reads back a run that already
    happened, so it has no `--send` and no dry-run mode, because it cannot
    reach the network and there is nothing to gate. The same absence `compare`
    has, for the same reason.

    `--allow-high-rate` is deliberately absent. This is a correctness tool
    sending a few hundred messages at a rate the operator chose; the thing that
    needs a hard ceiling is sustained load, and that is Barrage's job, with the
    ceiling already in place.
    """
    verify = sub.add_parser(
        "verify",
        help="check what the pipeline actually produced, not what it answered",
    )
    verify_sub = verify.add_subparsers(dest="command", required=True)

    v_fire = verify_sub.add_parser(
        "fire", help="fire the clean corpus, then read back what the system made of it"
    )
    v_fire.add_argument("--target")
    v_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED)
    v_fire.add_argument("--count", type=int, default=pipeline_verify.DEFAULT_COUNT)
    v_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    v_fire.add_argument(
        "--rate", type=float, default=pipeline_verify.DEFAULT_RATE,
        help="max requests per second",
    )
    v_fire.add_argument(
        "--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX,
        help=(
            "prefix for the per-message tag. Change it when two runs hit the "
            "same system at once, so neither reads the other's records"
        ),
    )
    v_fire.add_argument(
        "--body-exact", action="store_true",
        help=(
            "require the body to match exactly rather than contain the "
            "substantive text. Off by default because a pipeline that reads the "
            "HTML part returns an equivalent body, not an identical one"
        ),
    )
    v_fire.add_argument("--out", help="path to write the verification artifact JSON")
    v_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    _add_readback_args(v_fire)
    _add_readback_poll_args(v_fire)

    v_check = verify_sub.add_parser(
        "check",
        help="read back a run that already happened, sending nothing",
    )
    v_check.add_argument("run", help="path to a run artifact written by `verify fire`")
    v_check.add_argument(
        "--tag-prefix", default=None,
        help="override the tag prefix recorded in the artifact",
    )
    v_check.add_argument("--body-exact", action="store_true")
    v_check.add_argument("--out", help="path to write the verification artifact JSON")
    v_check.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    _add_readback_args(v_check)
    _add_readback_poll_args(v_check)

def _add_ledger_parser(sub) -> None:
    """The `ledger` subcommand.

    Deliberately without `--allow-high-rate`, for the same reason verify has
    none: this is a correctness tool sending a few hundred messages at a rate
    the operator chose. Sustained load is Barrage's job, with the ceiling
    already in place.
    """
    ledger = sub.add_parser(
        "ledger",
        help="exactly-once accounting: did we lose any messages?",
    )
    ledger_sub = ledger.add_subparsers(dest="command", required=True)

    l_fire = ledger_sub.add_parser(
        "fire", help="send N uniquely tagged messages and reconcile them"
    )
    l_fire.add_argument("--target")
    l_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED)
    l_fire.add_argument(
        "--count", type=int, default=pipeline_ledger.DEFAULT_COUNT,
        help="how many uniquely tagged messages to send",
    )
    l_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    l_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE)
    l_fire.add_argument("--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX)
    l_fire.add_argument("--out", help="path to write the ledger artifact JSON")
    l_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    _add_readback_args(l_fire)
    _add_readback_poll_args(l_fire)

def _add_redeliver_parser(sub) -> None:
    """The `redeliver` subcommand.

    Not an email sender and not a way to hammer an endpoint: it sends a couple
    of dozen payloads, paced, at infrastructure the operator owns, to find out
    what the pipeline does when a provider misbehaves.
    """
    redeliver = sub.add_parser(
        "redeliver",
        help="test delivery semantics: retries, duplicates, and out-of-order replies",
    )
    redeliver_sub = redeliver.add_subparsers(dest="command", required=True)

    r_fire = redeliver_sub.add_parser(
        "fire", help="run the redelivery scenarios and check for duplicate tickets"
    )
    r_fire.add_argument("--target")
    r_fire.add_argument("--seed", type=int, default=pipeline_verify.DEFAULT_SEED)
    r_fire.add_argument(
        "--count", type=int, default=pipeline_redeliver.DEFAULT_MESSAGES,
        help="how many messages the duplicate scenarios redeliver",
    )
    r_fire.add_argument(
        "--scenario", action="append", choices=list(pipeline_redeliver.SCENARIOS),
        help="run only this scenario; repeatable. Default is all of them",
    )
    r_fire.add_argument(
        "--retry-after", type=float, default=pipeline_redeliver.DEFAULT_RETRY_AFTER,
        help=(
            "seconds before the slow-retry redelivery. Deduplication windows "
            "vary widely, so the default is a starting point to adjust rather "
            "than a value that suits every pipeline"
        ),
    )
    r_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    r_fire.add_argument("--rate", type=float, default=pipeline_verify.DEFAULT_RATE)
    r_fire.add_argument("--tag-prefix", default=pipeline_messages.DEFAULT_TAG_PREFIX)
    r_fire.add_argument("--out", help="path to write the redelivery artifact JSON")
    r_fire.add_argument("--config", default=DEFAULT_TARGET_CONFIG)
    _add_readback_args(r_fire)
    _add_readback_poll_args(r_fire)

def _resolve_readback(args):
    """Build the ReadbackConfig for whichever pipeline command is running.

    Precedence is flag over config file. A custom adapter still gets the config
    file's `[readback]` table: the flag says HOW to read the system and the file
    says WHAT its url, field names and timeout are, and a factory handed only a
    spec has nothing to connect to.

    A missing adapter is a refusal, not a default. There is no sensible
    fallback, and silently proceeding without a readback would produce a
    verification run that verified nothing and said nothing, which is the exact
    false green this package was built to eliminate.
    """
    spec = getattr(args, "readback", None)
    override = getattr(args, "allow_public_readback", False)

    try:
        config = load_config(getattr(args, "config", DEFAULT_TARGET_CONFIG))
    except ConfigError:
        config = None
    raw = config.readback_table() if config is not None else None

    if spec:
        if isinstance(raw, dict):
            merged = dict(raw)
            merged["kind"] = spec
            merged.setdefault("allow_public_hosts", override)
            return pipeline_adapters.parse_readback_config(merged)
        return pipeline_adapters.parse_readback_config(
            {"kind": spec, "allow_public_hosts": override}
        )

    if raw is None:
        raise AdapterError(
            "no readback adapter configured. Pass --readback http, --readback "
            "mailbox, or --readback module:attribute, or declare a [readback] "
            "table in the target config."
        )
    if override and isinstance(raw, dict):
        raw = dict(raw)
        raw["allow_public_hosts"] = True
    return pipeline_adapters.parse_readback_config(raw)


def _add_readback_poll_args(parser) -> None:
    """How long to keep asking the system what it produced.

    A fixed sleep before looking once is wrong in both directions: too short and
    a queue consumer's output is reported as a loss, too long and every run pays
    for the slowest pipeline that ever was. Polling until the counts stop moving
    is the version that adapts, and the counts rather than the messages are what
    it waits on, so a duplicate that lands late is still caught.
    """
    parser.add_argument(
        "--quiet-window",
        type=float,
        default=common.DEFAULT_QUIET_WINDOW,
        help=(
            "seconds the record counts must hold steady before a read is "
            "believed. Not a timeout: a longer window costs a slower run, not a "
            "wrong one"
        ),
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=common.DEFAULT_MAX_WAIT,
        help="seconds to keep polling before reporting whatever was seen",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=common.DEFAULT_POLL_INTERVAL,
        help="seconds between lookups while waiting",
    )


def _add_readback_args(parser) -> None:
    """The arguments every pipeline tool needs to find a system to read.

    `--readback` is how the adapter is chosen, and it has three shapes because
    there are three honest options: a built-in kind, an import path for a
    custom adapter, or nothing, in which case the `[readback]` table in the
    target config is used. The precedence is flag over config, and a flag that
    names an import path is the only way to bring your own, because a Python
    object cannot be spelled on a command line.
    """
    parser.add_argument(
        "--readback",
        help=(
            "how to read the system under test: 'http', 'mailbox', or "
            "'module:attribute' for a custom adapter. Overrides the "
            "[readback] table in the target config."
        ),
    )
    parser.add_argument(
        "--allow-public-readback",
        action="store_true",
        help=(
            "allow the readback URL to be a public host. Refused by default, "
            "because on a real deployment the readback is a ticket store or a "
            "mail sink that may hold other people's data"
        ),
    )
    parser.add_argument(
        "--expect-route",
        help=(
            "the route every message should have been routed to, checked "
            "against what the system recorded. Without it, the expected route "
            "is the message's own recipient, and with no notion of routing the "
            "check reports as not-checked rather than passing"
        ),
    )


def _add_barrage_parser(sub) -> None:
    """The `barrage` subcommand: load-test a configured target.

    Barrage fires clean, provider-shaped payloads at an endpoint the
    operator controls, at a high but controlled rate, and reports how the
    pipeline held. It is NOT an email sender, NOT a flooding tool, and NOT
    for endpoints you do not own. Dry-run is the default here exactly as it
    is for blast: --send is required to put anything on the wire.
    """
    barrage = sub.add_parser(
        "barrage",
        help="load-test a configured target with clean payloads at a controlled rate",
    )
    barrage_sub = barrage.add_subparsers(dest="command", required=True)

    b_fire = barrage_sub.add_parser("fire", help="run a load test against a configured target")
    b_fire.add_argument("--target")
    b_fire.add_argument("--seed", type=int, default=barrage_fire.DEFAULT_SEED)
    b_fire.add_argument(
        "--rate", type=float, default=barrage_fire.DEFAULT_RATE,
        help="target requests per second",
    )
    b_fire.add_argument(
        "--duration", type=float, default=barrage_fire.DEFAULT_DURATION,
        help="total run duration in seconds, including the warmup ramp",
    )
    b_fire.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "closed-loop worker count. Refused with --mode open, which has no "
            "executor yet and dispatches one request at a time; see issue #38"
        ),
    )
    b_fire.add_argument(
        "--mode", choices=["open", "closed"], default=barrage_fire.DEFAULT_MODE,
        help="open-loop (fixed arrival rate) or closed-loop (fixed concurrency)",
    )
    b_fire.add_argument(
        "--warmup", type=float, default=barrage_fire.DEFAULT_WARMUP,
        help="seconds of ramp before steady state, taken out of --duration",
    )
    b_fire.add_argument(
        "--pool-size", type=int, default=barrage_fire.DEFAULT_POOL_SIZE,
        help="how many distinct seeded payloads to cycle through",
    )
    b_fire.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    b_fire.add_argument(
        "--allow-high-rate", action="store_true",
        help=(
            "raise the hard safety ceiling on rate and duration. This exists "
            "so a mistake cannot become a self-inflicted denial of service; "
            "pass it only deliberately, for a target you own"
        ),
    )
    b_fire.add_argument("--out", help="path to write the run artifact JSON")
    b_fire.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )

    b_replay = barrage_sub.add_parser("replay", help="re-run a saved barrage run")
    b_replay.add_argument("run", help="path to a previously written barrage artifact JSON")
    b_replay.add_argument(
        "--send", action="store_true", help="actually send (default is dry-run)"
    )
    b_replay.add_argument(
        "--allow-high-rate", action="store_true",
        help="raise the hard safety ceiling on rate and duration",
    )
    b_replay.add_argument("--out", help="path to write the run artifact JSON")
    b_replay.add_argument(
        "--config", default=DEFAULT_TARGET_CONFIG, help="path to target config TOML"
    )

    # compare. Note the absence of --target, --send and --config, which is the
    # point: it reads two files and compares them. There is no network path to
    # gate, so there is no gate. Unlike blast and barrage there is no
    # subcommand, because there is exactly one thing it does.
    compare = sub.add_parser(
        "compare",
        help="compare a baseline and a candidate run artifact and report what changed",
    )
    compare.add_argument(
        "--baseline", required=True, help="path to the baseline run artifact JSON"
    )
    compare.add_argument(
        "--candidate", required=True, help="path to the candidate run artifact JSON"
    )
    compare.add_argument("--out", help="path to write the difference report as JSON")
    compare.add_argument(
        "--fail-on-regression",
        action="store_true",
        help=(
            "exit non-zero when the candidate introduced a regression. Off by "
            "default so a plain comparison is just a comparison; turn it on in "
            "CI, where a non-zero exit should stop something"
        ),
    )


def _not_yet(command):
    """Fallback for a subcommand argparse accepted but main() has no handler
    for. Every current subcommand is implemented, so reaching this means the
    parser and the dispatcher disagree, which is a bug in this file rather
    than a missing feature. The message says so instead of describing a
    milestone that finished long ago."""
    print(
        f"testinghq blast {command}: no handler is wired up for this "
        f"subcommand, which is a bug (argparse offered it anyway)",
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


# ---------------------------------------------------------------------------
# blast replay
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# barrage fire / barrage replay
#
# These stay thin on purpose: the orchestration, the guardrail wiring, and
# the ceiling all live in testinghq/barrage/fire.py, so the CLI is only an
# argparse surface over it. Nothing safety-relevant is decided here.
# ---------------------------------------------------------------------------


def _barrage_execute(args, plan, seed: int, pool_size: int, target_name: Optional[str]) -> int:
    """Run the barrage send path, translating every refusal into an exit
    code rather than a traceback."""
    try:
        return barrage_fire.execute(
            plan,
            seed,
            pool_size,
            target_name,
            args.config,
            args.out,
            allow_high_rate=args.allow_high_rate,
        )
    except (
        guardrails.GuardrailError,
        ConfigError,
        RateCeilingError,
        barrage_fire.BarrageError,
    ) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED


def _cmd_barrage_fire(args) -> int:
    # An explicitly-passed --concurrency is refused in BOTH modes, here rather
    # than inside build_plan, because this is the layer where an operator is
    # asking for something that will have no effect. build_plan coerces it
    # instead, since the caller there may be `barrage replay` reading an
    # artifact whose stored number predates this being true.
    #
    # Arguments are validated before anything is announced, so a refusal does
    # not follow a line describing what the command was about to do.
    if args.concurrency is not None and args.concurrency != 1:
        print(
            f"refused: --concurrency {args.concurrency} has no effect in "
            f"--mode {args.mode}. Barrage has no executor yet, so requests are "
            f"dispatched one at a time in both modes and the real concurrency "
            f"is 1. Drop the flag. Tracked in issue #38.",
            file=sys.stderr,
        )
        return barrage_fire.EXIT_REFUSED

    try:
        plan = barrage_fire.build_plan(
            args.mode, args.rate, args.duration, args.concurrency, args.warmup
        )
        # The ceiling is checked here too, not only inside run(), so a
        # dry run reports an over-limit plan as refused instead of
        # cheerfully previewing a run that would never be allowed.
        barrage_fire.check_rate_ceiling(
            args.rate, args.duration, allow_high_rate=args.allow_high_rate
        )
    except (RateCeilingError, barrage_fire.BarrageError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED

    # Announced only now that the plan is known to be legal. See the note at
    # the top of this function.
    decision = guardrails.evaluate_send(args.send)
    print(f"barrage fire: {decision.reason}")

    if not decision.will_send:
        print(barrage_fire.format_dry_run_preview(plan, args.seed, args.pool_size))
        return barrage_fire.EXIT_DRY_RUN

    return _barrage_execute(args, plan, args.seed, args.pool_size, args.target)


def _cmd_barrage_replay(args) -> int:
    try:
        data = json.loads(Path(args.run).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"barrage replay: could not read {args.run!r}: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED
    except json.JSONDecodeError as exc:
        print(f"barrage replay: {args.run!r} is not valid JSON: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED

    seed = data.get("seed")
    config = data.get("config") or {}
    required = ("mode", "rate", "duration", "warmup", "concurrency", "pool_size")
    if seed is None or any(config.get(key) is None for key in required):
        print(
            f"barrage replay: {args.run!r} is missing seed or config "
            f"{required}, cannot reproduce the run",
            file=sys.stderr,
        )
        return barrage_fire.EXIT_REFUSED

    try:
        plan = barrage_fire.build_plan(
            config["mode"],
            config["rate"],
            config["duration"],
            config["concurrency"],
            config["warmup"],
        )
    except (barrage_fire.BarrageError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return barrage_fire.EXIT_REFUSED

    decision = guardrails.evaluate_send(args.send)
    print(f"barrage replay: {decision.reason}")

    if not decision.will_send:
        print(barrage_fire.format_dry_run_preview(plan, seed, config["pool_size"]))
        return barrage_fire.EXIT_DRY_RUN

    return _barrage_execute(args, plan, seed, config["pool_size"], config.get("target"))


def _cmd_compare(args) -> int:
    """Compare two saved run artifacts and report what changed.

    Kept thin on purpose, like the barrage handlers: all of the comparison
    logic lives in `testinghq/compare/runs.py`, so this is only argument
    plumbing, file reading, output, and the exit code.

    Note what is absent. There is no `evaluate_send` call, no target
    resolution, no config load and no transport. This function cannot put bytes
    on a wire, which is why it has no dry-run mode and no guardrail gate: the
    gate would have nothing to gate. Every refusal here is about a malformed
    input file, not about a request.
    """
    try:
        baseline = compare_runs.load_artifact(args.baseline)
        candidate = compare_runs.load_artifact(args.candidate)
    except compare_runs.CompareError as exc:
        print(f"compare: {exc}", file=sys.stderr)
        return compare_runs.EXIT_USAGE

    diff = compare_runs.compare(baseline, candidate)
    print(compare_runs.format_diff(diff))

    if args.out:
        Path(args.out).write_text(
            json.dumps(diff, indent=2, sort_keys=False), encoding="utf-8"
        )
        print(f"\nwrote {args.out}")

    if args.fail_on_regression and diff["regressed"]:
        return compare_runs.EXIT_REGRESSION
    return compare_runs.EXIT_NO_REGRESSION



def _cmd_verify_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    corpus = pipeline_verify.build_tagged_corpus(
        args.seed, args.count, args.tag_prefix
    )

    print(f"verify fire: {decision.reason}")
    if not decision.will_send:
        print(pipeline_verify.format_dry_run(corpus, readback, args.tag_prefix))
        return EXIT_DRY_RUN

    return pipeline_verify.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        tag_prefix=args.tag_prefix,
        rate=args.rate,
        route=args.expect_route,
        body_exact=args.body_exact,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
    )


def _cmd_verify_check(args) -> int:
    """Read back a finished run. No `--send`, because this path cannot put
    anything on a wire."""
    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    return pipeline_verify.check_saved_run(
        args.run,
        args.config,
        args.out,
        readback=readback,
        route=args.expect_route,
        body_exact=args.body_exact,
        tag_prefix=args.tag_prefix,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
    )



def _cmd_ledger_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"ledger fire: {decision.reason}")
    if not decision.will_send:
        print(
            pipeline_ledger.format_dry_run(
                args.count, args.seed, args.tag_prefix, readback
            )
        )
        return EXIT_DRY_RUN

    return pipeline_ledger.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        tag_prefix=args.tag_prefix,
        rate=args.rate,
        route=args.expect_route,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
    )



def _cmd_redeliver_fire(args) -> int:
    decision = guardrails.evaluate_send(args.send)

    try:
        readback = _resolve_readback(args)
    except (AdapterError, ConfigError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    try:
        scenarios = pipeline_redeliver.build_scenarios(
            args.seed, args.count, args.tag_prefix, args.retry_after, args.scenario
        )
    except pipeline_redeliver.ScenarioError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    print(f"redeliver fire: {decision.reason}")
    if not decision.will_send:
        print(pipeline_redeliver.format_dry_run(scenarios, readback, args.tag_prefix))
        return EXIT_DRY_RUN

    return pipeline_redeliver.execute(
        args.seed,
        args.count,
        args.target,
        args.config,
        args.out,
        readback=readback,
        scenarios=args.scenario,
        tag_prefix=args.tag_prefix,
        rate=args.rate,
        retry_after=args.retry_after,
        quiet_window=args.quiet_window,
        max_wait=args.max_wait,
        poll_interval=args.poll_interval,
    )


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
    if args.tool == "barrage":
        if args.command == "fire":
            return _cmd_barrage_fire(args)
        if args.command == "replay":
            return _cmd_barrage_replay(args)
        return _not_yet(args.command)
    if args.tool == "compare":
        return _cmd_compare(args)
    if args.tool == "redeliver":
        if args.command == "fire":
            return _cmd_redeliver_fire(args)
        return _not_yet(args.command)
    if args.tool == "ledger":
        if args.command == "fire":
            return _cmd_ledger_fire(args)
        return _not_yet(args.command)
    if args.tool == "verify":
        if args.command == "fire":
            return _cmd_verify_fire(args)
        if args.command == "check":
            return _cmd_verify_check(args)
        return _not_yet(args.command)
    return _not_yet(args.tool)


if __name__ == "__main__":
    raise SystemExit(main())
