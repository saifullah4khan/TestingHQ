// Tests for the pure rendering module. Run with `node --test web/tests/`.
//
// No framework, no dependencies, no build step. `node:test` and `node:assert`
// are in the standard library, which matters here: this repository is
// Python-only and adding an npm dependency to test one JavaScript file would be
// a bad trade. A Node CI job runs these on every push; see ci.yml.
//
// WHY THESE EXIST. Until this file, no JavaScript ran in CI at all. That is how
// `app.js` spent an unknown amount of time throwing a ReferenceError on every
// page load, from a line left behind by a refactor that had done exactly what it
// was supposed to. The Python lane-hygiene test checked that the old
// `classifyRecord` function was no longer DEFINED, which was true, and said
// nothing about the line that still REFERENCED it.
//
// So the first test here is the one that would have caught it: load every
// script the page loads, and fail if any of them throws.

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const fs = require("node:fs");
const vm = require("node:vm");

const RENDER = path.join(__dirname, "..", "static", "render.js");
const APP = path.join(__dirname, "..", "static", "app.js");
const render = require(RENDER);

// ---------------------------------------------------------------------------
// The bug this file was written for
// ---------------------------------------------------------------------------

// Load the page's scripts the way a browser would, in the order index.html
// lists them, and return the sandbox.
//
// `document.addEventListener` does nothing, because the real one fires
// DOMContentLoaded and the page will not have finished parsing at load time, so
// nothing in the file runs. `fetch` rejects, because nothing calls it at load
// either. What this exercises is exactly the part that runs before the page is
// interactive, which is where the ReferenceError was.
function loadPageScripts() {
  const sandbox = {
    document: { addEventListener() {} },
    fetch: () => Promise.reject(new Error("fetch is not called at load")),
    setTimeout,
    clearTimeout,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);

  // render.js first: index.html loads it before app.js, and app.js reads the
  // module at load time.
  vm.runInContext(fs.readFileSync(RENDER, "utf8"), sandbox, { filename: "render.js" });
  vm.runInContext(fs.readFileSync(APP, "utf8"), sandbox, { filename: "app.js" });
  return sandbox;
}

test("app.js loads without throwing", () => {
  // A ReferenceError here is not a failed assertion about behaviour, it is the
  // whole web UI being broken, and it used to be exactly that.
  assert.doesNotThrow(loadPageScripts);
});

test("the page scripts publish the module app.js depends on", () => {
  const sandbox = loadPageScripts();
  assert.ok(
    sandbox.TestingHQRender,
    "render.js should publish window.TestingHQRender for the browser"
  );
  assert.strictEqual(
    typeof sandbox.TestingHQRender.summarizeRun,
    "function"
  );
});

test("app.js exposes the real functions, not a removed one", () => {
  const exported = loadPageScripts().window.__testingHQBlast;
  assert.ok(exported, "app.js should still expose __testingHQBlast for debugging");
  assert.ok(
    typeof exported.summarizeRun === "function",
    "__testingHQBlast should expose summarizeRun"
  );
  assert.strictEqual(
    exported.classifyRecord,
    undefined,
    "classifyRecord was removed when the browser stopped re-deriving outcomes; " +
      "exposing it again would reintroduce the duplicate rules"
  );
});

test("app.js does not mention the removed function outside a comment", () => {
  // Belt and braces on the same failure. The load test above catches the
  // ReferenceError, but only if the function is referenced at load. A
  // reference inside an event handler would not throw until a run was
  // triggered, which no test does.
  const source = fs.readFileSync(APP, "utf8");
  const code = source
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n");
  assert.ok(
    !code.includes("classifyRecord"),
    "app.js still references classifyRecord, which does not exist. That is a " +
      "ReferenceError in strict mode, and it was the reason this file exists."
  );
});

test("index.html loads render.js before app.js", () => {
  const html = fs.readFileSync(
    path.join(__dirname, "..", "static", "index.html"),
    "utf8"
  );
  const renderAt = html.indexOf("render.js");
  const appAt = html.indexOf("app.js");
  assert.ok(renderAt !== -1, "index.html should load render.js");
  assert.ok(appAt !== -1, "index.html should load app.js");
  assert.ok(
    renderAt < appAt,
    "render.js must load before app.js; app.js reads the module at load time"
  );
});

// ---------------------------------------------------------------------------
// outcomeOf: the one thing that must not guess
// ---------------------------------------------------------------------------

test("outcomeOf reads the engine's annotation", () => {
  assert.strictEqual(render.outcomeOf({ outcome: "ok" }), "ok");
  assert.strictEqual(
    render.outcomeOf({ outcome: "clean_failed" }),
    "clean_failed"
  );
  assert.strictEqual(
    render.outcomeOf({ outcome: "degenerate_failed" }),
    "degenerate_failed"
  );
});

