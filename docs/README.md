# TestingHQ documentation

Start with the [project README](../README.md): what each tool answers, a quick
start, and the exit codes.

## Reference

- [CONFIG.md](CONFIG.md): every key in the config file (`[targets.<name>]`,
  `[readback]`, `[readback.fields]`, `[readback.headers]`, `[loop]`,
  `[loop.outbound]`, `[steady]`, `[sigcheck]`), generated from the schema in
  the code.
- [FORMATS.md](FORMATS.md): the five wire formats (`sendgrid`, `sendgrid-raw`,
  `mailgun`, `postmark`, `mime`), field by field, and the synthetic provider
  fields.
- [SECURITY.md](SECURITY.md): what the tool cannot do, what needs explicit
  consent (public targets and readback hosts), the threat model, and how to
  report a vulnerability.

## Guides

- [DOGFOOD.md](DOGFOOD.md): running the suite against a real staging
  deployment, with the consent flags and readback timing it needs.
- [examples/README.md](../examples/README.md): the loopback demo, the sample
  target config and the readback recipes for Zendesk, Freshdesk, Mailpit,
  Chatwoot, a generic REST API and a mail sink.

## Decisions

- [0001: how Barrage paces load](decisions/0001-barrage-pacing.md)
- [0002: the Python floor is 3.10](decisions/0002-python-floor-3-10.md)
- [0003: what the suite protects](decisions/0003-what-the-suite-protects.md)

## Direction

- [ROADMAP.md](ROADMAP.md): goals, the 1.x timeline, what 2.0 could hold,
  and what was set aside on purpose.

## Project

- [CHANGELOG.md](../CHANGELOG.md): what changed in each release.
- [CONTRIBUTING.md](../CONTRIBUTING.md): setting up, running the suites, and
  commit style.
