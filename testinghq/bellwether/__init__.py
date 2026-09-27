"""Bellwether: a regression differ for run artifacts.

Blast answers "is my parser correct under this messy input". Barrage answers
"does my pipeline hold under this much load". Neither can answer the question
you actually have after you change something: **did that change help?**

Today that question is answered by running the same seed twice and eyeballing
two terminal summaries, or opening two JSON files in a diff tool and reading
past a hundred unchanged records to find the three that moved. That is manual,
it is easy to get wrong, and the thing you most want to notice, a payload that
used to pass and now fails, is the easiest thing to miss.

Bellwether takes a baseline artifact and a candidate artifact and reports only
what moved.

TWO PROPERTIES WORTH KNOWING BEFORE YOU TRUST IT:

1. It cannot send anything. It reads two JSON files and prints a report. There
   is no target resolution, no transport, no `--send`, and no guardrail gate,
   because there is nothing to gate. Every other subcommand can put bytes on a
   wire; this one cannot, by construction rather than by policy. That is also
   why it needs no configured target and refuses nothing.

2. It defines no classification rules of its own. Outcomes come from
   `testinghq.core.report.classify_record`, the same canonical implementation
   that built both artifacts. If this module ever grew its own notion of what
   counts as a failure it would be a fourth copy of the expectation rules,
   which is a mistake this repository has already made three times.
"""