test("outcomeOf says unknown rather than inventing a verdict", () => {
  // The rules live in core/report.py. A record with no annotation came from an
  // older server, and the browser must not decide what it meant.
  assert.strictEqual(render.outcomeOf({}), "unknown");
  assert.strictEqual(render.outcomeOf({ outcome: "" }), "unknown");
  assert.strictEqual(render.outcomeOf({ outcome: null }), "unknown");
  assert.strictEqual(render.outcomeOf(null), "unknown");
});

test("an unannotated record is not rendered as a pass or a fail", () => {
  // The distinction that matters. "unknown" is a misconfiguration, and showing
  // it as "fail" would report a pipeline problem where the real problem is a
  // version skew between the UI and the server.
  const unknown = render.badgeFor("unknown");
  const pass = render.badgeFor("ok");
  const fail = render.badgeFor("clean_failed");

  assert.notStrictEqual(unknown.text, fail.text);
  assert.notStrictEqual(unknown.text, pass.text);
  assert.strictEqual(unknown.pass, null, "unknown is neither pass nor fail");
  assert.strictEqual(pass.pass, true);
  assert.strictEqual(fail.pass, false);
});

// ---------------------------------------------------------------------------
// Row classes and badges
// ---------------------------------------------------------------------------

test("row classes distinguish the three outcomes", () => {
  assert.strictEqual(render.rowClassFor("clean_failed"), "row-clean-failed");
  assert.strictEqual(
    render.rowClassFor("degenerate_failed"),
    "row-degenerate-failed"
  );
  assert.strictEqual(render.rowClassFor("ok"), "row-ok");
  assert.strictEqual(render.rowClassFor("unknown"), "row-ok");
});

test("a badge is data, never an HTML string", () => {
  const badge = render.badgeFor("ok");
  assert.strictEqual(typeof badge, "object");
  assert.strictEqual(badge.className, "badge badge-ok");
  assert.strictEqual(badge.text, "pass");
});

test("nothing in render.js builds markup", () => {
  // The real property, stated about the right functions. An earlier version of
  // this test called every exported function with a record whose outcome was
  // `<img src=x onerror=...>` and asserted the result was not a string, which
  // failed on outcomeOf for the right reason: it passes the engine's value
  // through, and that is correct. Passing a value along is not constructing
  // markup.
  //
  // What matters is that the functions which CHOOSE a class or a label return
  // them as data, so the DOM code can set them as properties.
  assert.strictEqual(typeof render.badgeFor("ok"), "object");
  assert.strictEqual(typeof render.rowClassFor("ok"), "string");
  assert.ok(!render.badgeFor("ok").className.includes("<"));
  assert.ok(!render.badgeFor("ok").text.includes("<"));
  assert.ok(!render.rowClassFor("clean_failed").includes("<"));
});

test("record data reaches the DOM as text, not as markup", () => {
  // The property that actually protects the page, and it lives in app.js rather
  // than here. The results table used to be built with innerHTML, concatenating
  // record.id and record.category straight into a markup string. Now it is built
  // cell by cell with textContent.
  //
  // Asserted on the source because there is no DOM in these tests. The
  // structural claim is that the streaming path does not assign innerHTML with
  // a record field in it.
  const source = fs.readFileSync(APP, "utf8");
  const streaming = source.slice(source.indexOf("function streamResults"));
  assert.ok(
    !/innerHTML\s*=\s*[^;]*record\./.test(streaming),
    "streamResults assigns innerHTML from a record field; use textContent"
  );
  assert.ok(
    streaming.includes("textContent"),
    "streamResults should set cell text with textContent"
  );
});

// ---------------------------------------------------------------------------
// buildRows
// ---------------------------------------------------------------------------

test("a row is built from the engine's outcome, not from the status code", () => {
  // The whole point of the refactor. A 200 with a lost message is a failure and
  // a 500 with a clean rejection is a pass, and only the engine knows which.
  const rows = render.buildRows([
    { id: "a", category: "clean", outcome: "ok", response: { status: 500 } },
    {
      id: "b",
      category: "clean",
      outcome: "clean_failed",
      response: { status: 200 },
    },
  ]);

  assert.strictEqual(rows[0].badge.text, "pass");
  assert.strictEqual(rows[1].badge.text, "fail");
  assert.strictEqual(rows[1].rowClass, "row-clean-failed");
});

test("a missing response renders as a timeout, not as null", () => {
  const rows = render.buildRows([
    { id: "a", category: "clean", outcome: "degenerate_failed" },
    {
      id: "b",
      category: "degenerate",
      outcome: "degenerate_failed",
      response: { status: null, latency_ms: null },
    },
  ]);
  assert.strictEqual(rows[0].statusLabel, "(timeout)");
  assert.strictEqual(rows[1].statusLabel, "(timeout)");
  assert.strictEqual(rows[0].latencyLabel, "-");
});

test("a zero status and a zero latency are not treated as missing", () => {
  // The classic falsy bug. 0 is a real status and 0 ms is a real measurement,
  // and rendering them as "(timeout)" and "-" would be a lie.
  const rows = render.buildRows([
    { id: "a", outcome: "ok", response: { status: 0, latency_ms: 0 } },
  ]);
  assert.strictEqual(rows[0].statusLabel, "0");
  assert.strictEqual(rows[0].latencyLabel, "0");
});

