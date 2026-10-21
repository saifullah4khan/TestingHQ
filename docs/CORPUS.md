# Real mail as a Blast source

`blast generate` and `blast fire` normally build a seeded corpus. With
`--corpus DIR` they read real mail instead: a helpdesk's own test messages, an
export of a support inbox, anything stored one message per file.

```
testinghq blast fire --corpus ./mail --count 100
testinghq blast fire --send --config sweep/configs/zammad.toml --target zammad \
    --corpus ./zammad/test/data/mail --count 100 --out corpus.json
testinghq blast generate --corpus ./mail --target local --out corpus-files
```

## Options

| Option | With `--corpus` |
| --- | --- |
| `--corpus DIR` | Read every `.eml` and `.box` file under `DIR`, recursively, in order of the path relative to `DIR`. The extension match ignores case. Any other file is skipped and counted. |
| `--count N` | Read at most the first `N` files. The default is all of them. |
| `--seed N` | Part of each record id (`corpus-<seed>-<index>`) and tag; the files decide the content. |
| `--tag-prefix P` | The prefix of each message's tag. The default is drawn per run (`hq-<yymmddHHMM>-<4 hex>`). Applies to `--corpus` only. |
| `--mix`, `--catalog` | Not allowed: they name a different source. Naming two sources is a usage error (exit 1). |
| `--attachments` | Refused: each file brings its own attachments. |

Everything else (`--send`, `--rate`, `--target`, `--config`, `--out`,
`--fail-on-body`, `--allow-public-target`) works as it does for a generated
corpus, and the payload goes out in the target's format: any HTTP wire format,
or the `mime` bytes over SMTP for a `transport = "smtp"` target.

## What happens to each file

1. **Parse.** The bytes go through `email.message_from_bytes`. A leading mbox
   `From ` line is dropped. Encoded-word headers are decoded and raw UTF-8
   headers are read as UTF-8. The text and HTML bodies are the inline
   `text/plain` and `text/html` parts; every other part, an embedded message
   included, is an attachment. A bounce keeps its report: `message/rfc822`,
   `message/delivery-status`, `message/disposition-notification` and the
   other `message/*` parts are sent as structured parts, not opaque bytes.
2. **Rewrite every address.** Each address becomes
   `<stem>-<hash>@corpus.example.test`: the stem is the old local part reduced
   to `[a-z0-9-]`, and the hash is the first ten hex digits of the sha256 of
   the whole old address, compared without case. The same address always maps
   to the same new one, across files and runs. Address headers (From, To, Cc,
   Bcc, Reply-To, Sender, Return-Path, Delivered-To, X-Original-To, the
   Resent and receipt headers, and others) are parsed and rebuilt, and what in
   them is not an address is dropped. Every other header, the subject, both
   bodies, attachment file names and textual attachments (`text/*`,
   `message/*`, JSON, XML, so a bounce's Final-Recipient and
   Original-Recipient too) have anything shaped like an address rewritten. An
   address already on a reserved domain or TLD is kept. Binary attachments
   are sent as they are.
3. **Envelope.** The sender is the rewritten Return-Path, else the first From
   address (a null `<>` Return-Path stays null). The recipients are the To
   addresses, else the Cc ones, else `support@corpus.example.test`. A
   `transport = "smtp"` target's `envelope_from` and `envelope_to` still
   override these.
4. **Tag.** The same stamp the pipeline tools use: an `X-TestingHQ-Tag`
   header, the `[testinghq:<tag>]` marker at the end of both bodies, and a
   Message-ID built from the tag. The marker is also appended to the subject,
   so a system whose only search covers subjects can find the message.
5. **Check.** The rewritten payload's addresses go through the same synthetic
   address check every send does, and its headers and bodies are scanned for
   anything address-shaped that is not synthetic.

A file that is empty, has no header fields, cannot be rebuilt as a message,
or fails the check is **unparseable**: it gets a record with the
reason and is never sent. A run never stops with a traceback because of a
file.

## In the artifact

- `config.corpus_dir` (as given on the command line), `config.corpus_ignored`
  (files skipped for their extension), `config.count` (files read) and
  `config.tag_prefix`.
- One record per file read, in file order, each with `source`, the file's
  path relative to the directory. The category is `corpus`.
- An unparseable file's record has `outcome: "unparseable"` and `error`, and
  an empty response. It has no response class and raises no flag.
- `summary.unparseable` lists `{source, error}` for each unparseable file, and
  `summary.by_category.corpus` counts the records.

The usual grading applies to what was sent: a 5xx or no response is flagged,
a 4xx is recorded.

`blast replay` rebuilds a corpus run from the same directory with the recorded
seed and tag prefix, and refuses if any file changed, was removed or no longer
parses the same way.

## In `report`

The `report --json` keys do not change. A corpus run adds one finding of kind
`unparseable` per unparseable file (`<source>: <reason>`) and one of kind
`ignored` when files were skipped; `counts.sent` counts only what was sent and
`counts.records` every file read. The text report names the corpus directory,
and the HTML report shows each record's `source` and the config.
