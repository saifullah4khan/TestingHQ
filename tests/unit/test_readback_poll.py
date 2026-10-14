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
from testinghq.pipeline.readback import FetchResult, FunctionAdapter, Readback

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
    read_back_all(
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


def test_absent_counts_as_settled_when_the_caller_says_so():
    """`loop` asks whether a pipeline filed a ticket for machine-generated
    mail, and under the default ticket policy a CORRECT pipeline files none, so
    "every tag found" is a condition its run can never satisfy. It passes
    `require_all_found=False` for that reason. The flag is now a no-op, since
    absence never holds the window open for any caller, and loop still gets the
    behaviour it asked for."""
    adapter = _Scripted({1: {}, 2: {}})
    outcome, clock = _read(adapter, require_all_found=False, quiet_window=0.5)
    assert outcome.stable is True
    # One interval to see the counts, then the window. Same shape as the
    # answers-immediately case above: the window starts when the counts are
    # first seen unchanged, so the total is one interval longer than the window.
    assert outcome.elapsed == pytest.approx(1.0)
    assert outcome.missing == TAGS, "absence is recorded, it is just not blocking"
    assert clock.now == pytest.approx(1.0)


def test_a_store_that_stays_empty_settles_with_every_tag_missing():
    """Absence does not hold the window open. An adapter that finds nothing,
    and keeps finding nothing for the whole quiet window, has answered: every
    tag is MISSING and the run is settled, so the tool reports a loss. It used
    to poll until max_wait and call that UNSETTLED, which is how a pipeline
    that dropped everything never got reported as having lost anything."""
    adapter = _Scripted({1: {}})
    outcome, clock = _read(adapter, quiet_window=1.0, max_wait=60.0)
    assert outcome.stable is True
    assert outcome.unsettled is False
    assert outcome.missing == TAGS
    assert outcome.headline() is None
    assert clock.now == pytest.approx(1.5), "settled on the window, not max_wait"


def test_a_late_duplicate_is_still_caught_with_the_relaxation():
    """Absence not holding the window open must not cost the thing the poll
    exists for. Counts still gate stability, so a second ticket arriving resets
    the window."""
    adapter = _Scripted({
        1: {tag: 1 for tag in TAGS},
        2: {tag: 1 for tag in TAGS},
        3: {"hq-1-0000": 2, "hq-1-0001": 1, "hq-1-0002": 1},
    })
    outcome, _clock = _read(adapter, require_all_found=False, quiet_window=0.0)
    assert len(outcome.readbacks["hq-1-0000"]) == 2


# ---------------------------------------------------------------------------
# Loss: a tag the store goes quiet without is MISSING, not UNSETTLED
# ---------------------------------------------------------------------------


def test_a_dropped_tag_is_missing_once_the_store_goes_quiet():
    """A pipeline that dropped one of three messages. The two it kept are
    there from the first poll and nothing changes after, so the store has
    answered: the run settles on the window and the third tag is MISSING. It
    used to keep polling until max_wait and call the loss UNSETTLED."""
    adapter = _Scripted({1: {"hq-1-0000": 1, "hq-1-0001": 1, "hq-1-0002": 0}})
    outcome, clock = _read(adapter)

    assert outcome.stable is True
    assert outcome.unsettled is False
    assert outcome.missing == ("hq-1-0002",)
    assert outcome.headline() is None
    assert common.readback_verdict("UNACCOUNTED", outcome) == ("UNACCOUNTED", "tool")
    assert clock.now == pytest.approx(5.5), "settled on the window, not on max_wait"


def test_a_store_still_writing_at_max_wait_is_unsettled():
    """Records still arriving when the budget runs out: one new tag every
    second, never a quiet five seconds. The tags not seen yet are not lost,
    because the store had not finished answering."""

    class _Arriving:
        def __init__(self, clock):
            self.clock = clock

        def fetch(self, probe):
            # Tag i becomes visible at t = 4 * i seconds, so tag 2 lands at 8s,
            # after a 6s budget, and tag 1 at 4s keeps the window from closing.
            index = TAGS.index(probe.tag)
            if self.clock.now >= 4.0 * index:
                return [Readback(exists=True, ticket_id=f"{probe.tag}#0")]
            return []

    clock = _Clock()
    outcome, _clock = _read(_Arriving(clock), clock=clock, max_wait=6.0)
    assert outcome.stable is False
    assert outcome.unsettled is True
    assert outcome.missing == ("hq-1-0002",)
    assert outcome.headline().startswith(common.UNSETTLED)
    assert common.readback_verdict("UNACCOUNTED", outcome) == (
        common.UNSETTLED, common.VERDICT_SOURCE_READBACK,
    )


def test_a_late_field_inside_the_window_is_seen_while_a_tag_stays_missing():
    """The quiet window still watches values, now that absence no longer holds
    it open. Tag 0 is created with no route and routed 3s later, inside the 5s
    window; tag 2 was dropped. The run settles on the routed value and reports
    the dropped tag, where it used to give up at max_wait on the missing tag."""

    class _RoutedLateOneDropped:
        def __init__(self, clock):
            self.clock = clock

        def fetch(self, probe):
            if probe.tag == "hq-1-0002":
                return []
            route = "billing" if self.clock.now >= 3.0 else None
            return [Readback(exists=True, ticket_id=f"{probe.tag}#0", route=route)]

    clock = _Clock()
    outcome, _clock = _read(_RoutedLateOneDropped(clock), clock=clock)
    assert outcome.stable is True
    assert outcome.readbacks["hq-1-0000"][0].route == "billing"
    assert outcome.missing == ("hq-1-0002",)
    # The value changed at t=3.0, then one interval to confirm and 5s quiet.
    assert outcome.elapsed == pytest.approx(8.5)


def test_a_quiet_store_shorter_than_max_wait_but_longer_than_the_window_settles():
    """The rule's other half: max_wait is only an upper bound. A store that
    goes quiet settles as soon as the window has passed, missing tags or not,
    and never waits out the budget."""
    adapter = _Scripted({1: {"hq-1-0000": 1}})
    outcome, clock = _read(adapter, quiet_window=2.0, max_wait=600.0)
    assert outcome.stable is True
    assert outcome.missing == ("hq-1-0001", "hq-1-0002")
    assert clock.now == pytest.approx(2.5)


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


# ---------------------------------------------------------------------------
# The --max-wait budget, UNSETTLED, and what the quiet window watches
# ---------------------------------------------------------------------------


class _Slow:
    """An adapter whose every lookup costs `cost` seconds of the clock."""

    def __init__(self, clock, cost, found=1):
        self.clock = clock
        self.cost = cost
        self.found = found
        self.lookups = 0

    def fetch(self, probe):
        self.lookups += 1
        self.clock.now += self.cost
        return [
            Readback(exists=True, ticket_id=f"{probe.tag}#{n}")
            for n in range(self.found)
        ]


def test_max_wait_is_enforced_between_lookups_inside_one_poll():
    """Three tags at 25s a lookup is 75s for one poll. The budget is 30s, so
    the poll stops after the second lookup instead of finishing the pass."""
    clock = _Clock()
    adapter = _Slow(clock, cost=25.0)
    outcome, _clock = _read(adapter, clock=clock, max_wait=30.0)
    assert adapter.lookups == 2
    assert outcome.polls == 1
    assert outcome.budget_exhausted is True
    assert outcome.unsettled is True


def test_a_run_that_never_settles_is_unsettled_not_lost():
    adapter = _Scripted({1: {"hq-1-0000": 1}})
    outcome, _clock = _read(adapter, max_wait=3.0)
    assert outcome.unsettled
    assert outcome.headline().startswith(common.UNSETTLED)
    payload = outcome.to_json()
    assert payload["verdict"] == common.UNSETTLED
    assert payload["unsettled"] is True
    assert common.readback_verdict("LOSSY", outcome) == (
        common.UNSETTLED, common.VERDICT_SOURCE_READBACK,
    )


def test_a_settled_run_keeps_the_tools_own_verdict():
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, _clock = _read(adapter)
    assert not outcome.unsettled
    assert outcome.headline() is None
    assert outcome.to_json()["verdict"] is None
    assert common.readback_verdict("BALANCED", outcome) == ("BALANCED", "tool")
    assert common.readback_verdict("BALANCED", None) == ("BALANCED", "tool")


def test_a_field_filled_after_the_record_appears_resets_the_window():
    """An async pipeline creates the ticket first, triages it, then routes it.
    The count is steady from the first poll, so a window over counts alone
    closes at t=2.5 on the interim "triage" value. Watching the values resets
    the window at each change and reports the route the record ended with."""

    class _RoutedLate:
        def __init__(self):
            self.polls = 0

        def fetch(self, probe):
            if probe.tag == TAGS[0]:
                self.polls += 1
            route = None
            if self.polls == 6:
                route = "triage"
            elif self.polls >= 7:
                route = "billing"
            return [Readback(exists=True, ticket_id=f"{probe.tag}#0", route=route)]

    outcome, _clock = _read(_RoutedLate(), quiet_window=2.0)
    assert outcome.stable
    assert outcome.readbacks[TAGS[0]][0].route == "billing"
    assert outcome.elapsed == pytest.approx(5.5)


def test_every_lookup_answering_404_is_named_as_a_wrong_url():
    def not_found(probe):
        return FetchResult([], status=404)

    outcome, _clock = _read(FunctionAdapter(not_found), max_wait=2.0)
    assert outcome.all_404 is True
    assert outcome.headline() == common.ALL_404_HEADLINE
    assert outcome.to_json()["all_404"] is True


def test_some_404s_among_answers_are_not_all_404():
    def mixed(probe):
        if probe.tag == TAGS[0]:
            return FetchResult(
                [Readback(exists=True, ticket_id="1")], status=200
            )
        return FetchResult([], status=404)

    outcome, _clock = _read(FunctionAdapter(mixed), max_wait=2.0)
    assert outcome.all_404 is False


def test_running_out_of_budget_inside_the_adapter_ends_the_poll_quietly():
    from testinghq.pipeline.adapters import BudgetExhausted

    def out_of_time(probe):
        raise BudgetExhausted("would retry past the --max-wait budget")

    outcome, _clock = _read(FunctionAdapter(out_of_time))
    assert outcome.budget_exhausted is True
    assert outcome.unsettled is True
    assert "budget" in outcome.detail


def test_an_adapter_with_bind_clock_is_given_the_deadline_then_released():
    class _Bindable(_Scripted):
        def __init__(self, script):
            super().__init__(script)
            self.bound = []

        def bind_clock(self, clock, sleep, deadline=None):
            self.bound.append(deadline)

    adapter = _Bindable({1: {tag: 1 for tag in TAGS}})
    _outcome, _clock = _read(adapter, max_wait=60.0)
    assert adapter.bound == [60.0, None]


def test_a_partial_lookup_is_named_in_the_outcome():
    def truncated(probe):
        return FetchResult(
            [Readback(exists=True, ticket_id=probe.tag)],
            partial=probe.tag == TAGS[1],
            status=200,
        )

    outcome, _clock = _read(FunctionAdapter(truncated))
    assert outcome.partial == (TAGS[1],)
    assert outcome.to_json()["partial_tags"] == [TAGS[1]]


def test_every_lookup_a_404_is_readback_failed_from_the_readback():
    """Not a loss and not unsettled: the run could not establish the answer."""

    def not_found(probe):
        return FetchResult([], status=404)

    outcome, _clock = _read(FunctionAdapter(not_found), max_wait=2.0)
    assert common.readback_verdict("UNACCOUNTED", outcome) == (
        "READBACK FAILED", "readback",
    )
    assert outcome.to_json()["verdict"] == "READBACK FAILED"


def test_some_404s_leave_the_tools_verdict_alone():
    def mixed(probe):
        if probe.tag == TAGS[0]:
            return FetchResult([Readback(exists=True, ticket_id="1")], status=200)
        return FetchResult([], status=404)

    outcome, _clock = _read(FunctionAdapter(mixed), max_wait=2.0)
    assert common.readback_verdict("UNACCOUNTED", outcome)[0] != "READBACK FAILED"
