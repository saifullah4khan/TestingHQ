"""The delivery-semantics scenarios: what they send, and what they check.

Two halves, and both matter. The first is that each scenario sends what a real
provider would send, because a delivery-semantics test that sends something
else is measuring something else. The second is that the judging catches a
pipeline that handles the delivery wrongly, and that it distinguishes the two
threading failures from each other.

The bug these tests were written after finding: the four scenarios share the
generated corpus, a generated payload carries a Message-ID, and a correct
pipeline deduplicates on Message-ID. So the slow-retry scenario delivered
messages the duplicate scenario had already delivered, the pipeline correctly
recognised them as redeliveries, and the slow-retry scenario reported that the
system held no record of any of its own messages. Nothing was wrong with
anything except the test design, which is the hardest kind of bug to see
because every component was behaving correctly.
"""
from __future__ import annotations

import pytest

from testinghq.core import report
from testinghq.pipeline import messages as pipeline_messages
from testinghq.pipeline import redeliver
from testinghq.pipeline.readback import Readback

SEED = 11
COUNT = 3

ALL_FIELDS = (
    "ticket_id",
    "from_addr",
    "subject",
    "body",
    "attachment_names",
    "route",
    "message_id",
    "in_reply_to",
    "references",
)


def _scenarios(names=None):
    return redeliver.build_scenarios(SEED, COUNT, "hq", 5.0, names)


def _by_name(name):
    return next(s for s in _scenarios([name]) if s.name == name)


# ---------------------------------------------------------------------------
# The plan: what each scenario sends
# ---------------------------------------------------------------------------


def test_there_are_four_scenarios_and_they_are_the_documented_four():
    assert redeliver.SCENARIOS == ("duplicate", "slow-retry", "reply-first", "references")


def test_the_duplicate_scenario_delivers_each_message_twice():
    scenario = _by_name(redeliver.SCENARIO_DUPLICATE)
    assert len(scenario.deliveries) == COUNT * 2
    tags = [d.tag for d in scenario.deliveries]
    assert len(set(tags)) == COUNT
    # Consecutive pairs: the duplicate arrives back to back, which is what a
    # provider does when its first delivery was refused and it retried at once.
    assert tags[0::2] == tags[1::2]


def test_the_duplicate_scenario_sends_byte_identical_payloads():
    """This is what makes the double a duplicate. If the two differed by so
    much as a marker count, a pipeline deduplicating on payload bytes would be
    right to keep both and this scenario would be measuring nothing."""
    scenario = _by_name(redeliver.SCENARIO_DUPLICATE)
    for first, second in zip(scenario.deliveries[0::2], scenario.deliveries[1::2]):
        assert report.payload_sha256(first.email) == report.payload_sha256(second.email)
        assert first.message_id == second.message_id


def test_the_duplicate_scenario_does_not_delay_the_second_copy():
    scenario = _by_name(redeliver.SCENARIO_DUPLICATE)
    assert all(d.delay_before == 0.0 for d in scenario.deliveries)


def test_the_slow_retry_scenario_delays_only_the_second_copy():
    scenario = _by_name(redeliver.SCENARIO_SLOW_RETRY)
    assert [d.delay_before for d in scenario.deliveries] == [0.0, 5.0] * COUNT


def test_the_slow_retry_gap_is_configurable():
    """A pipeline with a long deduplication window is being asked a different
    question, and testing it at five seconds would answer one nobody asked."""
    scenario = redeliver.build_slow_retry(
        redeliver.build_clean_corpus(SEED, 2), SEED, "hq", 42.0
    )
    assert [d.delay_before for d in scenario.deliveries] == [0.0, 42.0, 0.0, 42.0]


def test_a_negative_retry_gap_is_refused():
    with pytest.raises(redeliver.ScenarioError):
        redeliver.build_slow_retry(redeliver.build_clean_corpus(SEED, 1), SEED, "hq", -1.0)


def test_the_reply_first_scenario_delivers_the_replies_before_their_parent():
    scenario = _by_name(redeliver.SCENARIO_REPLY_FIRST)
    assert len(scenario.deliveries) == 3
    assert [d.is_reply for d in scenario.deliveries] == [True, True, False]
    assert scenario.deliveries[-1].tag == pipeline_messages.make_tag(
        f"hq-{redeliver.SCENARIO_TAG_SUFFIX[redeliver.SCENARIO_REPLY_FIRST]}", SEED, 0
    )


