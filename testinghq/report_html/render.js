// TestingHQ report page: sorting and filtering for the tables that
// `testinghq report --html` writes.
//
// The page is complete without this script. Python renders every verdict,
// count and row; this file only reorders and hides rows that are already
// there, so it never decides what a result means.
//
// The pure functions are exported for `node --test`. In a browser the same
// file publishes window.TestingHQRender and wires up the page it is inlined
// into. It makes no network requests and loads nothing.

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
    return;
  }
  root.TestingHQRender = api;
  if (root.document && typeof root.document.querySelectorAll === "function") {
    api.enhance(root.document);
  }
})(
  typeof self !== "undefined" ? self : this,
  function () {
    "use strict";

    var NUMBER = /^-?\d+(?:\.\d+)?(?:e[+-]?\d+)?$/i;

    //: A cell's numeric value, or null when it is not a plain number.
    function numericValue(text) {
      var trimmed = String(text === null || text === undefined ? "" : text).trim();
      if (!NUMBER.test(trimmed)) {
        return null;
      }
      return Number(trimmed);
    }

    //: Order two cell texts. Empty cells sort last, numbers compare as
    //: numbers, and anything else compares as case-insensitive text.
    function compareCells(a, b) {
      var left = String(a === null || a === undefined ? "" : a).trim();
      var right = String(b === null || b === undefined ? "" : b).trim();
      if (left === "" && right === "") return 0;
      if (left === "") return 1;
      if (right === "") return -1;
      var ln = numericValue(left);
      var rn = numericValue(right);
      if (ln !== null && rn !== null) {
        return ln < rn ? -1 : ln > rn ? 1 : 0;
      }
      if (ln !== null) return -1;
      if (rn !== null) return 1;
      var lc = left.toLowerCase();
      var rc = right.toLowerCase();
      return lc < rc ? -1 : lc > rc ? 1 : 0;
    }

    //: A sorted copy of rows ({cells: [...]}) by one column. Stable, and the
    //: input is left as it was. Empty cells stay last in both directions.
    function sortRows(rows, column, descending) {
      var indexed = (rows || []).map(function (row, index) {
        return { row: row, index: index };
      });
      indexed.sort(function (x, y) {
        var a = (x.row.cells || [])[column];
        var b = (y.row.cells || [])[column];
        var aEmpty = String(a === null || a === undefined ? "" : a).trim() === "";
        var bEmpty = String(b === null || b === undefined ? "" : b).trim() === "";
        var order;
        if (aEmpty || bEmpty) {
          order = compareCells(a, b);
        } else {
          order = descending ? compareCells(b, a) : compareCells(a, b);
        }
        return order !== 0 ? order : x.index - y.index;
      });
      return indexed.map(function (entry) {
        return entry.row;
      });
    }

    //: True when any cell contains the query, ignoring case. An empty query
    //: matches every row.
    function matchesQuery(row, query) {
      var needle = String(query || "").trim().toLowerCase();
      if (needle === "") return true;
      return (row.cells || []).some(function (cell) {
        return String(cell === null || cell === undefined ? "" : cell)
          .toLowerCase()
          .indexOf(needle) !== -1;
      });
    }

    function filterRows(rows, query) {
      return (rows || []).filter(function (row) {
        return matchesQuery(row, query);
      });
    }

    //: The aria-sort value a header moves to when it is clicked.
    function nextDirection(current) {
      return current === "ascending" ? "descending" : "ascending";
    }

    function toArray(list) {
      return Array.prototype.slice.call(list || []);
    }

    function rowsOf(tbody) {
      return toArray(tbody.rows).map(function (node) {
        return {
          node: node,
          cells: toArray(node.cells).map(function (cell) {
            return cell.textContent;
          }),
        };
      });
    }

    function wireSorting(table) {
      var head = table.tHead && table.tHead.rows[0];
      var tbody = table.tBodies && table.tBodies[0];
      if (!head || !tbody) return;
      var headers = toArray(head.cells);
      headers.forEach(function (th, column) {
        var button = th.querySelector("button");
        if (!button) return;
        button.addEventListener("click", function () {
          var direction = nextDirection(th.getAttribute("aria-sort"));
          headers.forEach(function (other) {
            other.removeAttribute("aria-sort");
          });
          th.setAttribute("aria-sort", direction);
          sortRows(rowsOf(tbody), column, direction === "descending").forEach(
            function (row) {
              tbody.appendChild(row.node);
            }
          );
        });
      });
    }

    function wireFilter(doc, input) {
      var table = doc.getElementById(input.getAttribute("data-filter"));
      if (!table || !table.tBodies || !table.tBodies[0]) return;
      var tbody = table.tBodies[0];
      input.hidden = false;
      input.addEventListener("input", function () {
        rowsOf(tbody).forEach(function (row) {
          row.node.hidden = !matchesQuery(row, input.value);
        });
      });
    }

    //: Attach sorting to every table marked data-sortable and show the
    //: filter boxes, which are hidden until a script can make them work.
    //: Returns the number of tables wired, for the tests.
    function enhance(doc) {
      var tables = toArray(doc.querySelectorAll("table[data-sortable]"));
      tables.forEach(wireSorting);
      toArray(doc.querySelectorAll("input[data-filter]")).forEach(function (input) {
        wireFilter(doc, input);
      });
      return tables.length;
    }

    return {
      numericValue: numericValue,
      compareCells: compareCells,
      sortRows: sortRows,
      matchesQuery: matchesQuery,
      filterRows: filterRows,
      nextDirection: nextDirection,
      enhance: enhance,
    };
  }
);
