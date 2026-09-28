// TestingHQ Blast web UI: the pure part of rendering.
//
// Everything in here is a function of its arguments and nothing else. No
// document, no window, no fetch, no timers. That is what lets CI run it under
// `node --test` with no browser and no DOM shim, which is the point of the
// file existing.
//
// WHY IT IS SEPARATE FROM app.js, and why that is not tidiness for its own sake.
//
// app.js used to carry its own `classifyRecord()`, re-deriving a record's
// outcome from its status code. That made the browser an independent copy of
// testinghq/core/report.py's rules, which is the exact hazard the lane-hygiene
// suite was written to prevent. The server now annotates every record with
// `outcome`, computed by the engine, and the browser renders it.
//
// The refactor that removed `classifyRecord` left one line behind:
//
//     window.__testingHQBlast = { classifyRecord };
//
// In strict mode, naming an identifier that does not exist is a ReferenceError
// at load, not a silent undefined. So app.js threw on every page load and the
// web UI did not work at all, for as long as that line was there. Nothing
// caught it: no JavaScript ran in CI, and the lane-hygiene test checks that
// `function classifyRecord(` is absent, which it was. The test asserted the
// copy was gone and said nothing about the reference to it.
//
// Two things follow from that, and both are why this file exists. The rendering
// decision is now in a module that CI can execute, so a name that does not
// resolve is a load error in a job that runs on every push. And a Python-side
// test asserts app.js contains no reference to the removed function at all,
// comments aside, so the same mistake cannot come back in a form the old guard
// would read as correct.
//
// The rules themselves are still not duplicated here. These functions read
// `record.outcome`, which the engine computed. A change to what an outcome means
// belongs in core/report.py alone, and this file follows it.
//
// Loading: a plain <script> in the browser sets window.TestingHQRender, and
// `require` under Node gets the same object. No build step, no package.json,
// no bundler, which is what the rest of this UI has and is not going to grow a
// dependency for.

(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.TestingHQRender = factory();
  }
})(
  typeof self !== "undefined" ? self : this,
  function () {
    "use strict";

    //: The row class for an outcome. Pure, and the reason the streaming table
    //: and the summary counters cannot disagree: both read the same outcome
    //: string the engine computed.
    function outcomeOf(record) {
      if (record && record.outcome) {
        return record.outcome;
      }
      // A record with no outcome came from a server older than the annotation.
      // Rendered as visibly unknown rather than guessed at: the browser is not
      // where the rules live, so it must not invent a verdict.
      return "unknown";
    }

    function isFailure(outcome) {
      return outcome === "clean_failed" || outcome === "degenerate_failed";
    }

    function rowClassFor(outcome) {
      if (outcome === "clean_failed") return "row-clean-failed";
      if (outcome === "degenerate_failed") return "row-degenerate-failed";
      return "row-ok";
    }

    //: The badge as plain data, not as an HTML string.
    //:
    //: The first version built an HTML fragment and returned it, which meant the
    //: only way to test it was to compare strings, and the only way to render it
    //: was innerHTML. Returning the class and the text separately lets the DOM
    //: code set them as properties, so nothing in this path is ever parsed as
    //: markup, and the badge can be checked without a browser.
    function badgeFor(outcome) {
      if (outcome === "ok") {
        return { className: "badge badge-ok", text: "pass", pass: true };
      }
      if (outcome === "unknown") {
        // Distinct from a failure on purpose. A record the server did not
        // annotate is a misconfiguration, and showing it as "fail" would
        // report a pipeline problem where the real problem is a version skew.
        return {
          className: "badge badge-unknown",
          text: "unknown",
          pass: null,
        };
      }
      return { className: "badge badge-fail", text: "fail", pass: false };
    }

    //: The empty-flags message. A run with no flags says so in words rather
    //: than showing an empty list, which reads as "did not check".
    var NO_FLAGS_MESSAGE =
      "No flags. Every clean payload 2xx'd; every degenerate payload was " +
      "rejected cleanly.";

    //: One row per record, with every presentation decision already made.
    function buildRows(records) {
      return (records || []).map(function (record) {
        var response = record.response || {};
        var status =
          response.status === null || response.status === undefined
            ? null
            : response.status;
        var latency =
          response.latency_ms === null || response.latency_ms === undefined
            ? null
            : response.latency_ms;
        var outcome = outcomeOf(record);
        return {
          id: record.id,
          category: record.category,
          // A null status means no response came back at all, which the engine
          // reports as a timeout. Rendering the raw null would put the word
          // "null" in a column headed "status".
          statusLabel: status === null ? "(timeout)" : String(status),
          latencyLabel: latency === null ? "-" : String(latency),
          outcome: outcome,
          rowClass: rowClassFor(outcome),
          badge: badgeFor(outcome),
        };
      });
    }

    //: A [label, value] pair list from a summary table, sorted by key.
    //:
    //: Sorted because the order comes out of the JSON object, and an unsorted
    //: table reshuffles between two runs of the same data, which makes a diff
    //: of two reports useless for spotting what changed.
    function summaryPairs(byKey) {
      return Object.keys(byKey || {})
        .sort()
        .map(function (key) {
          return [key, byKey[key]];
        });
    }

    //: Everything the UI shows for one run, derived from the artifact alone.
    //:
    //: One function so there is one answer. The counts in the summary panel and
    //: the classes on the rows used to be computed separately, and nothing
    //: checked that they agreed.
    function summarizeRun(artifact) {
      var a = artifact || {};
      var summary = a.summary || {};
      var rows = buildRows(a.records);

      var cleanFailed = 0;
      var degenerateFailed = 0;
      var unknown = 0;
      var passed = 0;
      rows.forEach(function (row) {
        if (row.outcome === "clean_failed") cleanFailed += 1;
        else if (row.outcome === "degenerate_failed") degenerateFailed += 1;
        else if (row.outcome === "unknown") unknown += 1;
        else passed += 1;
      });

      var flags = summary.flags || [];

      return {
        records: rows,
        counts: {
          total: rows.length,
          passed: passed,
          failed: cleanFailed + degenerateFailed,
          cleanFailed: cleanFailed,
          degenerateFailed: degenerateFailed,
          // A count of its own, because a server that sent no outcome is a
          // different problem from a run that passed, and the total alone
          // cannot tell them apart.
          unknown: unknown,
        },
        byStatus: summaryPairs(summary.by_status_class),
        byCategory: summaryPairs(summary.by_category),
        flags: flags,
        flagMessage: flags.length === 0 ? NO_FLAGS_MESSAGE : null,
      };
    }

    return {
      outcomeOf: outcomeOf,
      isFailure: isFailure,
      rowClassFor: rowClassFor,
      badgeFor: badgeFor,
      buildRows: buildRows,
      summaryPairs: summaryPairs,
      summarizeRun: summarizeRun,
      NO_FLAGS_MESSAGE: NO_FLAGS_MESSAGE,
    };
  }
);
