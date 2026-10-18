// Tests for testinghq/report_html/render.js. Run with
// `node --test tests/report_html/*.test.cjs`.
//
// TESTINGHQ_RENDER_JS points the tests at another copy of the file, which is
// how CI runs them against the render.js a wheel install put on disk.

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RENDER =
  process.env.TESTINGHQ_RENDER_JS ||
  path.join(__dirname, "..", "..", "testinghq", "report_html", "render.js");
const SOURCE = fs.readFileSync(RENDER, "utf8");
const render = require(RENDER);

const rows = (...lists) => lists.map((cells) => ({ cells }));
const firsts = (list) => list.map((row) => row.cells[0]);

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

test("require exports the pure functions", () => {
  for (const name of ["compareCells", "sortRows", "filterRows", "matchesQuery",
                      "nextDirection", "numericValue", "enhance"]) {
    assert.strictEqual(typeof render[name], "function", name);
  }
});

test("as a browser script it publishes TestingHQRender and wires the page", () => {
  let queried = 0;
  const sandbox = {
    document: {
      querySelectorAll() {
        queried += 1;
        return [];
      },
    },
  };
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, { filename: "render.js" });
  assert.strictEqual(typeof sandbox.TestingHQRender.sortRows, "function");
  assert.ok(queried > 0, "enhance ran against the document on load");
});