def test_the_reply_first_scenario_pins_correct_threading_headers():
    """The only thing wrong with the delivery is its order, so the headers
    have to be right or the test would be measuring a different bug."""
    scenario = _by_name(redeliver.SCENARIO_REPLY_FIRST)
    root = scenario.deliveries[-1]
    for reply in scenario.deliveries[:-1]:
        assert reply.email.headers["In-Reply-To"] == root.message_id
        assert root.message_id in reply.email.headers["References"]
        assert reply.message_id != root.message_id


def test_the_references_scenario_builds_a_growing_chain():
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    root, first, second = scenario.deliveries
    assert first.email.headers["In-Reply-To"] == root.message_id
    assert first.email.headers["References"] == root.message_id
    # The second reply answers the first, not the root, and its References
    # carries the whole ancestry. A pipeline that threads only on In-Reply-To
    # gets this right by accident; one that reads only the first Reference does
    # not.
    assert second.email.headers["In-Reply-To"] == first.message_id
    assert second.email.headers["References"] == f"{root.message_id} {first.message_id}"


def test_a_reply_subject_is_never_double_prefixed():
    """`Re: Re: ` is a subject no client has ever sent, and a parser that
    normalises it is right to."""
    assert redeliver.reply_subject("Hello") == "Re: Hello"
    for already in ("Re: Hello", "RE:Hello", "Fwd: Hello", "AW: Hello", "Re[2]: Hello"):
        assert redeliver.reply_subject(already) == already


def test_a_stamped_reply_describes_itself_in_its_own_ground_truth():
    """The bug this catches: `stamp` originally changed `subject` but not
    `ground_truth.subject`, so every check on a reply graded the reply's
    subject against its parent's and the threaded scenarios failed on a
    mismatch that was the test's own fault."""
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    root, first, _second = scenario.deliveries
    assert first.email.subject == first.email.ground_truth.subject
    assert first.email.subject != root.email.ground_truth.subject


# ---------------------------------------------------------------------------
# Scenario isolation
# ---------------------------------------------------------------------------


def test_no_two_scenarios_share_a_tag():
    """Otherwise a readback keyed by tag cannot tell which scenario a record
    came from, and one scenario's verdicts become another's."""
    all_tags = [tag for s in _scenarios() for tag in s.tags]
    assert len(set(all_tags)) == len(all_tags)


def test_no_two_scenarios_share_a_message_id():
    """The failure that made the first version of this suite useless. Shared
    payloads carry shared Message-IDs, a correct pipeline deduplicates on them,
    and the second scenario's deliveries were then absorbed by the first, which
    reported that the system held no record of any of its own messages.

    Compared across scenarios, not within one: the duplicate and slow-retry
    scenarios DELIBERATELY repeat a Message-ID inside themselves, and that
    repeat is the thing under test."""
    ids = [set(d.message_id for d in s.deliveries) for s in _scenarios()]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            assert not ids[i] & ids[j], f"scenarios {i} and {j} share Message-IDs"


def test_the_scenarios_still_share_their_payload_bytes():
    """The other half of the isolation, and deliberate. If one parse bug shows
    up in all four, that is one bug reported four ways rather than four
    unrelated findings, and the run's job is much easier to read.

    Ground truth is the right thing to compare, because it is the part of the
    payload that describes the message rather than the test's bookkeeping: the
    body carries a tag marker and the headers carry a Message-ID, both of which
    differ by scenario on purpose."""
    first = _by_name(redeliver.SCENARIO_DUPLICATE).deliveries[0]
    second = _by_name(redeliver.SCENARIO_SLOW_RETRY).deliveries[0]
    assert first.email.ground_truth == second.email.ground_truth
    assert [a.filename for a in first.email.attachments] == [
        a.filename for a in second.email.attachments
    ]
    # Only the identity differs.
    assert first.message_id != second.message_id
    assert first.tag != second.tag


def test_building_the_scenarios_is_reproducible():
    first = [
        [(d.tag, d.message_id, report.payload_sha256(d.email)) for d in s.deliveries]
        for s in _scenarios()
    ]
    second = [
        [(d.tag, d.message_id, report.payload_sha256(d.email)) for d in s.deliveries]
        for s in _scenarios()
    ]
    assert first == second


def test_an_unknown_scenario_name_is_refused_with_the_known_ones():
    with pytest.raises(redeliver.ScenarioError) as caught:
        _scenarios(["telepathy"])
    assert "duplicate" in str(caught.value)


def test_a_zero_count_is_refused():
    with pytest.raises(redeliver.ScenarioError):
        redeliver.build_scenarios(SEED, 0, "hq", 5.0, None)


