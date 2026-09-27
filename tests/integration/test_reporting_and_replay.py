"""Integration: reporting and replay, end to end through the real CLI paths.

The backlog's reporting and replay integration item. `core/report.py` and
`testinghq/cli.py`'s replay path are both unit tested in isolation, and
`tests/unit/test_report.py` checks the report module against the shipped
fixtures. Nothing checked the two together, which is where the claims actually
live:

- A fire run's printed summary and its written artifact are the same numbers.
- `blast replay` re-fires a saved run *byte-identically*, which is the
  product's reproducibility claim. The CLI checks payload hashes before it
  fires, but nothing checked that the bytes it would put on the wire the second
  time really are the bytes it put the first time.
- A dry-run replay makes zero network calls, on the replay path as well as the
  fire path. Dry-run-by-default is the guardrail the whole product rests on, and
  replay is a second door into the same machinery.

Hermetic: every test injects a recording HTTP client, so nothing opens a
socket. The rate is high and the bucket is satisfied by its initial capacity, so
the pacing never introduces a real sleep.
"""
import json

import pytest

from testinghq import cli
from testinghq.core import guardrails, report
from testinghq.core.transport import ClientResponse

SEED = 4242
COUNT = 40
# Above the 50 req/s ceiling? No. At or under it, so a plan is never refused
# for exceeding the hard rate limit and the test measures replay rather than the
# ceiling.
RATE = 50

TARGET_NAME = "local"
TARGET_URL = "http://127.0.0.1:9/intake"  # nothing listens; nothing is sent


class RecordingClient:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self.body = body
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ClientResponse(status=self.status, body=self.body)


class ExplodingClient:
    """A transport that always fails, which is how a timeout is modelled."""

    def send(self, request):
        raise OSError("connection refused")


@pytest.fixture()
def config_file(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET_NAME}]\nname = "{TARGET_NAME}"\nurl = "{TARGET_URL}"\n',
        encoding="utf-8",
    )
    return str(path)


def _fire(config_file, client, out=None, pairs=None, seed=SEED, count=COUNT):
    """Drive the CLI's real send path with an injected client.

    `pairs` defaults to the default-mix corpus the CLI itself builds. Tests
    that care about one category pass a single-category corpus instead, so
    they never depend on a random draw happening to produce the category they
    are about. `degenerate` carries a 5% default weight, so "did any degenerate
    payload appear" is a coin flip at small corpus sizes and a flake at large
    ones. Zero tolerance for that here.
    """
    if pairs is None:
        pairs = cli._build_corpus(seed, count)
    code = cli._run_fire(
        pairs, seed, count, TARGET_NAME, RATE, out, config_file, client=client
    )
    assert code == 0, "the send path refused or failed unexpectedly"
    return pairs


def _single_category_pairs(category, count=12, seed=SEED):
    """A corpus where every payload is `category`, with no random draw
    involved."""
    from testinghq.blast.corrupt import corrupt_corpus
    from testinghq.blast.generate import generate_corpus

    corrupted = corrupt_corpus(generate_corpus(seed, count), seed, {category: 1.0})
    return [(email, report.category_label(recipe)) for email, recipe in corrupted]


def _replay_args(run_path, config_file, send=False, rate=RATE):
    """Build replay's args the way the real CLI would.

    Two things worth knowing, both found by getting them wrong first. There is
    no `--dry-run` flag: dry-run is the *absence* of `--send`, and passing it is
    an argparse error. And `--rate` defaults to 5 req/s, which the CLI then
    enforces with a real `time.sleep` through the rate bucket, so a 40-payload
    replay at the default rate spends seven seconds being paced. The test
    passes RATE explicitly so the suite measures replay rather than the
    pacer. The pacing itself is covered in tests/unit/barrage and
    tests/unit/test_ratelimit.py, where the clock is injected.
    """
    argv = [
        "blast", "replay", str(run_path),
        "--config", config_file,
        "--rate", str(rate),
    ]
    if send:
        argv.append("--send")
    return cli.build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# A fire run produces an artifact whose numbers are the engine's
