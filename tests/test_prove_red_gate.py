"""Deliberately failing test.

This exists only to prove the CI gate can actually report red before we trust a
green check. On HandleHQ the required check was structurally incapable of failing
for three weeks; we do not repeat that here. Once the 'tests' check is confirmed
red on this PR, close the PR without merging and delete the branch.
"""


def test_gate_can_go_red():
    assert False, "intentional failure to verify CI reports red"
