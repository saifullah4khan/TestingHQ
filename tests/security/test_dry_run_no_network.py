"""Prove dry-run makes zero network calls.

README's contract: "dry run by default: shows what it would send, makes no
network calls." This test builds a minimal fire loop the same shape the
real fire command uses (check the guardrail, only then touch the network),
and proves no network call happens when send_flag is False, at two layers:
a fake network client (call counting) and a real-socket backstop
(forbid_network, from conftest.py).
"""
from testinghq.core import guardrails


class FakeNetworkClient:
    """Stands in for the real transport. Never touches a socket itself, so
    tests can assert on call counts without any I/O."""

    def __init__(self):
        self.calls = 0

    def send(self, *args, **kwargs):
        self.calls += 1


def _fire(send_flag, network_client):
    """Stand-in for the real fire path: evaluate the guardrail first, only
    call the network client if the decision says to send."""
    decision = guardrails.evaluate_send(send_flag)
    if decision.will_send:
        network_client.send()
    return decision


def test_dry_run_default_never_touches_the_network(forbid_network):
    client = FakeNetworkClient()
    decision = _fire(send_flag=False, network_client=client)
    assert decision.will_send is False
    assert client.calls == 0


def test_dry_run_reason_names_the_default(forbid_network):
    decision = guardrails.evaluate_send(send_flag=False)
    assert "dry-run" in decision.reason


def test_explicit_send_is_the_only_path_that_calls_the_network(forbid_network):
    client = FakeNetworkClient()
    decision = _fire(send_flag=True, network_client=client)
    assert decision.will_send is True
    assert client.calls == 1