# ---------------------------------------------------------------------------


def test_a_fire_run_writes_an_artifact_the_engine_agrees_with(config_file, tmp_path):
    """The artifact is not a UI summary or a status dump. It must be exactly
    what `core.report` derives from the same records, or `blast replay` and
    the web UI are reading a different run than the one that was fired."""
    out = tmp_path / "run.json"
    _fire(config_file, RecordingClient(), out=str(out))

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["seed"] == SEED
    assert artifact["config"]["dry_run"] is False
    assert artifact["config"]["target"] == TARGET_NAME
    assert len(artifact["records"]) == COUNT

    assert artifact["summary"] == report.compute_summary(
        artifact["records"], artifact["seed"], artifact["config"]
    )
    # Every record is accounted for in exactly one bucket.
    assert sum(artifact["summary"]["by_category"].values()) == COUNT
    assert sum(artifact["summary"]["by_status_class"].values()) == COUNT


def test_the_artifact_records_the_statuses_the_endpoint_actually_returned(
    config_file, tmp_path
):
    """A 500 from the endpoint has to reach the artifact as a 500, and a clean
    payload that got one has to be flagged. This is the expectation-based
    reading the product is named for, checked through a real send path rather
    than by constructing records by hand.

    Driven from a clean-only corpus so the assertion cannot pass or fail on
    whether a random draw happened to produce a clean payload."""
    pairs = _single_category_pairs("clean")
    out = tmp_path / "run.json"
    _fire(config_file, RecordingClient(status=500, body=b"boom"), out=str(out), pairs=pairs)

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["by_status_class"]["5xx"] == len(pairs)
    assert artifact["summary"]["by_category"]["clean"] == len(pairs)

    for record in artifact["records"]:
        assert record["response"]["status"] == 500
        assert record["assertion"]["passed"] is False
        assert record["assertion"]["mismatches"]

    assert len(artifact["summary"]["flags"]) == len(pairs), (
        "every clean payload that got a 500 should be named in the flags list"
    )


def test_a_transport_failure_is_recorded_as_a_timeout_not_a_status(
    config_file, tmp_path
):
    """A client that raises is not a 5xx. Recording it as one would make an
    unreachable endpoint look like a reachable one that is failing, which is
    exactly backwards."""
    pairs = _single_category_pairs("degenerate")
    out = tmp_path / "run.json"
    _fire(config_file, ExplodingClient(), out=str(out), pairs=pairs)

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert artifact["summary"]["by_status_class"]["timeout"] == len(pairs)
    for record in artifact["records"]:
        assert record["response"]["status"] is None
    # Degenerate payloads that timed out are failures, by the product's rules,
    # while a clean payload that timed out is also a failure. The interesting
    # half is that a *messy-but-valid* payload timing out is not flagged as
    # garbage, it just has no verdict to give.
    for record in artifact["records"]:
        assert record["assertion"]["passed"] is False
    assert len(artifact["summary"]["flags"]) == len(pairs)


# ---------------------------------------------------------------------------
# Replay is byte-identical
# ---------------------------------------------------------------------------


def test_replay_refires_the_exact_same_bytes(config_file, tmp_path):
    """The reproducibility claim, checked the only way that means anything:
    compare the request bodies of the original fire against the replay's.

    The CLI does verify payload hashes before firing, so a hash mismatch is
    already caught. What was not covered was the weaker claim, that the bytes
    which go over the wire are identical, which is a property of
    serialization and not of the hash.
    """
    original_client = RecordingClient()
    out = tmp_path / "run.json"
    _fire(config_file, original_client, out=str(out))

    replay_client = RecordingClient()
    args = _replay_args(out, config_file, send=True)
    assert cli._cmd_replay(args, client=replay_client) == 0

    assert len(replay_client.requests) == len(original_client.requests) == COUNT
    for first, second in zip(original_client.requests, replay_client.requests):
        assert first.body == second.body
        assert first.url == second.url
        assert first.headers == second.headers


