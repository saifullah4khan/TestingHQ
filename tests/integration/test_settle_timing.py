"""The readback's settle timing at the real flag defaults, over the http wire.

Every other settling test drives a readback that costs no time per lookup. A
real http readback is paced to --readback-rate (4 requests a second by default)
and asks every tag once a poll, so a poll of N tags costs N / 4 seconds. At
that cost the old rule, which started the quiet window at the END of the second
identical poll and checked it again only after a third, needed three full polls
to settle: 75 seconds for 100 tags against a 60 second --max-wait. A correct,
instantly consistent store then read UNSETTLED and exit 3, and the headline
said the store "was still changing" when every poll had been identical.

These runs go through the real `HttpJsonAdapter` and its rate limiter with a
virtual clock, at the default --quiet-window, --max-wait, --poll-interval and
--readback-rate, so the timing asserted is the timing a user gets. The default
--max-wait is 60s, raised to fit a plan that needs more; a run that passes
--max-wait gets exactly that.

Hermetic: the store is an in-process HTTP client, never a socket.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pipeline_under_test import PipelineUnderTest

from testinghq.core.transport import ClientResponse
from testinghq.pipeline import common
from testinghq.pipeline import ledger as ledger_tool
from testinghq.pipeline import steady as steady_tool
from testinghq.pipeline.adapters import DEFAULT_READBACK_RATE, ReadbackConfig
from testinghq.pipeline.common import EXIT_MISMATCH, EXIT_OK

TARGET = "local"
TARGET_URL = "http://localhost:9/intake"
READBACK_URL = "http://localhost:9/tickets"


class _VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class HttpStore:
    """`PipelineUnderTest` behind the http readback's wire. A POST is the
    intake; a GET with ?tag= is a lookup and a GET without one lists every
    record. Instantly consistent: a record exists from the moment its POST
    returns. `route_at`, when set, gives every record a route that depends on
    the clock, which is a store whose answers never stop changing."""

    def __init__(self, labels=None, route_at=None):
        self.pipeline = PipelineUnderTest()
        self.labels = dict(labels or {})
        self.route_at = route_at
        self.lookups = 0

    def send(self, request):
        if request.method == "POST":
            return self.pipeline.send(request)
        tag = (parse_qs(urlsplit(request.url).query).get("tag") or [None])[0]
        if tag is not None:
            self.lookups += 1
            records = self.pipeline.records_for(tag)
        else:
            records = list(self.pipeline.messages)
        body = json.dumps([self._json(record) for record in records]).encode("utf-8")
        return ClientResponse(status=200, body=body)

    def _json(self, record):
        return {
            "id": record.ticket_id,
            "from": record.from_addr,
            "subject": record.subject,
            "body": record.body,
            "attachments": list(record.attachment_names),
            "route": self.route_at() if self.route_at else record.route,
            "message_id": record.message_id,
            "in_reply_to": record.in_reply_to,
            "references": list(record.references),
            "tag": record.tag,
            "category": self.labels.get(record.tag),
        }

    def close(self):
        return None


@pytest.fixture
def target_config(tmp_path) -> str:
    path = tmp_path / "target.toml"
    path.write_text(f'[targets.{TARGET}]\nurl = "{TARGET_URL}"\n', encoding="utf-8")
    return str(path)


def _readback() -> ReadbackConfig:
    config = ReadbackConfig(kind="http", url=READBACK_URL)
    assert config.rate == DEFAULT_READBACK_RATE == 4.0
    return config


def _ledger(store, clock, target_config, tmp_path, count, **flags):
    lines: list = []
    code = ledger_tool.execute(
        7, count, TARGET, target_config, str(tmp_path / "ledger.json"),
        readback=_readback(), tag_prefix="settle",
        client=store, readback_client=store, sleep=clock.sleep, clock=clock,
        printer=lines.append, **flags,
    )
    artifact = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    return code, artifact, "\n".join(lines)


def test_100_tags_at_default_flags_settle_on_an_instantly_consistent_store(
    target_config, tmp_path
):
    """The reviewer's run: 100 tags, default flags, a correct store. Two
    identical polls of 25s each are the whole answer: the quiet window counts
    from the start of the first of them, so the run settles at the end of the
    second, inside --max-wait, BALANCED with nothing missing."""
    store = HttpStore()
    clock = _VirtualClock()
    code, artifact, text = _ledger(store, clock, target_config, tmp_path, 100)

    summary = artifact["summary"]
    readback = summary["readback"]
    assert code == EXIT_OK, text
    assert summary["verdict"] == "BALANCED", text
    assert readback["stable"] is True
    assert readback["polls"] == 2
    assert readback["missing_tags"] == []
    assert readback["elapsed_s"] < common.DEFAULT_MAX_WAIT
    assert store.lookups == 1 + 200, "the preflight lookup, then every tag twice"
    assert "UNSETTLED" not in text
    assert "settled" in text


def test_a_store_still_changing_at_max_wait_is_unsettled_and_says_still_changing(
    target_config, tmp_path
):
    """The same run against a store whose route field moves every ten
    seconds: no two polls agree, so --max-wait runs out with the state still
    changing. UNSETTLED, exit 3, and the headline says still changing."""
    clock = _VirtualClock()
    store = HttpStore(route_at=lambda: f"queue-{int(clock.now // 10)}")
    code, artifact, text = _ledger(store, clock, target_config, tmp_path, 100)
    # 2 x 25 + 5 = 55s, plus 25%: the default sized to the plan.
    assert artifact["config"]["readback_poll"]["max_wait"] == 69.0

    summary = artifact["summary"]
    assert code == EXIT_MISMATCH, text
    assert summary["verdict"] == common.UNSETTLED, text
    assert summary["readback"]["cause"] == common.CAUSE_CHANGING
    assert summary["readback"]["complete_polls"] == 2
    assert "the store was still changing: the last two complete polls differed" in text
    assert "UNSETTLED, still changing at --max-wait" in text
    assert "not enough time" not in text


def test_a_run_too_big_to_poll_twice_says_so_with_the_numbers(target_config, tmp_path):
    """300 tags at 4/s is 75s a poll, so not even one poll fits in a --max-wait
    of 60 given on the command line, which is honoured as given. Nothing
    was seen changing, so the headline must not say it was: it names the
    tags, the rate, --max-wait, and the --max-wait or --readback-rate that
    would fit (2 x 75 + 5 = 155s; 600 lookups in 55s is 10.91/s)."""
    store = HttpStore()
    clock = _VirtualClock()
    code, artifact, text = _ledger(
        store, clock, target_config, tmp_path, 300, max_wait=60.0
    )
    assert artifact["config"]["readback_poll"]["max_wait"] == 60.0
    assert artifact["config"]["readback_poll"]["max_wait_source"] == "given"

    summary = artifact["summary"]
    assert code == EXIT_MISMATCH, text
    assert summary["verdict"] == common.UNSETTLED, text
    assert summary["readback"]["cause"] == common.CAUSE_NO_TIME
    assert "still changing" not in text
    assert (
        "not enough time to poll every tag twice: 300 tag(s) at --readback-rate "
        "4/s take about 75.0s a poll, and --max-wait is 60s"
    ) in text
    assert "raise --max-wait to at least 155, or --readback-rate to at least 11" in text
    assert "UNSETTLED, not enough time within --max-wait" in text


def test_the_same_run_at_the_default_max_wait_gets_time_to_settle(
    target_config, tmp_path
):
    """Without --max-wait the default grows with the plan: 300 tags need
    2 x 75 + 5 = 155s, plus 25% is 193.75, so 194s. A correct store settles
    BALANCED, and the artifact records the max-wait used and that it was the
    default."""
    store = HttpStore()
    clock = _VirtualClock()
    code, artifact, text = _ledger(store, clock, target_config, tmp_path, 300)

    summary = artifact["summary"]
    poll = artifact["config"]["readback_poll"]
    assert code == EXIT_OK, text
    assert summary["verdict"] == "BALANCED", text
    assert summary["readback"]["stable"] is True
    assert summary["readback"]["polls"] == 2
    assert poll["max_wait"] == 194.0
    assert poll["max_wait_source"] == common.MAX_WAIT_DEFAULT
    assert "UNSETTLED" not in text


# ---------------------------------------------------------------------------
# steady at its defaults
# ---------------------------------------------------------------------------


def _steady(clock, target_config, tmp_path, repeats, seed=5, **flags):
    count = steady_tool.DEFAULT_COUNT
    families = steady_tool.load_families(seed, count)
    items = steady_tool.build_items(families, tag_prefix="steady", seed=seed)
    labels = {}
    index = 0
    for family in families:
        for _variant in family.variants:
            labels[items[index][1]] = family.category_hint
            index += 1
    if repeats > 1:
        repeated = steady_tool.repeat_items(families, repeats, "steady", seed)
        for position, family in enumerate(families):
            for _e, tag, _r, _p in repeated[position * repeats:(position + 1) * repeats]:
                labels[tag] = family.category_hint
    store = HttpStore(labels=labels)
    lines: list = []
    code = steady_tool.execute(
        seed, count, TARGET, target_config, str(tmp_path / "steady.json"),
        readback=_readback(), tag_prefix="steady", label_field="category",
        repeats=repeats, client=store, readback_client=store,
        sleep=clock.sleep, clock=clock, printer=lines.append, **flags,
    )
    artifact = json.loads((tmp_path / "steady.json").read_text(encoding="utf-8"))
    return code, artifact, "\n".join(lines), len(labels)


def test_steady_at_its_defaults_settles(target_config, tmp_path):
    """steady's default plan is six families, about 107 tags. Under the old
    rule it could never settle at the default flags."""
    code, artifact, text, tags = _steady(_VirtualClock(), target_config, tmp_path, 1)
    assert tags > 100
    assert code == EXIT_OK, text
    assert artifact["stability"]["verdict"] == "STABLE", text
    assert text.splitlines()[0].startswith("steady: STABLE")


def test_steady_with_repeats_at_the_default_max_wait_is_stable(target_config, tmp_path):
    """The README's run: the default seed with --repeats 3 is 123 tags, about
    31s a poll at 4/s, so two polls and the window need about 66.5s, over a
    flat 60s. Every steady run like it used to end UNSETTLED, exit 3, on a
    correct pipeline. The default --max-wait now fits the plan: 66.5s plus
    25% is 84s."""
    code, artifact, text, tags = _steady(
        _VirtualClock(), target_config, tmp_path, 3, seed=steady_tool.DEFAULT_SEED
    )
    assert tags == 123
    assert 2 * tags / 4.0 + common.DEFAULT_QUIET_WINDOW > common.DEFAULT_MAX_WAIT
    assert code == EXIT_OK, text
    stability = artifact["stability"]
    assert stability["verdict"] == "STABLE", text
    assert stability["readback"]["stable"] is True
    assert artifact["config"]["readback_poll"]["max_wait"] == 84.0
    assert artifact["config"]["readback_poll"]["max_wait_source"] == "default"
    assert "gate (10.0%): passed" in text
    assert "UNSETTLED" not in text


def test_steady_headline_says_unsettled_on_an_unsettled_run(target_config, tmp_path):
    """The same 123 tags with --max-wait 60 given: honoured as given, so two
    polls do not fit. The first line used to say STABLE while the gate below
    it said FAILED; the headline is the readback's verdict, as the artifact's
    is. And the gate is not judged: "gate (10.0%): FAILED" above "majority
    dissent 0.0% is within the gate" contradicted itself."""
    code, artifact, text, tags = _steady(
        _VirtualClock(), target_config, tmp_path, 3, seed=steady_tool.DEFAULT_SEED,
        max_wait=60.0,
    )
    assert 2 * tags / 4.0 > common.DEFAULT_MAX_WAIT
    assert code == EXIT_MISMATCH, text
    assert artifact["stability"]["verdict"] == common.UNSETTLED
    assert artifact["stability"]["gate"]["judged"] is False
    first = text.splitlines()[0]
    assert first.startswith("steady: UNSETTLED"), first
    assert "STABLE" not in first
    assert "gate (10.0%): not judged" in text
    assert "gate (10.0%): FAILED" not in text
    assert "is within the" not in text
    assert "not judged, because the readback did not settle" in text
    assert "UNSETTLED, not enough time within --max-wait" in text
    assert "not enough time to poll every tag twice" in text