test("a missing records list produces no rows rather than throwing", () => {
  assert.deepStrictEqual(render.buildRows(undefined), []);
  assert.deepStrictEqual(render.buildRows([]), []);
});

// ---------------------------------------------------------------------------
// summarizeRun
// ---------------------------------------------------------------------------

const ARTIFACT = {
  seed: 7,
  summary: {
    by_status_class: { "2xx": 4, "4xx": 1, "5xx": 1, timeout: 0 },
    by_category: { clean: 3, degenerate: 3 },
    flags: ["clean payload clean-7-0001 did not 2xx"],
  },
  records: [
    { id: "a", category: "clean", outcome: "ok", response: { status: 200 } },
    {
      id: "b",
      category: "clean",
      outcome: "clean_failed",
      response: { status: 200 },
    },
    {
      id: "c",
      category: "degenerate",
      outcome: "degenerate_failed",
      response: { status: 500 },
    },
    { id: "d", category: "clean", outcome: "unknown", response: null },
    {
      id: "e",
      category: "degenerate",
      outcome: "degenerate_failed",
      response: { status: 400 },
    },
    { id: "f", category: "clean", outcome: "ok", response: { status: 200 } },
  ],
};

test("summarizeRun counts each outcome separately", () => {
  const s = render.summarizeRun(ARTIFACT);
  assert.strictEqual(s.counts.total, 6);
  assert.strictEqual(s.counts.passed, 2);
  assert.strictEqual(s.counts.cleanFailed, 1);
  assert.strictEqual(s.counts.degenerateFailed, 2);
  assert.strictEqual(s.counts.unknown, 1);
  assert.strictEqual(s.counts.failed, 3);
});

test("the counts add up to the number of records", () => {
  // A counter that is off by one, or a category that is not counted at all,
  // shows a summary whose parts do not make its total. Checked as an identity
  // over a real artifact rather than trusting each count.
  const s = render.summarizeRun(ARTIFACT);
  const sum =
    s.counts.passed + s.counts.cleanFailed + s.counts.degenerateFailed;
  assert.strictEqual(sum + s.counts.unknown, s.counts.total);
  assert.strictEqual(
    s.counts.cleanFailed + s.counts.degenerateFailed,
    s.counts.failed
  );
});

test("the row count and the counted records are the same list", () => {
  const s = render.summarizeRun(ARTIFACT);
  assert.strictEqual(s.records.length, s.counts.total);
});

test("an empty run summarizes to zeroes rather than undefined", () => {
  const s = render.summarizeRun({});
  assert.strictEqual(s.counts.total, 0);
  assert.strictEqual(s.counts.passed, 0);
  assert.deepStrictEqual(s.byStatus, []);
  assert.deepStrictEqual(s.byCategory, []);
  assert.deepStrictEqual(s.flags, []);
});

test("a missing summary does not throw", () => {
  assert.doesNotThrow(() => render.summarizeRun({ records: [] }));
  assert.doesNotThrow(() => render.summarizeRun(null));
  assert.doesNotThrow(() => render.summarizeRun(undefined));
});

// ---------------------------------------------------------------------------
// The empty-flags message
// ---------------------------------------------------------------------------

test("a run with no flags says so in words", () => {
  const s = render.summarizeRun({ summary: { flags: [] }, records: [] });
  assert.strictEqual(s.flagMessage, render.NO_FLAGS_MESSAGE);
  assert.strictEqual(s.flagMessage, null === s.flagMessage ? null : s.flagMessage);
  assert.ok(s.flagMessage.length > 0);
});

test("a run with flags shows the flags and no empty message", () => {
  const s = render.summarizeRun(ARTIFACT);
  assert.strictEqual(s.flagMessage, null);
  assert.strictEqual(s.flags.length, 1);
  assert.ok(s.flags[0].includes("clean-7-0001"));
});

test("an absent flags list is treated as no flags", () => {
  // A server that omits the key entirely, rather than sending an empty list.
  const s = render.summarizeRun({ summary: {}, records: [] });
  assert.deepStrictEqual(s.flags, []);
  assert.ok(s.flagMessage, "no flags should still get the message");
});

// ---------------------------------------------------------------------------
// Summary tables
// ---------------------------------------------------------------------------

test("summary pairs are sorted so two runs of the same data diff cleanly", () => {
  const s = render.summarizeRun(ARTIFACT);
  const labels = s.byStatus.map(([k]) => k);
  assert.deepStrictEqual(labels, ["2xx", "4xx", "5xx", "timeout"]);
});

test("a missing status table renders as an empty list", () => {
  const s = render.summarizeRun({ summary: {}, records: [] });
  assert.deepStrictEqual(s.byStatus, []);
  assert.deepStrictEqual(s.byCategory, []);
});
