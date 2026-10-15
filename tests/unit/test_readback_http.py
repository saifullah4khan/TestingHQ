"""The http readback adapter against a fake client: retries, pacing, query and
body templating, pagination, preflight, and error legibility.

Every test injects a fake client and a virtual clock, so nothing opens a socket
and a five-second backoff costs nothing."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from testinghq.core.transport import ClientResponse
from testinghq.pipeline import adapters
from testinghq.pipeline.adapters import AdapterError, BudgetExhausted, HttpJsonAdapter
from testinghq.pipeline.messages import Probe


def _probe(tag: str = "hq-1-0000") -> Probe:
    return Probe(
        record_id="clean-1-0000",
        tag=tag,
        payload_sha256="a" * 64,
        from_addr="alice@example.com",
        subject="Your order",
        recipient="support@example.com",
        message_id="m@widgets.example",
    )


def _json(payload, status: int = 200, headers=None) -> ClientResponse:
    return ClientResponse(
        status=status, body=json.dumps(payload).encode("utf-8"), headers=dict(headers or {})
    )


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Client:
    """Answers from a queue; an exception in the queue is raised. Records the
    virtual time of each request so pacing can be asserted."""

    def __init__(self, *answers, clock=None):
        self.answers = list(answers)
        self.requests = []
        self.times = []
        self.clock = clock

    def send(self, request):
        self.requests.append(request)
        if self.clock is not None:
            self.times.append(self.clock())
        if not self.answers:
            raise AssertionError("more requests than answers")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _adapter(client, clock=None, **kwargs) -> HttpJsonAdapter:
    clock = clock or _Clock()
    kwargs.setdefault("rate", None)
    return HttpJsonAdapter(
        kwargs.pop("url", "http://localhost:8000/tickets"),
        client=client,
        sleep=clock.sleep,
        clock=clock,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_a_429_is_retried_and_retry_after_is_honoured():
    clock = _Clock()
    client = _Client(
        _json({"error": "slow down"}, status=429, headers={"Retry-After": "7"}),
        _json([{"id": "T1"}]),
    )
    adapter = _adapter(client, clock)
    found = adapter.fetch(_probe())
    assert [r.ticket_id for r in found] == ["T1"]
    assert len(client.requests) == 2
    assert clock.slept and clock.slept[0] >= 7.0, clock.slept
    assert adapter.retries_made == 1


def test_retry_after_as_an_http_date_is_read():
    from email.utils import formatdate

    response = _json({}, status=429, headers={"retry-after": formatdate(1_000_030, usegmt=True)})
    assert adapters._retry_after_seconds(response, now=lambda: 1_000_000) == pytest.approx(30)


def test_a_5xx_backs_off_exponentially_with_jitter():
    clock = _Clock()
    client = _Client(
        _json({}, status=503), _json({}, status=502), _json({}, status=500), _json([])
    )
    adapter = _adapter(client, clock, retry_base_seconds=1.0)
    assert adapter.fetch(_probe()) == []
    first, second, third = clock.slept
    assert 0.5 <= first <= 1.0
    assert 1.0 <= second <= 2.0
    assert 2.0 <= third <= 4.0


def test_a_connection_error_is_retried_then_becomes_an_adapter_error():
    """Never a raw URLError or socket timeout: the tools catch AdapterError."""
    import urllib.error

    client = _Client(
        urllib.error.URLError("connection refused"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    )
    adapter = _adapter(client, retry_max=2)
    with pytest.raises(AdapterError) as caught:
        adapter.fetch(_probe())
    assert "3 attempt(s)" in str(caught.value)
    assert "ConnectionResetError" in str(caught.value)


def test_a_4xx_other_than_429_is_not_retried():
    client = _Client(_json({"error": "bad query"}, status=400))
    adapter = _adapter(client)
    with pytest.raises(AdapterError) as caught:
        adapter.fetch(_probe())
    assert "400" in str(caught.value)
    assert len(client.requests) == 1


def test_a_retry_after_beyond_the_ceiling_is_a_failure_not_a_long_sleep():
    clock = _Clock()
    client = _Client(_json({}, status=429, headers={"Retry-After": "86400"}))
    with pytest.raises(AdapterError) as caught:
        _adapter(client, clock).fetch(_probe())
    assert "Retry-After" in str(caught.value)
    assert clock.slept == []


def test_a_retry_that_would_outlast_the_budget_raises_budget_exhausted():
    clock = _Clock()
    client = _Client(_json({}, status=429, headers={"Retry-After": "30"}))
    adapter = _adapter(client, clock)
    adapter.bind_clock(clock, clock.sleep, deadline=10.0)
    with pytest.raises(BudgetExhausted):
        adapter.fetch(_probe())
    assert clock.now < 10.0


def test_the_request_timeout_is_capped_by_the_remaining_budget():
    clock = _Clock()
    client = _Client(_json([]))
    adapter = _adapter(client, clock, timeout=20.0)
    adapter.bind_clock(clock, clock.sleep, deadline=3.0)
    adapter.fetch(_probe())
    assert client.requests[0].timeout == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


def test_requests_are_paced_to_the_readback_rate():
    clock = _Clock()
    client = _Client(*[_json([]) for _ in range(5)], clock=clock)
    adapter = _adapter(client, clock, rate=4.0)
    for index in range(5):
        adapter.fetch(_probe(f"hq-1-{index:04d}"))
    gaps = [b - a for a, b in zip(client.times, client.times[1:])]
    assert all(gap >= 0.25 - 1e-9 for gap in gaps), gaps


def test_the_rate_is_a_config_value_with_a_default_of_four():
    config = adapters.parse_readback_config(
        {"kind": "http", "url": "http://localhost:8000/t"}
    )
    assert config.rate == adapters.DEFAULT_READBACK_RATE == 4.0
    built = adapters.build_adapter(config, client=_Client())
    assert built.rate == 4.0


# ---------------------------------------------------------------------------
# Query and body templates
# ---------------------------------------------------------------------------


def test_a_query_template_fills_the_tag_into_the_configured_parameter():
    client = _Client(_json({"results": []}))
    adapter = _adapter(
        client, url="https://support.example.test/api/v2/search.json",
        tag_param="query", query="type:ticket tags:{tag}", items_key="results",
    )
    adapter.fetch(_probe("hq-5-0001"))
    params = parse_qs(urlsplit(client.requests[0].url).query)
    assert params == {"query": ["type:ticket tags:hq-5-0001"]}


def test_a_post_search_templates_the_body():
    client = _Client(_json({"results": [{"id": "C1"}]}))
    body = '{"filterGroups": [{"filters": [{"propertyName": "hq_tag", "value": "{tag}"}]}]}'
    adapter = _adapter(
        client, url="http://localhost:8000/crm/v3/objects/tickets/search",
        method="POST", body=body, items_key="results",
    )
    found = adapter.fetch(_probe("hq-5-0002"))
    request = client.requests[0]
    assert request.method == "POST"
    assert json.loads(request.body)["filterGroups"][0]["filters"][0]["value"] == "hq-5-0002"
    assert request.headers["Content-Type"] == "application/json"
    assert "hq-5-0002" not in request.url
    assert [r.ticket_id for r in found] == ["C1"]


def test_a_query_template_without_the_placeholder_is_refused():
    with pytest.raises(AdapterError) as caught:
        adapters.parse_readback_config(
            {"kind": "http", "url": "http://x.example.test", "query": "type:ticket"}
        )
    assert "{tag}" in str(caught.value)


def test_a_body_needs_post():
    with pytest.raises(AdapterError):
        adapters.parse_readback_config(
            {"kind": "http", "url": "http://x.example.test", "body": '{"q": "{tag}"}'}
        )


def test_an_unknown_method_is_refused():
    with pytest.raises(AdapterError):
        adapters.parse_readback_config(
            {"kind": "http", "url": "http://x.example.test", "method": "DELETE"}
        )


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_next_path_pagination_follows_every_page():
    client = _Client(
        _json({"results": [{"id": "A"}], "next_page": "http://localhost:8000/tickets?page=2"}),
        _json({"results": [{"id": "B"}], "next_page": None}),
    )
    adapter = _adapter(client, items_key="results", next_path="/next_page")
    listed = adapter.list_all()
    assert [r.ticket_id for r in listed] == ["A", "B"]
    assert listed.partial is False


def test_pagination_stopped_by_max_pages_is_marked_partial():
    """A partial enumeration reported as complete is how strays go missing,
    so the result says it was cut short."""
    pages = [
        _json({"results": [{"id": f"R{n}"}], "next_page": f"/tickets?page={n + 1}"})
        for n in range(1, 6)
    ]
    client = _Client(*pages)
    adapter = _adapter(client, items_key="results", next_path="/next_page", max_pages=3)
    listed = adapter.list_all()
    assert [r.ticket_id for r in listed] == ["R1", "R2", "R3"]
    assert listed.partial is True
    assert len(client.requests) == 3


def test_page_number_pagination_stops_at_a_short_page():
    full = [{"id": f"T{n}"} for n in range(adapters.DEFAULT_PAGE_SIZE)]
    client = _Client(_json(full), _json([{"id": "LAST"}]))
    adapter = _adapter(client, page_param="page", page_size_param="per_page")
    listed = adapter.list_all()
    assert len(listed) == adapters.DEFAULT_PAGE_SIZE + 1
    assert listed.partial is False
    first = parse_qs(urlsplit(client.requests[0].url).query)
    second = parse_qs(urlsplit(client.requests[1].url).query)
    assert first == {"page": ["1"], "per_page": [str(adapters.DEFAULT_PAGE_SIZE)]}
    assert second["page"] == ["2"]


def test_page_number_pagination_marks_a_truncated_lookup_partial():
    full = [{"id": f"T{n}"} for n in range(adapters.DEFAULT_PAGE_SIZE)]
    client = _Client(_json(full), _json(full))
    adapter = _adapter(client, page_param="page", page_size_param="per_page", max_pages=2)
    found = adapter.fetch(_probe())
    assert found.partial is True


def test_a_next_page_on_another_host_is_refused():
    """Following it would send the configured credentials to a host nobody
    configured."""
    client = _Client(
        _json({"results": [{"id": "A"}], "next_page": "http://elsewhere.example.test/p2"})
    )
    adapter = _adapter(client, items_key="results", next_path="next_page")
    with pytest.raises(AdapterError) as caught:
        adapter.list_all()
    assert "different host" in str(caught.value)


def test_next_path_and_page_param_together_are_refused():
    with pytest.raises(AdapterError):
        adapters.parse_readback_config(
            {"kind": "http", "url": "http://x.example.test", "next_path": "/n", "page_param": "p"}
        )


# ---------------------------------------------------------------------------
# The list url
# ---------------------------------------------------------------------------


def test_list_all_removes_the_tag_parameter_and_keeps_the_rest():
    """The old split on `tag=` dropped every parameter after it."""
    client = _Client(_json([]))
    adapter = _adapter(
        client, url="http://localhost:8000/tickets?tag=x&tenant=acme&sort=desc"
    )
    adapter.list_all()
    params = parse_qs(urlsplit(client.requests[0].url).query)
    assert params == {"tenant": ["acme"], "sort": ["desc"]}


def test_a_relative_list_path_resolves_against_the_url():
    client = _Client(_json([]))
    adapter = _adapter(client, url="http://localhost:8000/api/tickets", list_path="all")
    adapter.list_all()
    assert client.requests[0].url == "http://localhost:8000/api/all"


def test_a_public_list_path_is_gated_like_the_url():
    from testinghq.core import guardrails

    with pytest.raises(guardrails.GuardrailError):
        _adapter(_Client(), list_path="https://tickets.acmecorp.com/all")


# ---------------------------------------------------------------------------
# Error legibility
# ---------------------------------------------------------------------------


def test_a_wrapper_under_a_different_key_names_the_key():
    """Previously read as one record and refused for having no identifier."""
    client = _Client(_json({"results": [{"id": "T1"}], "count": 1}))
    adapter = _adapter(client)
    with pytest.raises(AdapterError) as caught:
        adapter.fetch(_probe())
    message = str(caught.value)
    assert "'items'" in message
    assert "items_key = 'results'" in message


def test_a_dotted_items_key_reaches_a_nested_list():
    client = _Client(_json({"data": {"payload": [{"id": 7}]}}))
    adapter = _adapter(client, items_key="data.payload")
    assert [r.ticket_id for r in adapter.fetch(_probe())] == ["7"]


def test_an_empty_body_is_no_records():
    client = _Client(ClientResponse(status=200, body=b""))
    assert _adapter(client).fetch(_probe()) == []


def test_a_404_lookup_records_its_status():
    client = _Client(_json({"error": "no"}, status=404))
    found = _adapter(client).fetch(_probe())
    assert found == [] and found.status == 404


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_preflight_refuses_rejected_credentials(status):
    client = _Client(_json({"error": "auth"}, status=status))
    with pytest.raises(AdapterError) as caught:
        _adapter(client).preflight()
    assert str(status) in str(caught.value)
    assert "credentials" in str(caught.value)


def test_preflight_refuses_a_404_list_path():
    client = _Client(_json([]), _json({}, status=404))
    with pytest.raises(AdapterError) as caught:
        _adapter(client).preflight()
    assert "404" in str(caught.value)
    assert "list" in str(caught.value)


def test_preflight_tolerates_a_404_lookup_when_listing_is_off():
    client = _Client(_json({}, status=404))
    _adapter(client, enumerate_records=False).preflight()
    assert len(client.requests) == 1


def test_preflight_refuses_a_response_that_is_not_json():
    client = _Client(ClientResponse(status=200, body=b"<html>login</html>"))
    with pytest.raises(AdapterError) as caught:
        _adapter(client, enumerate_records=False).preflight()
    assert "valid JSON" in str(caught.value)


def test_preflight_refuses_a_field_map_that_finds_no_identifier():
    client = _Client(_json([{"ticket": {"number": 1}}]))
    with pytest.raises(AdapterError) as caught:
        _adapter(client, enumerate_records=False).preflight()
    assert "identifier" in str(caught.value)


def test_preflight_passes_a_working_endpoint():
    client = _Client(_json([]), _json([{"id": "T1"}]))
    _adapter(client).preflight()
    assert [r.method for r in client.requests] == ["GET", "GET"]
    assert adapters.PREFLIGHT_TAG in client.requests[0].url


# ---------------------------------------------------------------------------
# The default client hands Retry-After to the adapter
# ---------------------------------------------------------------------------


def test_the_urllib_client_keeps_the_response_headers(monkeypatch):
    """Retry-After lives in a header. A client that dropped headers would
    make honouring it impossible on the default path."""
    import io
    import urllib.error
    from email.message import Message

    from testinghq.core import transport

    headers = Message()
    headers["Retry-After"] = "3"

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", headers, io.BytesIO(b"{}")
        )

    monkeypatch.setattr(transport.urllib.request, "urlopen", refuse)
    response = transport.UrllibHttpClient().send(
        transport.PreparedRequest(
            url="http://127.0.0.1:9/t", method="GET", headers={}, body=b"", timeout=1.0
        )
    )
    assert response.status == 429
    assert adapters._retry_after_seconds(response) == 3.0