# ---------------------------------------------------------------------------
# Judging
# ---------------------------------------------------------------------------


def _readbacks_for(scenario, tickets_per_tag=1, route=None):
    """Every record carrying the same content, for tests that only care about
    how many there are."""
    by_tag = {}
    for delivery in scenario.deliveries:
        for index in range(tickets_per_tag):
            by_tag.setdefault(delivery.tag, []).append(
                Readback(
                    exists=True,
                    ticket_id=f"T{index + 1}",
                    from_addr="alice@example.com",
                    subject="whatever",
                    body="whatever",
                    route=route,
                    fields=ALL_FIELDS,
                )
            )
    return by_tag


def _content_readbacks(scenario, tickets_per_tag=1):
    """Readbacks whose content matches each delivery's own ground truth, so
    only the delivery-semantics checks are under test.

    Every message in a correct run lands on ticket T1, including the replies,
    which is what a pipeline that threads properly does. `tickets_per_tag`
    above one simulates the redelivery that produced a duplicate.
    """
    by_tag = {}
    for delivery in scenario.deliveries:
        if delivery.tag in by_tag:
            continue
        parent = scenario.parent_of(delivery.tag)
        for index in range(tickets_per_tag):
            by_tag.setdefault(delivery.tag, []).append(
                Readback(
                    exists=True,
                    ticket_id="T1",
                    from_addr=delivery.email.ground_truth.from_addr,
                    subject=delivery.email.ground_truth.subject,
                    body=delivery.email.text,
                    attachment_names=tuple(
                        a.filename for a in delivery.email.attachments
                    ),
                    route=delivery.email.envelope.to[0],
                    in_reply_to=parent.message_id if parent else None,
                    references=(parent.message_id,) if parent else (),
                    fields=ALL_FIELDS,
                )
            )
    return by_tag


def test_a_pipeline_that_deduplicates_correctly_passes_every_scenario():
    for scenario in _scenarios():
        result = redeliver.judge_scenario(scenario, _content_readbacks(scenario))
        assert result.passed, f"{scenario.name}: {result.findings}"


def test_a_pipeline_that_duplicates_a_redelivery_is_caught():
    """The headline finding, and the one that is invisible to a status code:
    two tickets for one message, each announced with a 200."""
    for name in (redeliver.SCENARIO_DUPLICATE, redeliver.SCENARIO_SLOW_RETRY):
        scenario = _by_name(name)
        result = redeliver.judge_scenario(
            scenario, _content_readbacks(scenario, tickets_per_tag=2)
        )
        assert result.passed is False
        assert any("redelivery created a duplicate" in f for f in result.findings)


def test_a_pipeline_that_loses_a_message_is_caught():
    scenario = _by_name(redeliver.SCENARIO_DUPLICATE)
    readbacks = _content_readbacks(scenario)
    readbacks[scenario.deliveries[0].tag] = []
    result = redeliver.judge_scenario(scenario, readbacks)
    assert result.passed is False
    assert any("holds no record of it" in f for f in result.findings)


def test_a_pipeline_that_drops_threading_headers_is_caught_by_the_link_check():
    """A pipeline that stores no In-Reply-To reports the field as present and
    null, which is a real finding: it looked, and there was nothing there."""
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    readbacks = _content_readbacks(scenario)
    reply = next(d for d in scenario.deliveries if d.is_reply)
    readbacks[reply.tag] = [
        Readback(
            exists=True, ticket_id="T1", from_addr=reply.email.ground_truth.from_addr,
            subject=reply.email.ground_truth.subject, body=reply.email.text,
            attachment_names=(), route=reply.email.envelope.to[0],
            in_reply_to=None, references=(),
            fields=ALL_FIELDS,
        )
    ]
    result = redeliver.judge_scenario(scenario, readbacks)
    assert result.passed is False
    assert any("In-Reply-To is None" in f for f in result.findings)
    assert any("References [] does not contain" in f for f in result.findings)


