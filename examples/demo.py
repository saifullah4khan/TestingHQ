"""Demo: generate a small clean corpus and dry-run it end to end.

Run with:

    python examples/demo.py

This never touches the network. It builds a deterministic clean corpus with
testinghq.blast.generate, serializes each payload the way transport.post
would, and (by default) stops there in dry-run mode, per the guardrail
decision from testinghq.core.guardrails.evaluate_send. Pass send=True to
run_demo to also "fire" the corpus through an in-process fake HTTP client
that only records what it received; that client never opens a socket, so
this script is safe to run with no configuration at all.

For a real target, see examples/target.example.toml and examples/README.md
(owned by another lane) and use `testinghq blast fire` instead.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import List

from testinghq.blast.generate import generate_corpus
from testinghq.core.guardrails import evaluate_send
from testinghq.core.transport import ClientResponse, PreparedRequest, post


@dataclass
class RecordingFakeClient:
    """An in-process fake HTTP client: records every request it receives and
    always returns a canned 200 OK. No sockets, no network. Only used by this
    demo script to make "fire" mode safe to run with zero configuration."""

    received: List[PreparedRequest] = field(default_factory=list)

    def send(self, request: PreparedRequest) -> ClientResponse:
        self.received.append(request)
        return ClientResponse(status=200, body=b'{"status":"ok"}')


def run_demo(seed: int = 1, count: int = 5, send: bool = False) -> RecordingFakeClient:
    """Generate `count` clean payloads from `seed` and either print a
    dry-run summary or fire them at an in-process fake sink. Returns the
    fake client so callers (or tests) can inspect what it recorded."""
    decision = evaluate_send(send)
    print(f"blast demo: {decision.reason}")

    corpus = generate_corpus(seed=seed, count=count)
    client = RecordingFakeClient()

    for index, email in enumerate(corpus, start=1):
        if decision.will_send:
            result = post(email, "https://sink.example.test/inbound", client)
            print(
                f"[{index}/{count}] subject={email.subject!r} "
                f"status={result.status} latency_ms={result.latency_ms:.2f}"
            )
        else:
            print(f"[{index}/{count}] dry-run subject={email.subject!r} to={email.to!r}")

    if decision.will_send:
        print(f"fired {len(client.received)} payload(s) at the in-process fake sink")
    else:
        print("dry-run only: no network calls were made (pass --send to fire at the fake sink)")

    return client


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument(
        "--send",
        action="store_true",
        help="fire at the in-process fake sink instead of a dry run",
    )
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    run_demo(seed=args.seed, count=args.count, send=args.send)


if __name__ == "__main__":
    main()