test("the file can be inlined and loads nothing", () => {
  assert.ok(!/<\/script/i.test(SOURCE), "a closing script tag would end the inline block");
  assert.ok(!/https?:\/\//.test(SOURCE), "no URLs");
  for (const api of ["fetch(", "XMLHttpRequest", "import(", "WebSocket", "sendBeacon"]) {
    assert.ok(!SOURCE.includes(api), `render.js uses ${api}`);
  }
});

// ---------------------------------------------------------------------------
// Ordering
// ---------------------------------------------------------------------------

test("numbers compare as numbers, not as text", () => {
  assert.ok(render.compareCells("9", "10") < 0);
  assert.ok(render.compareCells("1.5", "-2") > 0);
  assert.strictEqual(render.compareCells("3", "3.0"), 0);
});

test("text compares without case", () => {
  assert.ok(render.compareCells("apple", "Banana") < 0);
  assert.strictEqual(render.compareCells("Clean", "clean"), 0);
});

test("numbers sort before text and empty cells sort last", () => {
  assert.ok(render.compareCells("200", "timeout") < 0);
  assert.ok(render.compareCells("", "a") > 0);
  assert.ok(render.compareCells("a", "") < 0);
  assert.strictEqual(render.compareCells("", ""), 0);
  assert.ok(render.compareCells(null, "1") > 0);
});

test("numericValue reads plain numbers only", () => {
  assert.strictEqual(render.numericValue(" 42 "), 42);
  assert.strictEqual(render.numericValue("1e3"), 1000);
  assert.strictEqual(render.numericValue("12ms"), null);
  assert.strictEqual(render.numericValue(""), null);
});

test("sortRows sorts a copy, ascending and descending", () => {
  const input = rows(["b", "500"], ["a", "200"], ["c", "40"]);
  assert.deepStrictEqual(firsts(render.sortRows(input, 1, false)), ["c", "a", "b"]);
  assert.deepStrictEqual(firsts(render.sortRows(input, 1, true)), ["b", "a", "c"]);
  assert.deepStrictEqual(firsts(input), ["b", "a", "c"], "input untouched");
});

test("sortRows is stable for equal cells", () => {
  const input = rows(["first", "x"], ["second", "x"], ["third", "x"]);
  assert.deepStrictEqual(firsts(render.sortRows(input, 1, false)), ["first", "second", "third"]);
  assert.deepStrictEqual(firsts(render.sortRows(input, 1, true)), ["first", "second", "third"]);
});

test("empty cells stay last in both directions", () => {
  const input = rows(["none", ""], ["low", "1"], ["high", "9"]);
  assert.deepStrictEqual(firsts(render.sortRows(input, 1, false)), ["low", "high", "none"]);
  assert.deepStrictEqual(firsts(render.sortRows(input, 1, true)), ["high", "low", "none"]);
});

test("a header click toggles ascending and descending", () => {
  assert.strictEqual(render.nextDirection(null), "ascending");
  assert.strictEqual(render.nextDirection("ascending"), "descending");
  assert.strictEqual(render.nextDirection("descending"), "ascending");
});

// ---------------------------------------------------------------------------
// Filtering
// ---------------------------------------------------------------------------

test("filtering matches any cell, ignoring case", () => {
  const input = rows(["clean-1", "200"], ["degenerate-1", "500"]);
  assert.deepStrictEqual(firsts(render.filterRows(input, "DEGEN")), ["degenerate-1"]);
  assert.deepStrictEqual(firsts(render.filterRows(input, "200")), ["clean-1"]);
  assert.deepStrictEqual(render.filterRows(input, "nothing"), []);
});

test("an empty query keeps every row", () => {
  const input = rows(["a"], ["b"]);
  assert.strictEqual(render.filterRows(input, "").length, 2);
  assert.strictEqual(render.filterRows(input, "   ").length, 2);
  assert.strictEqual(render.filterRows(undefined, "x").length, 0);
});

// ---------------------------------------------------------------------------
// enhance, against a minimal fake DOM
// ---------------------------------------------------------------------------

function fakeElement(attrs) {
  const attributes = Object.assign({}, attrs);
  const listeners = {};
  return {
    hidden: false,
    getAttribute: (name) => (name in attributes ? attributes[name] : null),
    setAttribute: (name, value) => { attributes[name] = value; },
    removeAttribute: (name) => { delete attributes[name]; },
    addEventListener: (type, fn) => { listeners[type] = fn; },
    fire: (type) => listeners[type](),
  };
}

function fakeTable(id, header, body) {
  const buttons = header.map(() => fakeElement());
  const cells = header.map((_, i) => {
    const th = fakeElement();
    th.querySelector = () => buttons[i];
    return th;
  });
  const tbody = { rows: [] };
  tbody.rows = body.map((values) => ({
    hidden: false,
    cells: values.map((text) => ({ textContent: text })),
  }));
  tbody.appendChild = (node) => {
    tbody.rows.splice(tbody.rows.indexOf(node), 1);
    tbody.rows.push(node);
  };
  return { id, tHead: { rows: [{ cells }] }, tBodies: [tbody], buttons, cells, tbody };
}

function fakeDocument(table) {
  const input = fakeElement({ "data-filter": table.id });
  input.hidden = true;
  input.value = "";
  return {
    input,
    querySelectorAll: (selector) =>
      selector.startsWith("table") ? [table] : selector.startsWith("input") ? [input] : [],
    getElementById: (id) => (id === table.id ? table : null),
  };
}

test("enhance sorts a table when its header is clicked", () => {
  const table = fakeTable("t", ["id", "status"], [["a", "500"], ["b", "200"], ["c", "404"]]);
  const doc = fakeDocument(table);
  assert.strictEqual(render.enhance(doc), 1);

  const order = () => table.tbody.rows.map((row) => row.cells[0].textContent);
  table.buttons[1].fire("click");
  assert.deepStrictEqual(order(), ["b", "c", "a"]);
  assert.strictEqual(table.cells[1].getAttribute("aria-sort"), "ascending");

  table.buttons[1].fire("click");
  assert.deepStrictEqual(order(), ["a", "c", "b"]);
  assert.strictEqual(table.cells[1].getAttribute("aria-sort"), "descending");

  table.buttons[0].fire("click");
  assert.strictEqual(table.cells[1].getAttribute("aria-sort"), null, "only one header is sorted");
});

test("enhance shows the filter box and hides rows that do not match", () => {
  const table = fakeTable("t", ["id"], [["clean-1"], ["degenerate-1"]]);
  const doc = fakeDocument(table);
  render.enhance(doc);
  assert.strictEqual(doc.input.hidden, false, "the box is hidden until a script can drive it");

  doc.input.value = "degen";
  doc.input.fire("input");
  assert.deepStrictEqual(table.tbody.rows.map((row) => row.hidden), [true, false]);

  doc.input.value = "";
  doc.input.fire("input");
  assert.deepStrictEqual(table.tbody.rows.map((row) => row.hidden), [false, false]);
});