def test_an_adapter_that_cannot_see_threading_headers_is_not_blamed_for_it():
    """The other case, and the reason the check consults `fields`. A lookup
    that cannot see the headers says so; it is not the pipeline's fault and
    reporting it as one would be a false positive in every run."""
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    readbacks = _content_readbacks(scenario)
    reply = next(d for d in scenario.deliveries if d.is_reply)
    readbacks[reply.tag] = [
        Readback(
            exists=True, ticket_id="T1", from_addr=reply.email.ground_truth.from_addr,
            subject=reply.email.ground_truth.subject, body=reply.email.text,
            attachment_names=(), route=reply.email.envelope.to[0],
            fields=("from_addr", "subject", "body", "attachment_names", "route",
                    "ticket_id"),
        )
    ]
    result = redeliver.judge_scenario(scenario, readbacks)
    assert result.passed is True, result.findings
    # Scoped to the one record, not the last one seen: the other reply in the
    # thread did expose its headers and does pass, so a whole-run dict would
    # report whichever happened to be checked last.
    record = next(r for r in result.records if r["tag"] == reply.tag)
    statuses = {c["check"]: c["status"] for c in record["checks"]}
    assert statuses["thread_link"] == "skipped"
    assert statuses["thread_together"] == "passed"


def test_a_pipeline_that_files_a_reply_on_its_own_ticket_is_caught_by_together():
    """Correct headers, separate ticket: the outcome that makes an agent open
    a duplicate ticket a fortnight later. The link check passes and only the
    together check fails, which is the whole reason they are separate."""
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    readbacks = _content_readbacks(scenario)
    reply = next(d for d in scenario.deliveries if d.is_reply)
    parent = scenario.parent_of(reply.tag)
    good = readbacks[reply.tag][0]
    readbacks[reply.tag] = [
        Readback(
            exists=True, ticket_id="T-SEPARATE", from_addr=good.from_addr,
            subject=good.subject, body=good.body, attachment_names=(),
            route=good.route, in_reply_to=parent.message_id,
            references=(parent.message_id,), fields=ALL_FIELDS,
        )
    ]
    result = redeliver.judge_scenario(scenario, readbacks)
    assert result.passed is False
    assert any("T-SEPARATE" in f for f in result.findings)
    assert not any("In-Reply-To is" in f for f in result.findings)

    checks = {
        c["check"]: c["status"]
        for r in result.records
        if r["tag"] == reply.tag
        for c in r["checks"]
    }
    assert checks["thread_link"] == "passed"
    assert checks["thread_together"] == "failed"


def test_a_pipeline_that_misparses_the_content_is_caught_too():
    """The scenarios are not only counting. A duplicate count is worthless if
    the tickets it counted were never the customer's message."""
    scenario = _by_name(redeliver.SCENARIO_DUPLICATE)
    readbacks = _content_readbacks(scenario)
    first = readbacks[scenario.deliveries[0].tag]
    readbacks[scenario.deliveries[0].tag] = [
        Readback(exists=True, ticket_id="T1", subject="wrong", fields=("subject",))
    ]
    result = redeliver.judge_scenario(scenario, readbacks)
    assert result.passed is False
    assert any("subject" in f for f in result.findings)


def test_a_delivery_is_graded_against_its_own_ground_truth():
    """A reply's expected subject is the reply's own, not its parent's. Looked
    up by tag rather than by role, because a reply has no root of its own."""
    scenario = _by_name(redeliver.SCENARIO_REPLY_FIRST)
    reply = next(d for d in scenario.deliveries if d.is_reply)
    assert scenario.delivery_for(reply.tag) is reply
    assert scenario.delivery_for(scenario.deliveries[-1].tag).is_reply is False
    assert scenario.delivery_for("no-such-tag") is None


def test_a_scenario_never_delivers_a_reply_to_a_message_it_does_not_send():
    """A build bug in the scenario must be reported as one, not as a pipeline
    failure. Reporting it as a pipeline bug would put a permanent false
    positive in every run."""
    scenario = _by_name(redeliver.SCENARIO_REPLY_FIRST)
    for delivery in scenario.deliveries:
        if delivery.is_reply:
            assert scenario.parent_of(delivery.tag) is not None


# ---------------------------------------------------------------------------
# Strays across the whole run
# ---------------------------------------------------------------------------


def test_a_stray_record_is_reported_against_the_run_not_every_scenario():
    """One stray is one finding. Attaching it to all four scenarios would make
    four bugs out of one and bury the real ones."""
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    strays = [Readback(exists=True, ticket_id="STRAY", tag="not-ours")]

    results = redeliver.judge_all(scenarios, readbacks, strays)
    assert sum(len(r.findings) for r in results) == 1
    assert "STRAY" in results[0].findings[0]
    assert results[0].passed is False
    # The scenarios that were themselves fine stay fine. Blaming all four would
    # make the report useless precisely when it is needed.
    assert [r.passed for r in results] == [False, True, True, True]


