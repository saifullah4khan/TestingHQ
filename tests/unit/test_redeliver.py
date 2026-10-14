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
from testinghq.pipeline import common as pipeline_common
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


def test_the_slow_retry_scenario_sends_every_original_then_waits_once():
    """One wait for the batch. A wait per message turned the default of three
    messages at 120s into six minutes."""
    scenario = _by_name(redeliver.SCENARIO_SLOW_RETRY)
    assert [d.delay_before for d in scenario.deliveries] == (
        [0.0] * COUNT + [5.0] + [0.0] * (COUNT - 1)
    )
    tags = [d.tag for d in scenario.deliveries]
    assert tags[:COUNT] == tags[COUNT:]
    assert len(set(tags[:COUNT])) == COUNT


def test_the_slow_retry_gap_is_configurable():
    """A pipeline with a long deduplication window is being asked a different
    question, and testing it at five seconds would answer one nobody asked."""
    scenario = redeliver.build_slow_retry(
        redeliver.build_clean_corpus(SEED, 2), SEED, "hq", 42.0
    )
    assert [d.delay_before for d in scenario.deliveries] == [0.0, 0.0, 42.0, 0.0]


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
        assert reply.email.headers["In-Reply-To"] == f"<{root.message_id}>"
        assert reply.email.headers["References"] == f"<{root.message_id}>"
        assert reply.message_id != root.message_id


def test_the_references_scenario_builds_a_growing_chain():
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    root, first, second = scenario.deliveries
    assert first.email.headers["In-Reply-To"] == f"<{root.message_id}>"
    assert first.email.headers["References"] == f"<{root.message_id}>"
    # The second reply answers the first, not the root, and its References
    # carries the whole ancestry. A pipeline that threads only on In-Reply-To
    # gets this right by accident; one that reads only the first Reference does
    # not.
    assert second.email.headers["In-Reply-To"] == f"<{first.message_id}>"
    assert second.email.headers["References"] == (
        f"<{root.message_id}> <{first.message_id}>"
    )


def test_every_message_id_redeliver_writes_is_in_angle_brackets():
    """RFC 5322 msg-id is `<id-left@id-right>`. The Delivery keeps the bare id
    the readback compares against."""
    for scenario in redeliver.build_scenarios(SEED, COUNT, "hq", 5.0):
        for delivery in scenario.deliveries:
            headers = delivery.email.headers
            assert headers["Message-ID"] == f"<{delivery.message_id}>"
            assert "<" not in delivery.message_id
            for name in ("In-Reply-To", "References"):
                if name in headers:
                    for token in headers[name].split():
                        assert token.startswith("<") and token.endswith(">"), (name, token)


def _threaded(delivery, ticket, in_reply_to=None, references=()):
    """A record matching `delivery`'s content, carrying the threading headers
    as a store returned them."""
    return Readback(
        exists=True,
        ticket_id=ticket,
        from_addr=delivery.email.ground_truth.from_addr,
        subject=delivery.email.ground_truth.subject,
        body=delivery.email.text,
        attachment_names=tuple(a.filename for a in delivery.email.attachments),
        route=delivery.email.envelope.to[0],
        in_reply_to=in_reply_to,
        references=references,
        fields=ALL_FIELDS,
    )


@pytest.mark.parametrize("bracketed", [True, False])
def test_the_ancestry_check_matches_stored_ids_with_or_without_brackets(bracketed):
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    root, first, second = scenario.deliveries
    wrap = (lambda mid: f"<{mid}>") if bracketed else (lambda mid: mid)
    stored = {
        root.tag: [_threaded(root, "T1")],
        first.tag: [_threaded(first, "T1", wrap(root.message_id), [wrap(root.message_id)])],
        second.tag: [_threaded(
            second, "T1", wrap(first.message_id),
            f"{wrap(root.message_id)} {wrap(first.message_id)}",
        )],
    }
    result = redeliver.judge_scenario(scenario, stored)
    assert result.findings == []
    assert scenario.ancestry_of(second.tag) == (root.message_id, first.message_id)


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
        ancestry = scenario.ancestry_of(delivery.tag)
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
                    references=ancestry,
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


def test_a_stray_attaches_to_the_scenario_whose_tag_it_carries():
    """One stray is one finding, on the scenario that produced it, not on
    whichever scenario happened to be first."""
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    stray_tag = pipeline_messages.make_tag("hq-ref", SEED, 7)
    strays = [Readback(exists=True, ticket_id="STRAY", tag=stray_tag)]

    results = redeliver.judge_all(scenarios, readbacks, strays)
    assert sum(len(r.findings) for r in results) == 1
    by_name = {r.name: r for r in results}
    references = by_name[redeliver.SCENARIO_REFERENCES]
    assert "STRAY" in references.findings[0]
    assert references.strays == [{"tag": stray_tag, "ticket": "STRAY"}]
    assert [r.passed for r in results] == [True, True, True, False]


