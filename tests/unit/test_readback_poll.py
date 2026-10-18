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

    The window counts from the start of the first of two identical polls, the
    moment the state was first observed, so a store that answers the same
    thing from t=0 settles at the first poll at or after t=5.0.
    """
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, clock = _read(adapter)

    assert outcome.stable is True
    assert outcome.missing == ()
    assert outcome.elapsed == pytest.approx(5.0)
    assert all(len(found) == 1 for found in outcome.readbacks.values())


def test_polls_stop_as_soon_as_the_window_has_passed():
    """A run that waited out max_wait regardless would make every verification
    take a minute, which is how a useful check becomes one nobody runs."""
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, clock = _read(adapter)
    assert clock.now == pytest.approx(5.0)
    assert outcome.polls == 11, "t=0 through t=5.0 inclusive, at 0.5s intervals"


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
    assert outcome.elapsed == pytest.approx(5.5), (
        "the counts are complete from the t=0.5 poll, so the window runs to t=5.5"
    )


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

    # Poll 2 (t=0.5) sees the duplicate, poll 3 confirms it, and the 5s window
    # counts from the start of poll 2.
    assert outcome.elapsed == pytest.approx(5.5), (
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
    # Same shape as the answers-immediately case above: the window counts from
    # the first of the two identical polls, t=0, and the t=0.5 poll closes it.
    assert outcome.elapsed == pytest.approx(0.5)
    assert outcome.missing == TAGS, "absence is recorded, it is just not blocking"
    assert clock.now == pytest.approx(0.5)


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
    assert clock.now == pytest.approx(1.0), "settled on the window, not max_wait"


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
    assert clock.now == pytest.approx(5.0), "settled on the window, not on max_wait"


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
    # The value changed at t=3.0, and the window counts 5s from that poll.
    assert outcome.elapsed == pytest.approx(8.0)


def test_a_quiet_store_shorter_than_max_wait_but_longer_than_the_window_settles():
    """The rule's other half: max_wait is only an upper bound. A store that
    goes quiet settles as soon as the window has passed, missing tags or not,
    and never waits out the budget."""
    adapter = _Scripted({1: {"hq-1-0000": 1}})
    outcome, clock = _read(adapter, quiet_window=2.0, max_wait=600.0)
    assert outcome.stable is True
    assert outcome.missing == ("hq-1-0001", "hq-1-0002")
    assert clock.now == pytest.approx(2.0)


def test_the_outcome_serializes_the_numbers_a_report_needs():
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, _clock = _read(adapter)
    payload = outcome.to_json()
    assert payload["polls"] == outcome.polls
    assert payload["stable"] is True
    assert payload["missing_tags"] == []
    assert payload["elapsed_s"] == pytest.approx(5.0)


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
    closes at t=2.0 on the interim "triage" value. Watching the values resets
    the window at each change and reports the route the record ended with."""

    class _RoutedLate:
        def __init__(self):
            self.polls = 0

        def fetch(self, probe):
            if probe.tag == TAGS[0]:
                self.polls += 1
            route = None
            if self.polls == 5:
                route = "triage"
            elif self.polls >= 6:
                route = "billing"
            return [Readback(exists=True, ticket_id=f"{probe.tag}#0", route=route)]

    outcome, _clock = _read(_RoutedLate(), quiet_window=2.0)
    assert outcome.stable
    assert outcome.readbacks[TAGS[0]][0].route == "billing"
    # triage at t=2.0, billing at t=2.5, confirmed at t=3.0: the window counts
    # 2s from the t=2.5 poll.
    assert outcome.elapsed == pytest.approx(4.5)


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


# ---------------------------------------------------------------------------
# Running out of --max-wait: SETTLED, or UNSETTLED and why
# ---------------------------------------------------------------------------


def test_running_out_after_two_identical_polls_and_a_full_window_is_settled():
    """Three tags at 1s a lookup: polls at [0,3] and [3.5,6.5] agree, which is
    6.5s of quiet against an 8s window. The third poll is cut short at the 9s
    budget having seen nothing new, and by then the state has held for 9s
    from the start of the first identical poll. Running out of time while
    watching a quiet store is not a store still changing."""
    clock = _Clock()
    adapter = _Slow(clock, cost=1.0)
    outcome, _clock = _read(adapter, clock=clock, quiet_window=8.0, max_wait=9.0)
    assert outcome.complete_polls == 2
    assert outcome.budget_exhausted is True
    assert outcome.stable is True
    assert outcome.headline() is None
    assert outcome.to_json()["cause"] is None


def test_a_change_in_the_poll_the_budget_cut_short_is_still_changing():
    """The same timing, but the cut-short third poll sees a second ticket:
    the state was changing when the budget ran out."""
    clock = _Clock()

    class _GrowsLate(_Slow):
        def fetch(self, probe):
            records = super().fetch(probe)
            if self.clock.now > 7.0:
                records.append(Readback(exists=True, ticket_id=f"{probe.tag}#late"))
            return records

    outcome, _clock = _read(
        _GrowsLate(clock, cost=1.0), clock=clock, quiet_window=8.0, max_wait=9.0
    )
    assert outcome.unsettled
    assert outcome.cause == common.CAUSE_CHANGING
    assert "still changing" in outcome.headline()


