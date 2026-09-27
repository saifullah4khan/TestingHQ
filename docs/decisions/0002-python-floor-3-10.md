# Decision 0002: the Python floor is 3.10, not 3.9

Date: 2026-09-27. Status: accepted, 2026-09-27.

`requires-python` moves from `>=3.9` to `>=3.10`, and 3.9 comes out of every
CI matrix.

## Why

Two independent reasons, either of which is sufficient.

**3.9 reached end of life in October 2025.** Declaring support for an
unmaintained runtime invites installs on an interpreter that receives no
security fixes, which is the opposite of what a package metadata field is for.

**3.9 could not pass the dependency audit, and there was no honest way to make
it.** `pip-audit` audits the whole environment, which includes the runner's
`setuptools`. The fix for PYSEC-2026-3447 is setuptools 83.0.0, and 83.0.0
declares `requires-python >=3.10`, so on a 3.9 runner pip can install at most
79.0.1, which is the vulnerable version. The `dependency-audit (py3.9)` leg
therefore failed on every run, and the only way past it was `--ignore-vuln`,
which hides real CVEs to buy a green tick.

That second reason is worth dwelling on, because it was a property of the
toolchain rather than of this project. The project itself has no runtime
dependency on 3.9 beyond `tomli`, and OSV reports zero advisories against
`tomli`. So the argument for dropping 3.9 is not "3.9 is unsafe for this
package". It is "3.9 is unmaintained, and a support claim we cannot verify in
CI is not a claim worth making".

## What the code needed

Nothing. Every file already parsed as 3.9 syntax, and the only version-sensitive
code is the `tomllib`/`tomli` fallback, which is handled and is now exercised
by the 3.10 matrix leg. This was a metadata and CI change.

## What it did not fix

**The `parseaddr` window is narrowed, not closed.**

`docs/SECURITY.md` records that a `parseaddr`-based extractor would be a live
bypass of the synthetic-content guardrail on any Python older than 3.9.19 /
3.10.14 / 3.11.9 / 3.12.4, the versions before the CVE-2023-27043 fix.

Raising the floor to `>=3.10` removes 3.9 from the permitted set. It does
**not** remove 3.10.0 through 3.10.13, which predate the fix on the 3.10 line
and are still permitted. Only a patch-level floor of `>=3.10.14` would close
it, and that is not declared: it would exclude patch releases people are
already running, for a vulnerability that is not reachable today, because
`guardrails._extract_addresses` uses `getaddresses` and the note exists to stop
someone rewriting it to use `parseaddr`.

So this is defence in depth against a change nobody has made, not a live
vulnerability. Worth being precise about, because "we raised the floor" reads
like more than it is.

## How the floor is kept honest

`tests/unit/test_python_support_claims.py` asserts that `requires-python` and
the `ci.yml` matrix name the same versions, that the matrix tests the ceiling
as well as the floor, and that the interpreter running the suite is one the
project claims. A floor that drifts from the matrix fails the build.

## The 3.9 users

Anyone installing on 3.9 now needs 3.10. That is a real cost and the reason
this was raised as a question rather than done unilaterally. 3.10 is itself in
security-only support until October 2026, so the same argument will apply to it
when its time comes, and the same note should be written then.