def test_an_unsearchable_adapter_marks_every_scenario_rather_than_passing():
    """The check that did not run must not read as a check that passed."""
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    results = redeliver.judge_all(scenarios, readbacks, strays=None)

    assert all(r.strays_searched is False for r in results)
    assert all(r.passed for r in results)
    assert all("strays_searched" in r.to_json() for r in results)


# ---------------------------------------------------------------------------
# The artifact and the report
# ---------------------------------------------------------------------------


def test_the_artifact_records_the_verdict_and_the_questions():
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    results = redeliver.judge_all(scenarios, readbacks, strays=[])
    artifact = redeliver.build_artifact(
        SEED, redeliver.run_config(SEED, COUNT, "hq", "local", _config(), 5.0, None), results
    )

    assert artifact["summary"]["verdict"] == "DELIVERY-SAFE"
    assert artifact["summary"]["scenarios"] == 4
    assert artifact["summary"]["failed"] == 0
    assert artifact["summary"]["deliveries"] == sum(
        r.deliveries for r in results
    )
    for entry in artifact["scenarios"]:
        assert entry["question"], "a scenario with no question is not a report"
        assert entry["unique_messages"] > 0
        assert entry["findings"] == []


def test_the_report_prints_each_question_next_to_its_verdict():
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    results = redeliver.judge_all(scenarios, readbacks, strays=[])
    text = redeliver.format_redelivery(
        redeliver.build_artifact(
            SEED, redeliver.run_config(SEED, COUNT, "hq", "local", _config(), 5.0, None), results
        )
    )
    assert text.splitlines()[0].startswith("redeliver: DELIVERY-SAFE")
    for scenario in scenarios:
        assert scenario.question in text


def test_the_report_names_the_stray_shortcoming_when_there_is_one():
    scenarios = _scenarios(["duplicate"])
    readbacks = _content_readbacks(scenarios[0])
    results = redeliver.judge_all(scenarios, readbacks, strays=None)
    text = redeliver.format_redelivery(
        redeliver.build_artifact(
            SEED, redeliver.run_config(SEED, COUNT, "hq", "local", _config(), 5.0, ["duplicate"]),
            results,
        )
    )
    assert "strays not searched" in text


def test_the_dry_run_prints_the_delivery_schedule():
    scenarios = _scenarios()
    text = redeliver.format_dry_run(scenarios, _config(), "hq")
    assert "no network calls were made" in text
    for scenario in scenarios:
        assert scenario.name in text
    assert "+5s" in text


def _config():
    from testinghq.pipeline.adapters import ReadbackConfig

    return ReadbackConfig(kind="http", url="http://localhost:8000/tickets")


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def test_sending_a_scenario_delivers_every_delivery_in_order():
    """The order IS the scenario for reply-first, so a send that reordered or
    sorted would silently stop testing it."""
    scenario = _by_name(redeliver.SCENARIO_REPLY_FIRST)
    sent = redeliver.send_scenario(
        scenario, "http://localhost:9/intake", rate=1000.0, client=_RecordingClient(),
        sleep=lambda _s: None, clock=lambda: 0.0,
    )
    assert [m.tag for m in sent] == [d.tag for d in scenario.deliveries]
    assert all(m.result.status == 200 for m in sent)


def test_sending_a_scenario_honours_the_delays_through_the_injected_sleep():
    """The slow-retry scenario's gap is the whole point. A `sleep` that is not
    injectable means a test either takes five seconds or is testing a scenario
    with no gap in it, which is the duplicate scenario in a different hat."""
    scenario = _by_name(redeliver.SCENARIO_SLOW_RETRY)
    waited: list = []
    now = [0.0]

    def sleep(seconds):
        waited.append(seconds)
        now[0] += seconds

    redeliver.send_scenario(
        scenario, "http://localhost:9/intake", rate=1000.0, client=_RecordingClient(),
        sleep=sleep, clock=lambda: now[0],
    )
    assert waited == [5.0, 5.0, 5.0]


class _RecordingClient:
    """An `HttpClient` seam that answers 200 without a socket, and records what
    it was given."""

    def __init__(self) -> None:
        self.sent = []

    def send(self, request):
        self.sent.append(request)
        return _Response()

    def close(self) -> None:
        return None


class _Response:
    status = 200
    body = b"ok"


def test_a_scenario_needs_a_delivery_for_every_tag_it_reports():
    scenario = _by_name(redeliver.SCENARIO_DUPLICATE)
    assert len(scenario.tags) == COUNT
    assert scenario.deliveries[0].tag == scenario.tags[0]
    assert scenario.deliveries[-1].tag == scenario.tags[-1]
