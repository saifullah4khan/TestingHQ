"""Tests for the fixture-backed deterministic generator in web/generator.py.

No network, no real engine: this module stands in for
testinghq/blast/generate.py until the parallel engine lane lands. What
matters here is that it is genuinely deterministic and that its output
always matches the documented run-artifact schema shape.
"""
import pytest

from web import expectations, generator


def test_same_seed_mix_count_is_byte_identical():
    a = generator.generate_run(["clean", "degenerate"], 15, seed=42)
    b = generator.generate_run(["clean", "degenerate"], 15, seed=42)
    assert a == b


def test_different_seed_changes_output():
    a = generator.generate_run(["clean", "degenerate"], 15, seed=1)
    b = generator.generate_run(["clean", "degenerate"], 15, seed=2)
    assert a != b


def test_default_mix_is_all_five_categories():
    artifact = generator.generate_run([], 25, seed=0)
    assert set(artifact["config"]["mix"]) == set(expectations.CATEGORIES)


def test_mix_restricts_categories_present_in_records():
    artifact = generator.generate_run(["clean", "structurally-malformed"], 10, seed=3)
    categories_seen = {r["category"] for r in artifact["records"]}
    assert categories_seen <= {"clean", "structurally-malformed"}


def test_unknown_category_in_mix_raises():
    with pytest.raises(generator.GeneratorError):
        generator.generate_run(["not-a-real-category"], 5, seed=0)


def test_negative_count_raises():
    with pytest.raises(generator.GeneratorError):
        generator.generate_run(["clean"], -1, seed=0)


def test_record_count_matches_requested_count():
    artifact = generator.generate_run(["clean", "degenerate"], 17, seed=9)
    assert len(artifact["records"]) == 17


def test_zero_count_yields_empty_records_and_zeroed_summary():
    artifact = generator.generate_run(["clean"], 0, seed=0)
    assert artifact["records"] == []
    assert artifact["summary"]["flags"] == []
    assert artifact["summary"]["by_status_class"] == {
        "2xx": 0,
        "4xx": 0,
        "5xx": 0,
        "timeout": 0,
    }


def test_record_shape_matches_schema():
    artifact = generator.generate_run(["clean", "degenerate"], 5, seed=0)
    for record in artifact["records"]:
        assert set(record.keys()) == {
            "id",
            "category",
            "payload_sha256",
            "intended",
            "response",
            "assertion",
        }
        assert isinstance(record["id"], str)
        assert record["category"] in expectations.CATEGORIES
        assert len(record["payload_sha256"]) == 64  # hex sha256
        intended = record["intended"]
        assert set(intended.keys()) == {"from", "subject", "body_core", "attachments"}
        response = record["response"]
        assert set(response.keys()) == {"status", "latency_ms", "body_snippet"}
        assert response["status"] is None or isinstance(response["status"], int)
        assertion = record["assertion"]
        assert isinstance(assertion["passed"], bool)
        assert isinstance(assertion["mismatches"], list)


def test_synthetic_content_uses_reserved_domains_only():
    artifact = generator.generate_run(["clean"], 10, seed=0)
    for record in artifact["records"]:
        sender = record["intended"]["from"]
        assert sender.endswith("@example.com")


def test_degenerate_run_produces_both_failure_classes_at_sufficient_count():
    # The degenerate failure cycle repeats every 9 records (see
    # generator._status_for), so 20 records guarantees at least one 500 and
    # one timeout regardless of seed.
    artifact = generator.generate_run(["degenerate"], 20, seed=5)
    statuses = [r["response"]["status"] for r in artifact["records"]]
    assert 500 in statuses
    assert None in statuses
    flags = artifact["summary"]["flags"]
    assert any("returned 500" in f for f in flags)
    assert any("timed out" in f for f in flags)


def test_clean_run_produces_a_failure_at_sufficient_count():
    # The clean failure cycle repeats every 5 records, so 10 guarantees one.
    artifact = generator.generate_run(["clean"], 10, seed=0)
    statuses = [r["response"]["status"] for r in artifact["records"]]
    assert any(s != 200 for s in statuses)
    assert any("did not 2xx" in f for f in artifact["summary"]["flags"])


def test_config_block_records_dry_run_and_target():
    dry = generator.generate_run(["clean"], 3, seed=0, dry_run=True, target=None)
    assert dry["config"]["dry_run"] is True
    assert dry["config"]["target"] is None

    fired = generator.generate_run(["clean"], 3, seed=0, dry_run=False, target="staging-example")
    assert fired["config"]["dry_run"] is False
    assert fired["config"]["target"] == "staging-example"