def test_replay_of_an_untouched_run_is_accepted(config_file, tmp_path):
    out = tmp_path / "run.json"
    _fire(config_file, RecordingClient(), out=str(out))
    assert cli._cmd_replay(_replay_args(out, config_file)) == 2, (
        "a dry-run replay should decline to send, which is exit code 2"
    )


def test_replay_refuses_when_a_payload_hash_was_tampered_with(config_file, tmp_path):
    """The determinism guard, and the reason it exists. A drifted generator
    means seed+config no longer reproduces the run, and quietly firing a
    different corpus would be worse than refusing."""
    out = tmp_path / "run.json"
    _fire(config_file, RecordingClient(), out=str(out))

    artifact = json.loads(out.read_text(encoding="utf-8"))
    artifact["records"][7]["payload_sha256"] = "0" * 64
    out.write_text(json.dumps(artifact), encoding="utf-8")

    replay_client = RecordingClient()
    assert cli._cmd_replay(_replay_args(out, config_file, send=True), client=replay_client) == 1
    assert replay_client.requests == [], (
        "replay refused on a hash mismatch, so it must not have fired anything"
    )


def test_replay_refuses_a_run_that_is_not_a_whole_artifact(tmp_path, config_file):
    truncated = tmp_path / "partial.json"
    truncated.write_text(json.dumps({"seed": 1}), encoding="utf-8")
    assert cli._cmd_replay(_replay_args(truncated, config_file, send=True)) == 1


# ---------------------------------------------------------------------------
# Dry-run holds on the replay path too
# ---------------------------------------------------------------------------


def test_a_dry_run_replay_makes_no_network_call(config_file, tmp_path):
    out = tmp_path / "run.json"
    _fire(config_file, RecordingClient(), out=str(out))

    replay_client = RecordingClient()
    assert cli._cmd_replay(_replay_args(out, config_file), client=replay_client) == 2
    assert replay_client.requests == [], (
        "a replay without --send must not touch the transport at all"
    )


def test_dry_run_replay_does_not_need_a_working_target(config_file, tmp_path):
    """A dry run should not resolve or dial the target. Pointed at a config
    whose URL cannot resolve, it must still succeed."""
    out = tmp_path / "run.json"
    _fire(config_file, RecordingClient(), out=str(out))

    args = _replay_args(out, config_file)
    args.target = "nonexistent-unconfigured"
    assert cli._cmd_replay(args) == 2, (
        "dry-run replay must not care that the saved run names a target that "
        "is not configured here; it is not going to send"
    )


# ---------------------------------------------------------------------------
# The artifact round-trips through JSON
# ---------------------------------------------------------------------------


def test_the_artifact_is_json_serialisable_and_reloads_identically(
    config_file, tmp_path
):
    """Trivial on its face, and the reason it is here: the artifact is what
    travels between the CLI, the web UI, and a later replay, so it has to
    survive being written and read with nothing lost."""
    out = tmp_path / "run.json"
    _fire(config_file, RecordingClient(status=202, body=b"accepted"), out=str(out))

    first = json.loads(out.read_text(encoding="utf-8"))
    reserialised = tmp_path / "again.json"
    reserialised.write_text(
        json.dumps(first, indent=2, sort_keys=False), encoding="utf-8"
    )
    assert json.loads(reserialised.read_text(encoding="utf-8")) == first


def test_a_fire_run_at_an_unconfigured_target_is_refused(tmp_path, capsys):
    """The guardrail, reached through the same path everything else here uses.
    The other fire tests all configure their target, so without this one a
    regression that dropped the allow-list check would not turn anything red
    here."""
    pairs = cli._build_corpus(SEED, 5)
    client = RecordingClient()
    code = cli._run_fire(
        pairs, SEED, 5, "not-configured", RATE, None, str(tmp_path / "none.toml"),
        client=client,
    )
    assert code == 1
    assert "refused" in capsys.readouterr().err
    assert client.requests == []
    assert guardrails.GuardrailError is not None