def test_a_change_inside_the_last_window_is_still_changing():
    """The last two complete polls agree, but the change they agree on was
    seen at t=8.0, less than the 9s window before the 10s budget ran out."""
    clock = _Clock()

    class _ChangesAt(object):
        def fetch(self, probe):
            count = 2 if clock.now >= 8.0 else 1
            return [Readback(exists=True, ticket_id=f"{probe.tag}#{n}") for n in range(count)]

    outcome, _clock = _read(_ChangesAt(), clock=clock, quiet_window=9.0, max_wait=10.0)
    assert outcome.unsettled
    assert outcome.cause == common.CAUSE_CHANGING
    assert "the last change was seen 2.0s before --max-wait ran out" in outcome.headline()
    assert outcome.state_phrase() == "UNSETTLED, still changing at --max-wait"


def test_a_max_wait_shorter_than_the_window_is_not_enough_time_not_changing():
    """Nothing ever changed: the headline must not claim it did."""
    adapter = _Scripted({1: {tag: 1 for tag in TAGS}})
    outcome, _clock = _read(adapter, quiet_window=999.0, max_wait=4.0)
    assert outcome.unsettled
    assert outcome.cause == common.CAUSE_NO_TIME
    headline = outcome.headline()
    assert "still changing" not in headline
    assert "nothing changed over 8 poll(s)" in headline
    assert "--max-wait 4s ran out before the state had held for --quiet-window 999s" in headline
    assert "raise --max-wait to at least 999, so it covers --quiet-window" in headline
    assert outcome.to_json()["cause"] == common.CAUSE_NO_TIME


def test_one_poll_that_does_not_fit_is_not_enough_time_with_the_numbers():
    """An unpaced adapter (no --readback-rate applies) whose one poll
    outlasts the budget: the headline gives the measured poll and the
    --max-wait that would fit."""
    clock = _Clock()
    outcome, _clock = _read(_Slow(clock, cost=25.0), clock=clock, max_wait=30.0)
    assert outcome.cause == common.CAUSE_NO_TIME
    headline = outcome.headline()
    assert "not enough time to poll every tag twice" in headline
    assert "a poll of 3 tag(s) took more than 50.0s" in headline
    assert "raise --max-wait to at least 105" in headline


def test_an_artifact_without_a_cause_does_not_claim_one():
    assert common.readback_state_phrase({"stable": False}) == "UNSETTLED at --max-wait"
    assert common.readback_state_phrase({"stable": True}) == "settled"
    assert common.readback_state_phrase(
        {"stable": False, "cause": common.CAUSE_NO_TIME}
    ) == "UNSETTLED, not enough time within --max-wait to confirm the store quiet"


# ---------------------------------------------------------------------------
# The dry-run warning
# ---------------------------------------------------------------------------


def test_the_settle_warning_is_silent_when_two_polls_and_the_window_fit():
    # 2 x 100 / 4 + 5 = 55s inside 60s.
    assert common.settle_time_warning(100, 4.0, 5.0, 60.0) is None
    assert common.settle_time_warning(10_000, None, 5.0, 60.0) is None


def test_the_settle_warning_names_the_numbers_and_the_fix():
    # 2 x 120 / 4 + 5 = 65s, over 60s; 240 lookups in 55s is 4.37/s.
    warning = common.settle_time_warning(120, 4.0, 5.0, 60.0)
    assert warning.startswith("warning: a real run may end UNSETTLED for lack of time")
    assert "120 tag(s) at --readback-rate 4/s take about 30.0s a poll" in warning
    assert "about 65.0s, over --max-wait 60s" in warning
    assert "Raise --max-wait to at least 65, or --readback-rate to at least 4.4" in warning


def test_the_settle_warning_catches_a_window_longer_than_max_wait_unpaced():
    warning = common.settle_time_warning(3, None, 30.0, 10.0)
    assert "--quiet-window 30s, over --max-wait 10s" in warning
    assert "Raise --max-wait to at least 30" in warning


# ---------------------------------------------------------------------------
# The plan-sized --max-wait default
# ---------------------------------------------------------------------------


def test_a_max_wait_not_given_grows_with_the_plan():
    from testinghq.pipeline.adapters import ReadbackConfig

    http = ReadbackConfig(kind="http", url="http://localhost:9/tickets")
    # 2 x 123 / 4 + 5 = 66.5s, plus 25% is 83.125: 84s.
    resolved = common.resolve_max_wait(None, 123, http, 5.0)
    assert resolved.seconds == 84.0
    assert resolved.source == common.MAX_WAIT_DEFAULT
    assert resolved.raised
    assert resolved.needed == common.settle_time_needed(123, 4.0, 5.0) == 66.5
    assert common.settle_time_warning(123, 4.0, 5.0, resolved.seconds) is None
    # A small plan keeps the floor; an unpaced readback only needs the window.
    assert common.resolve_max_wait(None, 10, http, 5.0).seconds == common.DEFAULT_MAX_WAIT
    assert common.resolve_max_wait(None, 10_000, None, 5.0).seconds == common.DEFAULT_MAX_WAIT
    assert common.resolve_max_wait(None, 3, None, 100.0).seconds == 125.0


def test_a_max_wait_given_is_honoured_exactly():
    from testinghq.pipeline.adapters import ReadbackConfig

    http = ReadbackConfig(kind="http", url="http://localhost:9/tickets")
    resolved = common.resolve_max_wait(30.0, 123, http, 5.0)
    assert resolved.seconds == 30.0
    assert resolved.source == common.MAX_WAIT_GIVEN
    assert not resolved.raised
    assert common.readback_poll_settings(
        123, http, quiet_window=5.0, max_wait=30.0, poll_interval=0.5
    ) == {"quiet_window": 5.0, "max_wait": 30.0, "max_wait_source": "given",
          "poll_interval": 0.5}