def test_untagged_and_other_run_records_are_not_strays():
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    strays = [
        Readback(exists=True, ticket_id="CUSTOMER", tag=None),
        Readback(exists=True, ticket_id="OTHER", tag=pipeline_messages.make_tag("zz-dup", SEED, 0)),
    ]
    results, summary = redeliver.judge_run(scenarios, readbacks, strays)
    assert all(r.passed for r in results)
    assert summary["untagged_count"] == 1
    assert summary["other_runs_count"] == 1
    assert summary["unattributed"] == []


def test_a_stray_with_this_runs_prefix_and_no_scenarios_is_a_run_finding():
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    strays = [Readback(exists=True, ticket_id="ODD", tag=pipeline_messages.make_tag("hq", SEED, 3))]
    results, summary = redeliver.judge_run(scenarios, readbacks, strays)
    assert all(r.passed for r in results)
    assert summary["unattributed"] == [{"tag": "hq-11-0003", "ticket": "ODD"}]
    artifact = redeliver.build_artifact(
        SEED, redeliver.run_config(SEED, COUNT, "hq", "local", _config(), 5.0, None),
        results, None, summary,
    )
    assert artifact["summary"]["verdict"] == "DELIVERY-BUGS"
    assert "ODD" in redeliver.format_redelivery(artifact)


def test_the_references_scenario_checks_the_full_ancestry():
    """The last reply keeps its parent in References and drops the root. Its
    In-Reply-To is right, so only the ancestry check can see it."""
    scenario = _by_name(redeliver.SCENARIO_REFERENCES)
    root, first, second = scenario.deliveries
    assert scenario.ancestry_of(second.tag) == (root.message_id, first.message_id)
    readbacks = _content_readbacks(scenario)
    good = readbacks[second.tag][0]
    readbacks[second.tag] = [Readback(
        exists=True, ticket_id="T1", from_addr=good.from_addr, subject=good.subject,
        body=good.body, attachment_names=(), route=good.route,
        in_reply_to=first.message_id, references=(first.message_id,),
        fields=ALL_FIELDS,
    )]
    result = redeliver.judge_scenario(scenario, readbacks)
    assert result.passed is False
    assert any("full ancestry" in f and root.message_id in f for f in result.findings)


def test_the_subject_match_and_route_reach_the_scenarios():
    scenario = _by_name(redeliver.SCENARIO_DUPLICATE)
    readbacks = _content_readbacks(scenario)
    prefixed = {
        tag: [Readback(
            exists=True, ticket_id=r.ticket_id, from_addr=r.from_addr,
            subject=f"[Ticket #5] {r.subject}", body=r.body,
            attachment_names=r.attachment_names, route="queue:support",
            fields=ALL_FIELDS,
        ) for r in found]
        for tag, found in readbacks.items()
    }
    assert redeliver.judge_scenario(scenario, prefixed).passed
    exact = redeliver.judge_scenario(scenario, prefixed, subject_match="exact")
    assert any("match: exact" in f for f in exact.findings)
    routed = redeliver.judge_scenario(scenario, prefixed, route="queue:billing")
    assert any("routing" in f for f in routed.findings)
    assert redeliver.judge_scenario(scenario, prefixed, route="queue:support").passed


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
    assert waited == [5.0]
    assert now[0] == 5.0


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


# ---------------------------------------------------------------------------
# Strays under a fixed prefix next to per-run prefixes
# ---------------------------------------------------------------------------


def test_another_runs_per_run_prefix_is_not_this_runs_stray():
    """A run with `--tag-prefix hq` next to a store holding another run's
    `hq-<yymmddHHMM>-<4 hex>-dup-...` tickets: those are another run's, not a
    finding on this run's duplicate scenario."""
    scenarios = _scenarios()
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    other = pipeline_messages.make_tag("hq-2610141100-cd34-dup", SEED, 0)
    results, summary = redeliver.judge_run(
        scenarios, readbacks, [Readback(exists=True, ticket_id="OTHER", tag=other)]
    )
    assert all(r.passed for r in results)
    assert summary["unattributed"] == []
    assert summary["other_runs"] == [{"tag": other, "ticket": "OTHER"}]


