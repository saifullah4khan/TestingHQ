"""The readback poll: when is a read believed, and when is a duplicate caught.

The single lookup this replaces was wrong in one direction that mattered. It
read once after the last send, so a pipeline that had created one ticket for a
message and was about to create a second was reported clean, and the duplicate
surfaced as a second ticket in the customer's queue minutes later.

These tests drive `read_back_all` with a virtual clock, so a five-second quiet
window costs no real time and the polling order is asserted rather than waited
for.
"""
from __future__ import annotations

import pytest

from testinghq.pipeline import common
from testinghq.pipeline.common import read_back_all
from testinghq.pipeline.messages import build_chain_message_id
from testinghq.pipeline.readback import FunctionAdapter, Readback

TAGS = ("hq-1-0000", "hq-1-0001", "hq-1-0002")


def _probes(tags=TAGS):
    from testinghq.pipeline.messages import Probe

    return [
        Probe(
            record_id=f"clean-1-{i:04d}",
            tag=tag,
            payload_sha256="a" * 64,
            from_addr="alice@example.com",
            subject="Your order",
            recipient="support@example.com",
            message_id=build_chain_message_id(tag),
        )
        for i, tag in enumerate(tags)
    ]


class _Scripted:
    """An adapter whose answer changes at a named poll.

    `script` maps poll number (1-based) to {tag: count}. Anything beyond the
    script holds its last entry, which is what makes "and then nothing changed"
    expressible without saying so.
    """

    def __init__(self, script):
        self.script = script
        self.polls = 0
        self.asked = []

    def fetch(self, probe):
        self.polls += 1
        self.asked.append(probe.tag)
        counts = self.script.get(self.polls, self.script[max(self.script)])
        return [
            Readback(exists=True, ticket_id=f"{probe.tag}#{n}")
            for n in range(counts.get(probe.tag, 0))
        ]

    def close(self):
        return None


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def _read(adapter, clock=None, **kwargs):
    clock = clock or _Clock()
    settings = dict(quiet_window=5.0, max_wait=60.0, poll_interval=0.5)
    settings.update(kwargs)
    return read_back_all(adapter, _probes(), sleep=clock.sleep, clock=clock, **settings), clock


# ---------------------------------------------------------------------------
# Settling
# ---------------------------------------------------------------------------


def test_a_system_that_answers_immediately_still_waits_the_quiet_window():
    """One answer is not evidence. Waiting the window is the only way to know
    nothing else is coming.

    The window is measured from the first poll that confirms the counts, so a
    run that settles immediately still takes the window plus one interval: 0.5s
    to notice the counts had not moved, then 5s of them holding.
    """
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, clock = _read(adapter)

    assert outcome.stable is True
    assert outcome.missing == ()
    assert outcome.elapsed == pytest.approx(5.5)
    assert all(len(found) == 1 for found in outcome.readbacks.values())


def test_polls_stop_as_soon_as_the_window_has_passed():
    """A run that waited out max_wait regardless would make every verification
    take a minute, which is how a useful check becomes one nobody runs."""
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, clock = _read(adapter)
    assert clock.now == pytest.approx(5.5)
    assert outcome.polls == 12, "t=0 through t=5.5 inclusive, at 0.5s intervals"


def test_a_slow_pipeline_is_waited_for():
    """One poll of nothing, then the counts land, then the window."""
    adapter = _Scripted({
        1: {tag: 0 for tag in TAGS},
        2: {tag: 0 for tag in TAGS},
        3: {tag: 1 for tag in TAGS},
    })
    outcome, _clock = _read(adapter)

    assert outcome.stable is True
    assert outcome.missing == ()
    assert outcome.elapsed == pytest.approx(6.0), "counts land at t=1.0, window to t=6.0"


def test_a_run_that_never_settles_gives_up_and_says_so():
    """`stable` False is a different claim from "everything was found", and a
    report that conflates them would be asserting a quiet it never saw."""
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, _clock = _read(adapter, quiet_window=999.0, max_wait=4.0)

    assert outcome.stable is False
    assert outcome.missing == ()
    assert outcome.elapsed == pytest.approx(4.0)


def test_a_run_that_gives_up_names_what_it_never_saw():
    adapter = _Scripted({1: {"hq-1-0000": 1, "hq-1-0001": 1, "hq-1-0002": 0}})
    outcome, _clock = _read(adapter, quiet_window=999.0, max_wait=2.0)

    assert outcome.stable is False
    assert outcome.missing == ("hq-1-0002",)


# ---------------------------------------------------------------------------
# The late duplicate, which is the reason this polls
# ---------------------------------------------------------------------------


