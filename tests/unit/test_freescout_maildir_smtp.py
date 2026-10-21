"""The FreeScout stack's delivery sidecar: how a message lands in the Maildir
Dovecot serves, and where everything else goes. No sockets: these call the
delivery code the SMTP handler calls."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "thq_maildir_smtp_unit", REPO_ROOT / "sweep" / "compose" / "freescout" / "maildir_smtp.py"
)
maildir_smtp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maildir_smtp)

MESSAGE = (
    b"From: Alice <alice@hq.example.test>\r\n"
    b"To: support@freescout.test\r\n"
    b"Subject: Printer on fire\r\n"
    b"Message-ID: <hq-1-0000@hq.example.test>\r\n"
    b"\r\n"
    b"The third floor printer is smoking.\r\n"
    b"[testinghq:hq-1-0000]\r\n"
)


def test_a_delivery_lands_whole_in_new_under_a_maildir_name(tmp_path):
    maildir = tmp_path / "support" / "Maildir"
    path = maildir_smtp.maildir_deliver(
        maildir, MESSAGE, envelope_from="alice@hq.example.test",
        recipient="support@freescout.test", clock=lambda: 1700000000.25, hostname="deliver",
    )
    assert path.parent == maildir / "new"
    assert path.name.startswith("1700000000.M250000P")
    assert path.name.endswith(".deliver")
    assert list((maildir / "tmp").iterdir()) == []
    assert (maildir / "cur").is_dir()
    if os.name == "posix":  # Windows keeps no POSIX mode bits to check
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    stored = path.read_bytes()
    assert b"\r\n" not in stored
    assert stored.startswith(
        b"Return-Path: <alice@hq.example.test>\nDelivered-To: support@freescout.test\n"
        b"From: Alice"
    )
    assert stored.endswith(b"[testinghq:hq-1-0000]\n")


def test_two_deliveries_in_the_same_microsecond_get_different_names(tmp_path):
    names = {
        maildir_smtp.maildir_deliver(
            tmp_path, MESSAGE, envelope_from="a@hq.example.test",
            recipient="support@freescout.test", clock=lambda: 1.0, hostname="h",
        ).name
        for _ in range(3)
    }
    assert len(names) == 3


def test_names_within_one_second_sort_in_delivery_order():
    """A helpdesk imports a fetch in the order the mailbox lists it, so the
    names have to sort the way the mail arrived. Unpadded, 5.034706 was
    'M34706', which sorts after 'M250000' from earlier in the same second."""
    early = maildir_smtp.unique_name(clock=lambda: 5.25, hostname="h")
    late = maildir_smtp.unique_name(clock=lambda: 5.034706 + 0.5, hostname="h")
    earlier_still = maildir_smtp.unique_name(clock=lambda: 5.034706, hostname="h")
    assert sorted([late, early, earlier_still]) == [earlier_still, early, late]


def test_a_host_name_is_escaped_the_way_maildir_names_need():
    name = maildir_smtp.unique_name(clock=lambda: 5.0, hostname="a/b:c")
    assert name.endswith(".a\\057b\\072c")


def test_the_maildir_is_handed_to_the_mail_user(tmp_path, monkeypatch):
    owned = {}
    # raising=False: Windows has no os.chown to replace, and the sidecar
    # runs on Linux, so the test supplies one.
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: owned.__setitem__(Path(path), (uid, gid)),
                        raising=False)
    maildir = tmp_path / "support" / "Maildir"
    path = maildir_smtp.maildir_deliver(
        maildir, MESSAGE, envelope_from="a@hq.example.test",
        recipient="support@freescout.test", uid=1000, gid=1000,
    )
    for expected in (maildir.parent, maildir, maildir / "new", maildir / "cur",
                     maildir / "tmp", maildir / "tmp" / path.name):
        assert owned[expected] == (1000, 1000), expected


def test_mail_for_the_mailbox_goes_to_the_maildir_and_the_rest_to_the_sink(tmp_path):
    maildir = tmp_path / "Maildir"
    sink = tmp_path / "outbound.jsonl"
    delivered = []
    deliver = maildir_smtp.Delivery(
        maildir, ["Support@FreeScout.test"], sink=sink, on_delivered=delivered.append,
    )

    code, text = deliver("alice@hq.example.test", ("support@freescout.test",), MESSAGE)
    assert code == 250 and text.startswith("2.0.0 Ok")
    assert len(list((maildir / "new").iterdir())) == 1
    assert delivered == [maildir]
    assert not sink.exists()

    deliver("support@freescout.test", ("alice@hq.example.test",), MESSAGE)
    assert len(list((maildir / "new").iterdir())) == 1
    assert delivered == [maildir]
    [line] = [json.loads(raw) for raw in sink.read_text(encoding="utf-8").splitlines()]
    assert line["route"] == "alice@hq.example.test"
    assert line["subject"] == "Printer on fire"
    assert line["message_id"] == "hq-1-0000@hq.example.test"
    assert "[testinghq:hq-1-0000]" in line["body"]


def test_a_message_for_both_is_delivered_once_and_recorded_once(tmp_path):
    sink = tmp_path / "outbound.jsonl"
    deliver = maildir_smtp.Delivery(tmp_path / "Maildir", ["support@freescout.test"], sink=sink)
    deliver("a@hq.example.test", ("support@freescout.test", "bob@hq.example.test"), MESSAGE)
    assert len(list((tmp_path / "Maildir" / "new").iterdir())) == 1
    assert len(sink.read_text(encoding="utf-8").splitlines()) == 1


def test_a_delivery_with_no_mailbox_address_is_refused():
    with pytest.raises(ValueError):
        maildir_smtp.Delivery(Path("unused"), [" "])
