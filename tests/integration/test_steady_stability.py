"""steady against a pipeline double, through the real transport.

The double files every payload as the shared pipeline does and adds a
`category` the way a classifier would, from a label table the test sets per
tag. A stable classifier gives every variant of a family its family's label;
an unstable one moves some variants, and the run must say which transform and
which language moved them.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import types

import pytest
from pipeline_under_test import PipelineUnderTest

from testinghq import reporting
from testinghq.pipeline import steady as tool
from testinghq.pipeline.adapters import ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK, EXIT_REFUSED

SEED = 5
COUNT = 3
TARGET = "local"
TARGET_URL = "http://localhost:9/intake"
SPEC_MODULE = "steady_integration_spec"
PREFIX = "hq"


class ClassifyingPipeline(PipelineUnderTest):
    """The shared double plus a `category` read from `labels` (tag to label).
    A tag with no entry gets no record field at all when `blind` is set, and a
    null category otherwise."""

    def __init__(self, labels, *, blind=False, **kwargs):
        super().__init__(**kwargs)
        self.labels = dict(labels)
        self.blind = blind

    def fetch(self, probe):
        records = super().fetch(probe)
        if self.blind:
            return records
        return [
            dataclasses.replace(r, category=self.labels.get(probe.tag),
                                fields=(*r.fields, "category"))
            for r in records
        ]


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


def _variants(count=COUNT, repeats=1):
    """(tag, family, variant) in send order, built with the tool's own
    builders so the test and the run agree on which tag is which."""
    families = tool.load_families(SEED, count)
    items = tool.build_items(families, tag_prefix=PREFIX, seed=SEED)
    out = []
    index = 0
    for family in families:
        for variant in family.variants:
            out.append((items[index][1], family, variant))
            index += 1
    repeat = tool.repeat_items(families, repeats, PREFIX, SEED) if repeats > 1 else []
    return out, families, repeat


def stable_labels(repeats=1, move=lambda family, variant: False):
    """Every variant labelled with its family's category hint, except where
    `move` says otherwise."""
    variants, families, repeat = _variants(repeats=repeats)
    labels = {
        tag: ("moved" if move(family, variant) else family.category_hint)
        for tag, family, variant in variants
    }
    for index, family in enumerate(families):
        for _e, tag, _r, _p in repeat[index * repeats:(index + 1) * repeats]:
            labels[tag] = family.category_hint
    return labels


def _run(pipeline, target_config, tmp_path, spec_probe, name="steady.json",
         label_field="category", **kwargs):
    spec_probe.build = lambda config: pipeline
    clock = _VirtualClock()
    lines: list = []
    settings = {"quiet_window": 0.0, "poll_interval": 0.05}
    settings.update(kwargs)
    code = tool.execute(
        SEED, COUNT, TARGET, target_config, str(tmp_path / name),
        readback=ReadbackConfig(kind="python", spec=f"{SPEC_MODULE}:build"),
        tag_prefix=PREFIX, label_field=label_field,
        client=pipeline, readback_client=pipeline,
        sleep=clock.sleep, clock=clock, printer=lines.append, **settings,
    )
    text = "\n".join(lines)
    path = tmp_path / name
    artifact = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    return code, artifact, text


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_a_stable_classifier_passes(target_config, tmp_path, spec_probe):
    code, artifact, text = _run(
        ClassifyingPipeline(stable_labels()), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_OK, text
    stability = artifact["stability"]
    assert stability["verdict"] == "STABLE"
    assert stability["dissent_rate"] == 0.0
    assert stability["flip_rate"] == 0.0
    assert stability["constant_label"] is None
    assert text.splitlines()[0].startswith("steady: STABLE")
    assert "'category'" in text.splitlines()[0]
    assert f"{COUNT} families" in text


def test_every_payload_reaches_the_pipeline_with_a_neutral_subject(
    target_config, tmp_path, spec_probe
):
    pipeline = ClassifyingPipeline(stable_labels())
    _code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)
    variants, families, _r = _variants()
    assert len(pipeline.received) == len(variants) == artifact["stability"]["sent"]
    hints = {f.category_hint for f in families}
    ids = {f.intent_id for f in families}
    for fields in pipeline.received:
        subject = fields["subject"].lower()
        assert not any(h in subject for h in hints), subject
        assert not any(i in subject for i in ids), subject


def test_the_artifact_names_its_schema_tool_and_lineage(target_config, tmp_path, spec_probe):
    _code, artifact, _text = _run(
        ClassifyingPipeline(stable_labels()), target_config, tmp_path, spec_probe,
        repeats=1,
    )
    assert artifact["schema_version"] == 1
    assert artifact["tool"] == "steady"
    assert artifact["aborted"] is False
    config = artifact["config"]
    assert config["tool"] == "steady" and config["label_field"] == "category"
    assert config["seed"] == SEED and config["count"] == COUNT
    for family in artifact["families"]:
        for variant in family["variants"]:
            for key in ("intent_id", "language", "transform", "label", "label_state",
                        "subject", "payload_sha256"):
                assert key in variant
    assert json.loads(json.dumps(artifact)) == artifact


# ---------------------------------------------------------------------------
# Instability
# ---------------------------------------------------------------------------


def test_a_classifier_that_loses_track_of_a_signature_is_caught(
    target_config, tmp_path, spec_probe
):
    labels = stable_labels(move=lambda f, v: v.transform == "add-signature")
    code, artifact, text = _run(
        ClassifyingPipeline(labels), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_MISMATCH
    stability = artifact["stability"]
    assert stability["dissent_rate"] > 0.10
    by_transform = stability["by_transform"]
    assert by_transform["add-signature"]["dissent_rate"] == 1.0
    assert all(b["dissenters"] == 0 for n, b in by_transform.items() if n != "add-signature")
    assert "worst transforms" in text and "add-signature" in text
    assert artifact["stability"]["gate"]["failed"] is True


def test_dissent_in_one_language_is_blamed_on_that_language(
    target_config, tmp_path, spec_probe
):
    labels = stable_labels(move=lambda f, v: v.language == "ur" and v.transform == "baseline")
    _code, artifact, _text = _run(
        ClassifyingPipeline(labels), target_config, tmp_path, spec_probe,
        max_dissent_rate=0.5,
    )
    by_language = artifact["stability"]["by_language"]
    assert by_language["ur"]["dissenters"] == COUNT
    assert by_language["en"]["dissenters"] == 0
    split = artifact["stability"]["pairs_by_language"]
    assert split["cross_language"]["flips"] > 0


def test_a_dissent_below_the_gate_still_passes(target_config, tmp_path, spec_probe):
    labels = stable_labels(move=lambda f, v: v.transform == "add-signature" and v.language == "en")
    code, artifact, text = _run(
        ClassifyingPipeline(labels), target_config, tmp_path, spec_probe,
        max_dissent_rate=0.5,
    )
    assert code == EXIT_OK, text
    assert artifact["stability"]["dissent_rate"] > 0
    assert artifact["stability"]["gate"]["failed"] is False
    assert "gate (50.0%): passed" in text


def test_the_optional_flip_gate_fails_a_run_the_dissent_gate_passes(
    target_config, tmp_path, spec_probe
):
    labels = stable_labels(move=lambda f, v: v.transform == "add-signature" and v.language == "en")
    code, artifact, _text = _run(
        ClassifyingPipeline(labels), target_config, tmp_path, spec_probe,
        max_dissent_rate=0.5, max_flip_rate=0.0,
    )
    assert code == EXIT_MISMATCH
    assert "pairwise flip rate" in artifact["stability"]["gate"]["reason"]


def test_repeats_are_measured_on_one_payload_per_family(target_config, tmp_path, spec_probe):
    pipeline = ClassifyingPipeline(stable_labels(repeats=4))
    code, artifact, text = _run(
        pipeline, target_config, tmp_path, spec_probe, repeats=4
    )
    assert code == EXIT_OK, text
    stability = artifact["stability"]
    assert stability["repeats"] == 4
    assert stability["repeat_instability"] == 0.0
    assert set(stability["repeat_by_family"]) == {f.intent_id for f in _variants()[1]}
    variants, _f, _r = _variants()
    assert len(pipeline.received) == len(variants) + COUNT * 4
    assert "repeat instability" in text


# ---------------------------------------------------------------------------
# The label field
# ---------------------------------------------------------------------------


def test_the_default_route_is_refused_before_send_when_it_cannot_vary(
    target_config, tmp_path, spec_probe
):
    """Every family is sent to one mailbox, so a route that echoes the
    recipient is the same for all of them: 0% for the wrong reason."""
    pipeline = ClassifyingPipeline(stable_labels())
    code, artifact, text = _run(
        pipeline, target_config, tmp_path, spec_probe, label_field=None
    )
    assert code == EXIT_REFUSED
    assert artifact is None
    assert pipeline.received == []
    assert "would read 'intake@example.com' for every family" in text


def test_a_named_constant_field_is_warned_about_in_the_headline(
    target_config, tmp_path, spec_probe
):
    code, artifact, text = _run(
        ClassifyingPipeline(stable_labels()), target_config, tmp_path, spec_probe,
        label_field="route",
    )
    assert code == EXIT_OK
    assert "WARNING: every family read the one label" in artifact["stability"]["verdict"]
    assert "WARNING" in text.splitlines()[0]


def test_a_run_that_cannot_see_the_label_exits_3_and_says_why(
    target_config, tmp_path, spec_probe
):
    code, artifact, text = _run(
        ClassifyingPipeline({}, blind=True), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_MISMATCH
    stability = artifact["stability"]
    assert stability["verdict"] == "NOTHING MEASURED"
    reads = stability["label_reads"]
    assert reads["not-visible"] == len(_variants()[0])
    assert reads["labelled"] == 0
    assert "not visible" in text


def test_null_labels_and_missing_records_are_counted_apart(
    target_config, tmp_path, spec_probe
):
    variants, _f, _r = _variants()
    labels = stable_labels()
    null_tag, missing_tag = variants[0][0], variants[1][0]
    labels[null_tag] = None
    pipeline = ClassifyingPipeline(labels)

    original = pipeline.fetch

    def fetch(probe):
        return [] if probe.tag == missing_tag else original(probe)

    pipeline.fetch = fetch
    code, artifact, _text = _run(pipeline, target_config, tmp_path, spec_probe)
    reads = artifact["stability"]["label_reads"]
    assert reads["null"] == 1 and reads["no-record"] == 1
    assert code == EXIT_MISMATCH, "a family that could not be fully measured fails the gate"


# ---------------------------------------------------------------------------
# A custom fixture, interruption, and the report
# ---------------------------------------------------------------------------


def test_a_custom_intents_file_is_used(target_config, tmp_path, spec_probe):
    path = tmp_path / "intents.json"
    path.write_text(json.dumps({
        "closings": {"en": ["Thanks,"]},
        "intents": [{"id": "lost-parcel", "category_hint": "shipping",
                     "subjects": {"en": ["Where is my parcel"]},
                     "variants": {"en": "My parcel was due on Monday and has not "
                                        "arrived. Could you find out where it is?"}}],
    }), encoding="utf-8")
    pipeline = ClassifyingPipeline({})
    _code, artifact, _text = _run(
        pipeline, target_config, tmp_path, spec_probe, intents_path=str(path)
    )
    assert [f["intent_id"] for f in artifact["families"]] == ["lost-parcel"]
    assert artifact["config"]["intents_path"] == str(path)
    assert all(f["subject"].endswith("Where is my parcel") for f in pipeline.received)


def test_ctrl_c_writes_a_partial_artifact_and_exits_3(target_config, tmp_path, spec_probe):
    class Interrupted(ClassifyingPipeline):
        def send(self, request):
            if len(self.received) >= 2:
                raise KeyboardInterrupt
            return super().send(request)

    code, artifact, text = _run(
        Interrupted(stable_labels()), target_config, tmp_path, spec_probe
    )
    assert code == EXIT_MISMATCH
    assert artifact["aborted"] is True
    assert artifact["stability"]["verdict"] == "ABORTED"
    assert "ABORTED" in text


def test_report_reads_a_steady_artifact(target_config, tmp_path, spec_probe):
    labels = stable_labels(move=lambda f, v: v.transform == "add-signature")
    _code, _artifact, _text = _run(
        ClassifyingPipeline(labels), target_config, tmp_path, spec_probe
    )
    summary = reporting.load(tmp_path / "steady.json")
    assert summary.tool == "steady"
    assert summary.verdict == "UNSTABLE"
    assert summary.verdict_source == "recorded"
    assert summary.exit_code == EXIT_MISMATCH
    kinds = {f.kind for f in summary.findings}
    assert {"gate", "dissent"} <= kinds