def test_a_duplicate_that_appears_after_the_first_sighting_is_caught():
    """The case the single lookup missed. Poll 1 already sees one ticket, so a
    read that only required "all found" would have finished there and called the
    run clean moments before the second ticket landed."""
    adapter = _Scripted({
        1: {tag: 1 for tag in TAGS},
        2: {tag: 1 for tag in TAGS},
        3: {"hq-1-0000": 2, "hq-1-0001": 1, "hq-1-0002": 1},
    })
    outcome, _clock = _read(adapter)

    assert outcome.stable is True
    assert len(outcome.readbacks["hq-1-0000"]) == 2, outcome.readbacks
    assert len(outcome.readbacks["hq-1-0001"]) == 1


def test_a_count_moving_resets_the_window():
    """Otherwise the run would finish its window from the first sighting and
    still miss the duplicate, which is the bug with extra steps."""
    adapter = _Scripted({
        1: {tag: 1 for tag in TAGS},
        2: {tag: 1 for tag in TAGS},
        3: {"hq-1-0000": 2, "hq-1-0001": 1, "hq-1-0002": 1},
    })
    outcome, clock = _read(adapter)

    # 0.5s to poll 2, 0.5s to poll 3, then a full 5s window from poll 3.
    assert outcome.elapsed == pytest.approx(6.0), (
        "the window restarted when the count moved, so the run waited 5s again"
    )


def test_several_late_duplicates_are_all_caught():
    adapter = _Scripted({
        1: {tag: 1 for tag in TAGS},
        2: {tag: 2 for tag in TAGS},
        3: {tag: 3 for tag in TAGS},
    })
    outcome, _clock = _read(adapter)
    assert [len(outcome.readbacks[tag]) for tag in TAGS] == [3, 3, 3]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_each_tag_is_asked_once_per_poll():
    """A redelivery scenario sends the same message twice on purpose. Asking
    twice would either double-count the answer or return a different one the
    second time, and both would corrupt the accounting."""
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    probes = _probes() + _probes()
    outcome = read_back_all(
        adapter, probes, sleep=lambda _s: None, clock=_Clock(),
        quiet_window=0.0, max_wait=1.0, poll_interval=0.5,
    )
    assert adapter.asked[:3] == list(TAGS)
    assert set(adapter.asked) == set(TAGS)


def test_the_answer_is_keyed_by_tag_so_a_gap_cannot_shift_everything():
    adapter = _Scripted({1: {"hq-1-0000": 1, "hq-1-0001": 0, "hq-1-0002": 1}})
    outcome, _clock = _read(adapter, quiet_window=0.0, max_wait=1.0)
    assert outcome.readbacks["hq-1-0000"][0].ticket_id == "hq-1-0000#0"
    assert outcome.readbacks["hq-1-0001"] == []
    assert outcome.readbacks["hq-1-0002"][0].ticket_id == "hq-1-0002#0"


def test_the_outcome_serializes_the_numbers_a_report_needs():
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, _clock = _read(adapter)
    payload = outcome.to_json()
    assert payload["polls"] == outcome.polls
    assert payload["stable"] is True
    assert payload["missing_tags"] == []
    assert payload["elapsed_s"] == pytest.approx(5.5)


def test_a_zero_quiet_window_still_asks_twice():
    """Quiet window 0 is a legal setting meaning "one confirming poll", and it
    has to actually confirm. Returning on the first sighting would make the
    setting a way to disable the protection this phase exists for."""
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}, 2: {"hq-1-0000": 2,
                                                          "hq-1-0001": 1,
                                                          "hq-1-0002": 1}})
    outcome, _clock = _read(adapter, quiet_window=0.0)
    # Poll 1 sees one of each, poll 2 sees the duplicate, poll 3 confirms it.
    assert outcome.polls == 3
    assert len(outcome.readbacks["hq-1-0000"]) == 2


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quiet_window": -1.0},
        {"max_wait": -1.0},
        {"poll_interval": 0.0},
        {"poll_interval": -1.0},
    ],
)
def test_nonsense_poll_settings_are_refused_before_any_lookup(kwargs):
    class _Exploding:
        def fetch(self, probe):  # pragma: no cover - must never run
            raise AssertionError("looked something up before validating")

    with pytest.raises(ValueError):
        _read(_Exploding(), **kwargs)


def test_an_adapter_that_raises_is_not_swallowed():
    """A readback that fails halfway is a broken run, and reporting the first
    half as "the pipeline produced nothing else" would be a lie with a number
    attached."""
    def explode(probe):
        raise RuntimeError("the ticket store is down")

    with pytest.raises(RuntimeError):
        read_back_all(
            FunctionAdapter(explode), _probes(),
            sleep=lambda _s: None, clock=_Clock(),
            quiet_window=0.0, max_wait=1.0,
        )


def test_the_defaults_are_ordered_the_way_the_phase_needs():
    """A quiet window longer than max_wait would mean the phase can never settle
    and always gives up, which reads like a slow system rather than a
    misconfiguration."""
    assert common.DEFAULT_QUIET_WINDOW < common.DEFAULT_MAX_WAIT
    assert 0 < common.DEFAULT_POLL_INTERVAL < common.DEFAULT_QUIET_WINDOW
