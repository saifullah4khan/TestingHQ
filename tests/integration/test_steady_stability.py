"""steady against a real intake pipeline, through the real transport.

A classifier that is stable is the baseline, and a classifier that loses track of
a message when a signature is added is the finding. Both are produced by the same
pipeline with one switch changed, so the difference is attributable.

The three properties tested hardest are the ways this tool would produce a
confident wrong number: a run that could not see the label, a run where only
some families were measurable, and a run whose flip rate is under the gate while
the report says the wrong thing about stability.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from pipeline_under_test import PipelineUnderTest

from testinghq.pipeline import steady as tool
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK, EXIT_REFUSED

SEED = 5
COUNT = 3
TARGET = "local"
TARGET_URL = "http://localhost:9/intake"
SPEC_MODULE = "steady_integration_spec"

GATE = 0.10


class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def target_config(tmp_path) -> str:
    path = tmp_path / "target.toml"
    path.write_text(
        f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n\n'
        '[readback]\nkind = "http"\nurl = "http://localhost:8000/tickets"\n',
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def spec_probe():
    module = types.ModuleType(SPEC_MODULE)
    module.build = lambda config: None
    sys.modules[SPEC_MODULE] = module
    yield module
    del sys.modules[SPEC_MODULE]


def _publish(spec_probe, pipeline):
    spec_probe.build = lambda config: pipeline


def _tagged(items, families):
    """(tag, variant) in the order `build_items` walked the families.

    The test has to know which tag carries which transform, because the pipeline
    can only be told which payloads to relabel and the readback is keyed by tag.
    Going through the tool's own builders is the only way the test and the run
    agree, and a test that guessed the tag numbering would go stale silently.
    """
    pairs = []
    index = 0
    for family in families:
        for variant in family.variants:
            pairs.append((items[index][1], variant))
            index += 1
    return pairs


def _tags_for(transform, count=COUNT):
    families = tool.build_families(SEED, intents=tool.load_intents()[:count])
    items = tool.build_items(families, seed=SEED)
    return [tag for tag, variant in _tagged(items, families)
            if variant.transform == transform]


def _run(pipeline, target_config, tmp_path, spec_probe, name="steady.json", **kwargs):
    _publish(spec_probe, pipeline)
    clock = _VirtualClock()
    lines: list = []
    settings = {
        "quiet_window": 0.0, "poll_interval": 0.05, "max_flip_rate": GATE,
    }
    settings.update(kwargs)
    code = tool.execute(
        SEED,
        COUNT,
        TARGET,
        target_config,
        str(tmp_path / name),
        readback=ReadbackConfig(kind="python", spec=f"{SPEC_MODULE}:build"),
        # The pipeline is BOTH the sending client and the readback source. The
        # suite-wide network block caught its absence the first time this ran,
        # which is that guard doing its job.
        client=pipeline,
        readback_client=pipeline,
        sleep=clock.sleep,
        clock=clock,
        printer=lines.append,
        **settings,
    )
    text = "\n".join(lines)
    path = tmp_path / name
    if not path.is_file():
        raise AssertionError(
            f"no artifact at {path}. A refusal writes nothing, so the run's "
            f"output is the diagnosis.\n--- output ---\n{text}"
        )
    return code, json.loads(path.read_text(encoding="utf-8")), text


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_a_stable_classifier_passes(target_config, tmp_path, spec_probe):
    pipeline = PipelineUnderTest()
    code, artifact, text = _run(pipeline, target_config, tmp_path, spec_probe)

    assert code == EXIT_OK, text
    assert artifact["stability"]["flip_rate"] == 0.0
    assert artifact["stability"]["inconsistent_families"] == []
    assert "STABLE" in text
    assert text.splitlines()[0].startswith("steady:")


def test_the_pipeline_really_saw_every_payload(target_config, tmp_path, spec_probe):
    """Without this, a clean run could mean nothing was ever sent."""
    pipeline = PipelineUnderTest()
    _code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)
    sent = artifact["stability"]["sent"]
    expected = sum(len(f["variants"]) for f in artifact["families"])
    assert sent == expected
    assert len(pipeline.received) == expected


def test_the_report_names_the_label_field_it_measured(
    target_config, tmp_path, spec_probe
):
    """A flip rate with no field named is a number nobody can act on."""
    pipeline = PipelineUnderTest()
    _code, artifact, text = _run(pipeline, target_config, tmp_path, spec_probe)
    assert artifact["stability"]["label_field"] == "route"
    assert "'route'" in text.splitlines()[0]


# ---------------------------------------------------------------------------
# An unstable classifier
# ---------------------------------------------------------------------------


def test_a_classifier_that_loses_track_of_a_signature_is_caught(
    target_config, tmp_path, spec_probe
):
    """The bug the tool exists for. The route is computed perfectly from the
    message; the classifier simply files a signed message differently, and only a
    metamorphic comparison can see it."""
    signed_tags = _tags_for("add-signature")
    assert signed_tags, "the fixture produced no signed variants, so nothing to test"

    pipeline = PipelineUnderTest(unstable_tags=signed_tags)
    code, artifact, text = _run(pipeline, target_config, tmp_path, spec_probe)

    assert code == EXIT_MISMATCH
    assert artifact["stability"]["flip_rate"] > 0
    assert artifact["stability"]["inconsistent_families"]
    assert "add-signature" in text
    assert "worst transforms" in text


def test_the_per_transform_breakdown_blames_the_right_transform(
    target_config, tmp_path, spec_probe
):
    signed_tags = _tags_for("add-signature")
    pipeline = PipelineUnderTest(unstable_tags=signed_tags)

    _code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)
    by_transform = artifact["stability"]["by_transform"]
    assert by_transform["add-signature"]["dissenters"] > 0
    assert by_transform["add-signature"]["dissent_rate"] > 0
    for name, bucket in by_transform.items():
        if name != "add-signature":
            assert bucket["dissenters"] == 0, (
                f"{name} was blamed for a flip it did not cause"
            )


def test_a_flip_below_the_gate_still_passes(target_config, tmp_path, spec_probe):
    """The gate is a threshold, not a switch: a classifier that is a few percent
    unstable is worth a note in the artifact, not a red build."""
    pipeline = PipelineUnderTest(unstable_tags=_tags_for("add-signature")[:1])
    code, artifact, text = _run(
        pipeline, target_config, tmp_path, spec_probe, max_flip_rate=0.5
    )
    assert code == EXIT_OK, text
    assert artifact["stability"]["flip_rate"] > 0, "it did flip, and the gate let it"
    assert artifact["stability"]["gate"]["failed"] is False
    assert "gate (50.0%): passed" in text


def test_an_unknown_label_field_is_refused_before_anything_is_sent(tmp_path):
    """Directly, rather than through the CLI: the CLI constrains the field with
    `choices`, so this is the guard behind that, and a guard behind a guard is
    what stops a future caller skipping the choices."""
    pipeline = PipelineUnderTest()
    lines: list = []
    clock = _VirtualClock()
    code = tool.execute(
        SEED, COUNT, TARGET, "unused.toml", None,
        readback=ReadbackConfig(kind="mailbox", path="unused.jsonl"),
        client=pipeline, readback_client=pipeline,
        sleep=clock.sleep, clock=clock, printer=lines.append,
        label_field="mood", quiet_window=0.0, poll_interval=0.05,
    )
    text = "\n".join(lines)
    assert code == EXIT_REFUSED
    assert "label-field must be one of" in text
    assert pipeline.received == [], "it sent something before refusing"


def test_a_negative_gate_is_refused_before_anything_is_sent(tmp_path):
    pipeline = PipelineUnderTest()
    lines: list = []
    clock = _VirtualClock()
    code = tool.execute(
        SEED, COUNT, TARGET, "unused.toml", None,
        readback=ReadbackConfig(kind="mailbox", path="unused.jsonl"),
        client=pipeline, readback_client=pipeline,
        sleep=clock.sleep, clock=clock, printer=lines.append,
        max_flip_rate=-0.1, quiet_window=0.0, poll_interval=0.05,
    )
    assert code == EXIT_REFUSED
    assert "--max-flip-rate" in "\n".join(lines)
    assert pipeline.received == []


def test_zero_repeats_is_refused(tmp_path):
    pipeline = PipelineUnderTest()
    lines: list = []
    clock = _VirtualClock()
    code = tool.execute(
        SEED, COUNT, TARGET, "unused.toml", None,
        readback=ReadbackConfig(kind="mailbox", path="unused.jsonl"),
        client=pipeline, readback_client=pipeline,
        sleep=clock.sleep, clock=clock, printer=lines.append,
        repeats=0, quiet_window=0.0, poll_interval=0.05,
    )
    assert code == EXIT_REFUSED
    assert "--repeats" in "\n".join(lines)


def test_an_unknown_label_field_cannot_even_be_parsed():
    from testinghq import cli

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["steady", "fire", "--label-field", "mood"])


# ---------------------------------------------------------------------------
# When the label could not be seen
# ---------------------------------------------------------------------------


def test_a_run_that_cannot_see_the_label_exits_3_not_0(
    target_config, tmp_path, spec_probe
):
    """The most important exit code in the tool. Nothing was measured, and 0
    would be the most confident wrong answer it could give."""
    families = tool.build_families(SEED, intents=tool.load_intents()[:COUNT])
    items = tool.build_items(families)
    pipeline = PipelineUnderTest()
    _publish(spec_probe, pipeline)

    class _Blind:
        def fetch(self, probe):
            return [
                _no_route(probe.tag)
            ]

        def close(self):
            return None

    from testinghq.pipeline.readback import Readback

    def _no_route(tag):
        return Readback(
            exists=True, ticket_id=f"T-{tag}", subject="s",
            fields=("ticket_id", "subject"),
        )

    spec_probe.build = lambda config: _Blind()
    clock = _VirtualClock()
    lines: list = []
    code = tool.execute(
        SEED, COUNT, TARGET, target_config, str(tmp_path / "blind.json"),
        readback=ReadbackConfig(kind="python", spec=f"{SPEC_MODULE}:build"),
        client=pipeline, readback_client=pipeline,
        sleep=clock.sleep, clock=clock, printer=lines.append,
        quiet_window=0.0, poll_interval=0.05, max_flip_rate=GATE,
    )
    text = "\n".join(lines)
    artifact = json.loads((tmp_path / "blind.json").read_text(encoding="utf-8"))

    assert code == EXIT_MISMATCH
    assert artifact["stability"]["flip_rate"] is None
    assert "NOTHING MEASURED" in text
    assert "NOT MEASURED" in text
    assert "could not see" in text
    del families, items


# ---------------------------------------------------------------------------
# Repeat instability, kept separate from the flip rate
# ---------------------------------------------------------------------------


def test_repeats_are_sent_and_measured_separately(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest()
    code, artifact, text = _run(
        pipeline, target_config, tmp_path, spec_probe, repeats=4
    )

    assert code == EXIT_OK, text
    stability = artifact["stability"]
    assert stability["repeats"] == 4
    assert stability["repeat_instability"] == 0.0
    assert "repeat instability" in text
    assert stability["flip_rate"] == 0.0


def test_a_reply_is_measured_from_the_artifact(target_config, tmp_path, spec_probe):
    """The gate's own sentence, so a reader knows why a run failed."""
    signed_tags = _tags_for("wrap-forward")
    pipeline = PipelineUnderTest(unstable_tags=signed_tags)

    _code, artifact, _text = _run(
        pipeline, target_config, tmp_path, spec_probe, max_flip_rate=0.0
    )
    gate = artifact["stability"]["gate"]
    assert gate["failed"] is True
    assert "over the" in gate["reason"]
    assert "gate" in gate["reason"]


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def test_the_artifact_records_every_variant_with_its_lineage(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest()
    _code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)

    for family in artifact["families"]:
        assert "intent_id" in family and "variants" in family
        for variant in family["variants"]:
            for key in (
                "intent_id", "language", "transform", "label",
                "subject", "payload_sha256",
            ):
                assert key in variant, key
    transforms = {
        v["transform"] for f in artifact["families"] for v in f["variants"]
    }
    assert "baseline" in transforms and len(transforms) > 1


def test_the_artifact_survives_a_round_trip_through_json(
    target_config, tmp_path, spec_probe
):
    pipeline = PipelineUnderTest()
    _code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)
    assert json.loads(json.dumps(artifact)) == artifact


def test_the_artifact_config_reproduces_the_run(target_config, tmp_path, spec_probe):
    pipeline = PipelineUnderTest()
    _code, artifact, _text = _run(
        pipeline, target_config, tmp_path, spec_probe, repeats=3
    )
    config = artifact["config"]
    assert config["tool"] == "steady"
    assert config["seed"] == SEED and config["count"] == COUNT
    assert config["label_field"] == "route"
    assert config["repeats"] == 3
    assert config["max_flip_rate"] == GATE
    assert "latency" not in config and "timestamp" not in config
