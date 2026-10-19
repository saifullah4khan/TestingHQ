"""`blast generate|fire|replay` with `--corpus`, and `report` on what they write.

Driven through `testinghq.cli.main` with the transport's client replaced by a
recorder, so the real send path, guardrails and artifact builder run and no
socket opens.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from testinghq import cli
from testinghq.cli import blast as blast_cli
from testinghq.core.exit_codes import EXIT_DRY_RUN, EXIT_FINDING, EXIT_OK, EXIT_REFUSED
from testinghq.core.transport import ClientResponse
from testinghq.reporting import JSON_KEYS

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"

SOURCES = [
    "005-raw-utf8-headers.box",
    "007-forwarded.box",
    "inbox/001-plain-ascii.eml",
    "inbox/002-alternative-cc-reply-to.eml",
    "inbox/003-attachments.eml",
    "inbox/004-encoded-headers.box",
    "inbox/006-reply-thread.eml",
    "inbox/010-delivery-status-bounce.box",
    "nested/008-html-only.eml",
    "nested/009-degenerate.box",
]
UNPARSEABLE = "nested/009-degenerate.box"

REAL_DOMAINS = (
    "lindenbrook-freight.com", "orchardlane-supplies.com", "kestrelpoint.co.uk",
    "bluewhistle-studio.io", "mapleridge-insurance.com", "vandermeer-hydraulik.de",
    "petitpont-atelier.fr", "harbourview-clinic.ca", "tidewater-logistics.org",
    "sunvale-telecom.net",
)


class Recorder:
    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return ClientResponse(status=self.status, body=b'{"ok":true}')


@pytest.fixture
def recorder(monkeypatch):
    fake = Recorder()
    real_send = blast_cli.send
    monkeypatch.setattr(
        blast_cli, "send",
        lambda email, target, client=None, **kw: real_send(email, target, fake, **kw),
    )
    return fake


def _config(tmp_path, fmt=None):
    path = tmp_path / "target.toml"
    extra = f'format = "{fmt}"\n' if fmt else ""
    path.write_text(
        f'[targets.local]\nurl = "http://127.0.0.1:8000/inbound"\n{extra}', encoding="utf-8"
    )
    return str(path)


def _fire(tmp_path, *extra, corpus=CORPUS, fmt=None):
    out = tmp_path / "blast.json"
    code = cli.main([
        "blast", "fire", "--send", "--target", "local", "--config", _config(tmp_path, fmt),
        "--corpus", str(corpus), "--rate", "1000", "--tag-prefix", "hq-t", "--out", str(out),
        *extra,
    ])
    artifact = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return code, artifact


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["generate", "fire"])
@pytest.mark.parametrize("other", [["--mix", "clean=100"], ["--catalog"], ["--catalog", "empty_body"]])
def test_corpus_and_mix_or_catalog_are_a_usage_error(command, other, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["blast", command, "--corpus", str(CORPUS), *other])
    assert exc.value.code == EXIT_REFUSED
    assert "not allowed with argument" in capsys.readouterr().err


def test_corpus_with_attachments_is_refused(capsys):
    code = cli.main(["blast", "fire", "--corpus", str(CORPUS), "--attachments", "none"])
    assert code == EXIT_REFUSED
    assert "--attachments" in capsys.readouterr().err


def test_tag_prefix_without_corpus_is_refused(capsys):
    assert cli.main(["blast", "fire", "--tag-prefix", "x"]) == EXIT_REFUSED
    assert "--corpus only" in capsys.readouterr().err


def test_a_corpus_that_is_not_a_directory_is_refused(tmp_path, capsys):
    code = cli.main(["blast", "fire", "--corpus", str(tmp_path / "nope")])
    assert code == EXIT_REFUSED
    assert "not a directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# fire --send
# ---------------------------------------------------------------------------


def test_fire_sends_every_parsed_file_and_records_every_file(tmp_path, recorder, capsys):
    code, artifact = _fire(tmp_path)
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err

    records = artifact["records"]
    assert [r["source"] for r in records] == SOURCES
    assert [r["id"] for r in records] == [f"corpus-0-{i:04d}" for i in range(len(SOURCES))]
    assert all(r["category"] == "corpus" for r in records)
    assert len(recorder.requests) == len(SOURCES) - 1

    (bad,) = [r for r in records if r.get("outcome") == "unparseable"]
    assert bad["source"] == UNPARSEABLE
    assert "no header fields" in bad["error"]

    config = artifact["config"]
    assert config["corpus_dir"] == str(CORPUS)
    assert config["corpus_ignored"] == 1
    assert config["tag_prefix"] == "hq-t"
    assert config["count"] == len(SOURCES)

    summary = artifact["summary"]
    assert summary["unparseable"] == [{"source": UNPARSEABLE, "error": bad["error"]}]
    assert summary["by_status_class"]["2xx"] == len(SOURCES) - 1
    assert summary["by_status_class"]["no_response"] == 0
    assert summary["by_category"]["corpus"] == len(SOURCES)
    assert summary["flags"] == []

    assert f"Corpus {CORPUS}:" in captured.out
    assert f"    - {UNPARSEABLE}: " in captured.out
    assert "  ignored (not .eml or .box): 1" in captured.out


@pytest.mark.parametrize("fmt", ["sendgrid", "sendgrid-raw", "mailgun", "postmark", "mime"])
def test_no_real_address_from_the_corpus_is_in_the_sent_bytes(tmp_path, recorder, capsys, fmt):
    code, _artifact = _fire(tmp_path, fmt=fmt)
    assert code == EXIT_OK, capsys.readouterr().err
    assert recorder.requests
    for request in recorder.requests:
        body = request.body.decode("latin-1").lower()
        for domain in REAL_DOMAINS:
            assert "@" + domain not in body, (fmt, domain)
        assert "@corpus.example.test" in body


def test_a_real_address_on_the_target_side_is_never_sent(tmp_path, recorder, capsys):
    """Mail aimed straight at a public mailbox: the rewrite takes it, and
    what goes out is on the synthetic domain."""
    corpus = tmp_path / "mail"
    corpus.mkdir()
    (corpus / "one.eml").write_bytes(
        b"From: Someone <someone@gmail.com>\nTo: help@bigcorp-helpdesk.com\n"
        b"Cc: boss@bigcorp-helpdesk.com\nReply-To: someone.else@gmail.com\n"
        b"Subject: hi someone@gmail.com\n\nreach me at someone@gmail.com\n"
    )
    code, artifact = _fire(tmp_path, corpus=corpus)
    assert code == EXIT_OK, capsys.readouterr().err
    (request,) = recorder.requests
    assert b"gmail.com" not in request.body
    assert b"bigcorp-helpdesk.com" not in request.body
    assert artifact["records"][0]["intended"]["from"].endswith("@corpus.example.test")


def test_count_caps_the_files_sent(tmp_path, recorder, capsys):
    code, artifact = _fire(tmp_path, "--count", "2")
    assert code == EXIT_OK, capsys.readouterr().err
    assert [r["source"] for r in artifact["records"]] == SOURCES[:2]
    assert len(recorder.requests) == 2


def test_a_5xx_on_a_corpus_file_is_flagged(tmp_path, recorder, capsys):
    recorder.status = 500
    code, artifact = _fire(tmp_path, "--count", "1")
    assert code == EXIT_FINDING
    assert artifact["summary"]["flags"] == ["corpus corpus-0-0000 returned 500"]


def test_a_corpus_of_only_unparseable_files_sends_nothing_and_says_so(tmp_path, recorder, capsys):
    corpus = tmp_path / "mail"
    corpus.mkdir()
    shutil.copy(CORPUS / UNPARSEABLE, corpus / "bad.box")
    code, artifact = _fire(tmp_path, corpus=corpus)
    assert code == EXIT_OK, capsys.readouterr().err
    assert recorder.requests == []
    assert artifact["records"][0]["outcome"] == "unparseable"
    assert artifact["summary"]["by_status_class"]["no_response"] == 0


# ---------------------------------------------------------------------------
# Dry run, generate
# ---------------------------------------------------------------------------


def test_a_dry_run_lists_the_corpus_and_writes_its_plan(tmp_path, capsys):
    out = tmp_path / "dry.json"
    code = cli.main(["blast", "fire", "--corpus", str(CORPUS), "--out", str(out)])
    captured = capsys.readouterr().out
    assert code == EXIT_DRY_RUN
    assert f"dry-run preview: {len(SOURCES)} file(s) from {CORPUS}" in captured
    assert "  parsed: 9" in captured and "  unparseable: 1" in captured
    artifact = json.loads(out.read_text(encoding="utf-8"))
    assert [r["source"] for r in artifact["records"]] == SOURCES
    assert artifact["config"]["dry_run"] is True
    assert artifact["summary"]["unparseable"][0]["source"] == UNPARSEABLE


def test_generate_writes_one_file_per_parsed_message_and_lists_the_rest(tmp_path, capsys):
    out = tmp_path / "gen"
    code = cli.main([
        "blast", "generate", "--corpus", str(CORPUS), "--tag-prefix", "hq-g", "--out", str(out),
    ])
    assert code == EXIT_OK
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["corpus_dir"] == str(CORPUS)
    assert [item["source"] for item in manifest["items"]] == SOURCES
    bad = manifest["items"][-1]
    assert bad["outcome"] == "unparseable" and "payload_sha256" not in bad
    written = sorted(p.name for p in out.iterdir() if p.name != "manifest.json")
    assert len(written) == len(SOURCES) - 1
    assert f"{UNPARSEABLE}: " in capsys.readouterr().out


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _copy_corpus(tmp_path):
    corpus = tmp_path / "copy"
    shutil.copytree(CORPUS, corpus)
    return corpus


def test_replay_resends_an_untouched_corpus_run(tmp_path, recorder, capsys):
    corpus = _copy_corpus(tmp_path)
    code, _artifact = _fire(tmp_path, corpus=corpus)
    assert code == EXIT_OK
    first = [r.body for r in recorder.requests]
    recorder.requests.clear()
    code = cli.main([
        "blast", "replay", str(tmp_path / "blast.json"), "--send",
        "--config", _config(tmp_path), "--rate", "1000",
        "--out", str(tmp_path / "again.json"),
    ])
    assert code == EXIT_OK, capsys.readouterr().err
    assert [r.body for r in recorder.requests] == first


@pytest.mark.parametrize("change", ["edit", "remove"])
def test_replay_refuses_when_a_corpus_file_changed(tmp_path, recorder, capsys, change):
    corpus = _copy_corpus(tmp_path)
    _fire(tmp_path, corpus=corpus)
    target = corpus / "inbox" / "001-plain-ascii.eml"
    if change == "edit":
        target.write_bytes(target.read_bytes().replace(b"11 pallets", b"10 pallets"))
    else:
        target.unlink()
    capsys.readouterr()
    code = cli.main(["blast", "replay", str(tmp_path / "blast.json")])
    assert code == EXIT_REFUSED
    assert "no longer build the payloads" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_shows_the_corpus_and_keeps_the_json_keys(tmp_path, recorder, capsys):
    code, _artifact = _fire(tmp_path)
    assert code == EXIT_OK
    path = str(tmp_path / "blast.json")
    capsys.readouterr()

    page = tmp_path / "report.html"
    assert cli.main(["report", path, "--json", "--html", str(page)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert tuple(sorted(payload)) == tuple(sorted(JSON_KEYS))
    assert payload["verdict"] == "CLEAN"
    assert payload["counts"]["sent"] == len(SOURCES) - 1
    assert payload["counts"]["passed"] == len(SOURCES) - 1
    assert payload["counts"]["failed"] == 0
    assert payload["counts"]["records"] == len(SOURCES)
    kinds = [(f["kind"], f["detail"]) for f in payload["findings"]]
    assert kinds[0][0] == "unparseable" and kinds[0][1].startswith(f"{UNPARSEABLE}: ")
    assert kinds[1][0] == "ignored" and kinds[1][1].startswith("1 file(s) under ")

    html = page.read_text(encoding="utf-8")
    assert UNPARSEABLE in html and "inbox/001-plain-ascii.eml" in html
    assert "corpus_dir" in html

    assert cli.main(["report", path]) == EXIT_OK
    text = capsys.readouterr().out
    assert f"corpus:  {CORPUS}" in text
    assert "[unparseable] nested/009-degenerate.box: no header fields" in text


def test_report_of_a_corpus_dry_run_lists_the_unparseable_file(tmp_path, capsys):
    out = tmp_path / "dry.json"
    assert cli.main(["blast", "fire", "--corpus", str(CORPUS), "--out", str(out)]) == EXIT_DRY_RUN
    capsys.readouterr()
    assert cli.main(["report", str(out), "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "DRY RUN"
    assert [f["kind"] for f in payload["findings"]] == ["unparseable", "ignored"]