def test_a_stray_under_a_per_run_prefix_attaches_to_its_scenario():
    run = "hq-2610141200-ab12"
    scenarios = redeliver.build_scenarios(SEED, COUNT, run, 5.0, None)
    readbacks = {tag: rs for s in scenarios for tag, rs in _content_readbacks(s).items()}
    stray = pipeline_messages.make_tag(f"{run}-dup", SEED, 9)
    results, summary = redeliver.judge_run(
        scenarios, readbacks, [Readback(exists=True, ticket_id="S", tag=stray)],
        tag_prefix=run,
    )
    by_name = {r.name: r for r in results}
    assert by_name[redeliver.SCENARIO_DUPLICATE].strays == [{"tag": stray, "ticket": "S"}]
    assert summary["unattributed"] == []


# ---------------------------------------------------------------------------
# The run: Ctrl-C, and what the artifact records about the target
# ---------------------------------------------------------------------------


def _target_config(tmp_path):
    path = tmp_path / "target.toml"
    path.write_text('[targets.local]\nurl = "http://localhost:9/intake"\n', encoding="utf-8")
    return str(path)


def test_ctrl_c_during_the_slow_retry_wait_writes_what_it_has_and_exits_3(
    tmp_path, monkeypatch
):
    """The one long wait in the tool. An interrupt there keeps the scenarios
    already delivered and says the run was aborted, never graded."""
    from testinghq.pipeline.common import EXIT_MISMATCH

    class _Never:
        def fetch(self, probe):
            raise AssertionError("an aborted run must not read back")

    monkeypatch.setattr(pipeline_common, "build_adapter", lambda config, **kw: _Never())

    now = [0.0]

    def sleep(seconds):
        if seconds >= 120:
            raise KeyboardInterrupt
        now[0] += seconds

    lines = []
    out = tmp_path / "redeliver.json"
    code = redeliver.execute(
        SEED, COUNT, "local", _target_config(tmp_path), str(out),
        readback=_config(), scenarios=None, tag_prefix="hq", retry_after=120.0,
        rate=1000.0, client=_RecordingClient(), sleep=sleep,
        clock=lambda: now[0], printer=lines.append,
    )
    import json

    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert code == EXIT_MISMATCH
    assert artifact["aborted"] is True
    assert artifact["tool"] == "redeliver"
    assert artifact["summary"]["aborted_during"] == "send"
    # The duplicate scenario finished before the slow-retry wait began.
    duplicate = _by_name(redeliver.SCENARIO_DUPLICATE)
    assert artifact["summary"]["sent"] == len(duplicate.deliveries)
    assert "ABORTED" in "\n".join(lines)


def test_the_redelivery_artifact_records_the_target_format(tmp_path, monkeypatch):
    class _Empty:
        def fetch(self, probe):
            return []

    monkeypatch.setattr(pipeline_common, "build_adapter", lambda config, **kw: _Empty())
    out = tmp_path / "redeliver.json"
    redeliver.execute(
        SEED, COUNT, "local", _target_config(tmp_path), str(out),
        readback=_config(), scenarios=["duplicate"], tag_prefix="hq",
        rate=1000.0, client=_RecordingClient(), sleep=lambda _s: None,
        clock=lambda: 0.0,
        printer=lambda _line: None, quiet_window=0.0, max_wait=0.0,
    )
    import json

    config = json.loads(out.read_text(encoding="utf-8"))["config"]
    assert config["format"] == "sendgrid"
    assert config["allow_public_target"] is False


def test_every_lookup_a_404_is_readback_failed_and_no_scenario_reports_a_loss():
    from testinghq.core.exit_codes import EXIT_FINDING
    from testinghq.pipeline.common import ReadbackOutcome
    from testinghq.reporting import summarize

    scenarios = _scenarios()
    tags = [tag for s in scenarios for tag in s.tags]
    outcome = ReadbackOutcome(
        readbacks={tag: [] for tag in tags}, elapsed=1.0, polls=2, stable=True,
        missing=tuple(tags), all_404=True,
    )
    results, strays = redeliver.judge_run(
        scenarios, outcome.readbacks, None, readback_failed=True
    )
    artifact = redeliver.build_artifact(
        SEED, redeliver.run_config(SEED, COUNT, "hq", "local", _config(), 5.0, None),
        results, outcome, strays,
    )
    summary = artifact["summary"]
    assert summary["verdict"] == "READBACK FAILED"
    assert summary["verdict_source"] == "readback"
    assert summary["failed"] == 0 and summary["passed"] == 0
    assert summary["not_checked"] == len(scenarios)
    for entry in artifact["scenarios"]:
        assert entry["findings"] == [] and entry["missing"] == []
        assert entry["not_checked"]
    text = redeliver.format_redelivery(artifact)
    assert text.splitlines()[0].startswith("redeliver: READBACK FAILED")
    assert "every lookup was 404: check the readback url" in text
    assert "holds no record" not in text
    assert "[FAIL]" not in text

    read = summarize(artifact)
    assert read.exit_code == EXIT_FINDING
    assert [f.kind for f in read.findings] == ["readback-failed"]
