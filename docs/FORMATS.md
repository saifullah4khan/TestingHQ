# Wire formats

Every send uses the format named by the target's `format` key in
`[targets.<name>]`. Absent means `sendgrid`. An unknown name is refused when the
config loads.

```toml
[targets.staging]
url = "https://staging.example.test/inbound"
format = "sendgrid-raw"
```

`blast generate --target <name>` writes files in that target's format, so you
can inspect the exact bytes before sending anything.

All formats are deterministic: the same payload always produces the same bytes.
Multipart boundaries are fixed and JSON keys are sorted. None of these formats
has been verified against a live provider account; each is written from the
provider's published documentation.

## sendgrid (default)

SendGrid Inbound Parse, parsed mode. `multipart/form-data`.

| Field | Value |
| --- | --- |
| `headers` | Raw header block, one `Name: value` line per header |
| `dkim` | Synthetic, see below |
| `content-ids` | JSON map of Content-ID to attachment field name; `{}` when no part is inline |
| `to`, `cc`, `from`, `subject` | From the message; `cc` is the Cc header or empty |
| `text`, `html` | The two bodies |
| `sender_ip`, `spam_report`, `spam_score` | Synthetic, see below |
| `envelope` | JSON `{"from": "...", "to": [...]}` |
| `charsets` | JSON map of field to charset; always includes `text` |
| `SPF` | Synthetic, see below |
| `attachments` | Attachment count |
| `attachment-info` | JSON map of `attachmentN` to `filename`, `name`, `type`, and `content-id` for an inline part (present only when there are attachments) |
| `attachment1` .. `attachmentN` | One file part per attachment |

An image attachment is treated as inline and gets a deterministic Gmail-style
Content-ID (`ii_` plus 16 hex characters). Other attachments have none, which
matches SendGrid's documented example.

## sendgrid-raw

SendGrid Inbound Parse with "POST the raw, full MIME message" enabled.
`multipart/form-data`.

| Field | Value |
| --- | --- |
| `email` | The full RFC 5322 message, built exactly as the `mime` format builds it, with its own boundary |
| `to`, `from`, `subject` | From the message |
| `envelope` | JSON `{"from": "...", "to": [...]}` |
| `charsets` | JSON map for `to`, `from` and `subject` |
| `dkim`, `SPF`, `sender_ip`, `spam_score`, `spam_report` | Synthetic, see below |

There are no `text`, `html`, `headers` or attachment parts: a raw-mode receiver
parses the message itself. This is the shape Chatwoot's Action Mailbox ingress
accepts for SendGrid.

## Synthetic provider fields

SendGrid computes these on its own side. TestingHQ sends fixed values with
SendGrid's shape that claim nothing about a real domain or host.

| Field | Value |
| --- | --- |
| `dkim` | `{@example.com : pass}` |
| `SPF` | `pass` |
| `sender_ip` | `192.0.2.1` (TEST-NET-1, RFC 5737) |
| `spam_score` | `0.0` |
| `spam_report` | empty |

A pipeline that rejects mail on these values will accept every TestingHQ
message. Test that logic separately.

## mailgun

Mailgun inbound routing ("forward" action). `multipart/form-data`.

`recipient` (envelope recipient), `sender` (envelope sender), `from` (From
header), `subject`, `body-plain`, `body-html`, `message-headers` (JSON list of
`[name, value]` pairs), `attachment-count` when there are attachments, and
`attachment-1` .. `attachment-N`. `stripped-text`, `stripped-html` and
`stripped-signature` are not sent: Mailgun derives them with its own parser.
`timestamp`, `token` and `signature` are sent only when signature fields are
supplied, never fabricated.

## postmark

Postmark inbound webhook. `application/json`, one object with sorted keys.

`From`, `To`, `Cc`, `Bcc`, `Subject`, `MessageID`, `Headers` (list of
`{"Name", "Value"}`), `HtmlBody`, `TextBody`, `ReplyTo`, `StrippedTextReply`,
and `Attachments` (list of `Name`, `ContentType`, `ContentLength`, base64
`Content`; an empty list when there are none). Postmark sends no envelope
field, so none is invented.

## mime

The whole RFC 5322 message as the request body, `Content-Type:
message/rfc822`, CRLF line endings. Text and HTML are `multipart/alternative`;
attachments make it `multipart/mixed`. Every boundary is derived from the
fixed one (`<boundary>`, `<boundary>-1`, ...). Non-ASCII headers are RFC 2047
encoded. For a receiver that takes a raw message, such as osTicket's
`api/tickets.email` or an SMTP-to-HTTP bridge.
