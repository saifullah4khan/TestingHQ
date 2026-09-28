// TestingHQ - Blast web UI
//
// Vanilla JS, no build step, no framework. Talks to the local stdlib server
// in web/server.py over two endpoints: POST /api/dry-run (default, never
// sends) and POST /api/fire (requires a configured target and an explicit
// confirm step).
//
// The expectation rules are NOT duplicated here. This file used to carry its
// own classifyRecord(), re-deriving the outcome from the status code, which
// made the browser a third copy of testinghq/core/report.py's rules. The
// server now annotates every record with `outcome`, computed by the engine,
// and this file renders it. A rule change belongs in core/report.py alone.
//
// The presentation decisions live in render.js, which is a separate file so CI
// can run them under `node --test` with no browser. That is not tidiness: this
// file used to end with
//
//     window.__testingHQBlast = { classifyRecord };
//
// left behind by the refactor above. Naming an identifier that does not exist
// is a ReferenceError in strict mode, not a silent undefined, so the page threw
// on every load and the web UI did not work at all. Nothing caught it, because
// no JavaScript ran in CI and the Python lane-hygiene test checks that
// `function classifyRecord(` is gone, which it was. It said nothing about the
// line that still referenced it.
//
// So the debug export below names functions that exist, and
// tests/web/render.test.cjs loads this file the way a browser would and fails
// if it throws.

(function () {
  "use strict";

  const R = window.TestingHQRender;

  const state = {
    targets: [],
    categories: [],
  };

  function $(id) {
    return document.getElementById(id);
  }

  async function fetchConfig() {
    const res = await fetch("/api/config");
    if (!res.ok) {
      throw new Error("failed to load /api/config: " + res.status);
    }
    return res.json();
  }

  function renderMixOptions(categories) {
    const container = $("mix-options");
    container.innerHTML = "";
    categories.forEach((cat) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "mix";
      input.value = cat;
      input.checked = true;
      label.appendChild(input);
      label.appendChild(document.createTextNode(cat));
      container.appendChild(label);
    });
  }

  function renderTargetOptions(targets) {
    const select = $("target");
    while (select.options.length > 1) {
      select.remove(1);
    }
    targets.forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.name;
      opt.textContent = t.name;
      select.appendChild(opt);
    });
  }

  function selectedMix() {
    return Array.from(document.querySelectorAll('input[name="mix"]:checked')).map(
      (el) => el.value
    );
  }

  function showError(message) {
    const el = $("run-error");
    if (!message) {
      el.classList.add("hidden");
      el.textContent = "";
      return;
    }
    el.textContent = message;
    el.classList.remove("hidden");
  }

  function renderMiniTable(tableEl, rows) {
    tableEl.innerHTML = "";
    rows.forEach(([label, value]) => {
      const tr = document.createElement("tr");
      const th = document.createElement("td");
      th.textContent = label;
      const td = document.createElement("td");
      td.textContent = value;
      tr.appendChild(th);
      tr.appendChild(td);
      tableEl.appendChild(tr);
    });
  }

  function renderSummary(artifact) {
    $("summary-empty").classList.add("hidden");
    $("summary-content").classList.remove("hidden");

    // Every number and every decision below comes from one call, so the counts
    // in this panel and the classes on the rows cannot disagree. They used to
    // be computed separately and nothing checked that they did.
    const s = R.summarizeRun(artifact);

    $("count-clean-failed").textContent = s.counts.cleanFailed;
    $("count-degenerate-failed").textContent = s.counts.degenerateFailed;

    renderMiniTable($("status-class-table"), s.byStatus);
    renderMiniTable($("category-table"), s.byCategory);

    const flagsList = $("flags-list");
    flagsList.innerHTML = "";
    if (s.flagMessage) {
      const li = document.createElement("li");
      li.textContent = s.flagMessage;
      flagsList.appendChild(li);
    } else {
      s.flags.forEach((flag) => {
        const li = document.createElement("li");
        li.textContent = flag;
        flagsList.appendChild(li);
      });
    }
  }

  // "Streaming" results table: append rows with a short stagger instead of
  // dumping the whole table at once, so a run reads as it arrives.
  function streamResults(rows) {
    $("results-empty").classList.add("hidden");
    $("results-table-wrap").classList.remove("hidden");
    const tbody = $("results-tbody");
    tbody.innerHTML = "";

    const delayPerRow = rows.length > 60 ? 0 : 12;

    rows.forEach((row, i) => {
      window.setTimeout(() => {
        const tr = document.createElement("tr");
        tr.className = row.rowClass;
        // Built with textContent, cell by cell. Nothing here is parsed as
        // markup, so a record id or category containing a tag is displayed
        // rather than executed.
        [row.id, row.category, row.statusLabel, row.latencyLabel].forEach(
          (value) => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
          }
        );
        const badgeCell = document.createElement("td");
        const badge = document.createElement("span");
        badge.className = row.badge.className;
        badge.textContent = row.badge.text;
        badgeCell.appendChild(badge);
        tr.appendChild(badgeCell);
        tbody.appendChild(tr);
      }, i * delayPerRow);
    });
  }

  function render(artifact) {
    const s = R.summarizeRun(artifact);
    renderSummary(artifact);
    streamResults(s.records);
  }

  function currentRunParams() {
    const count = parseInt($("count").value, 10);
    const seed = parseInt($("seed").value, 10);
    const mix = selectedMix();
    return { count, seed, mix };
  }

  async function runDryRun(event) {
    if (event) event.preventDefault();
    showError(null);
    const { count, seed, mix } = currentRunParams();
    try {
      const res = await fetch("/api/dry-run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mix, count, seed }),
      });
      const payload = await res.json();
      if (!res.ok) {
        showError(payload.error || "dry-run failed");
        return;
      }
      render(payload);
    } catch (err) {
      showError(String(err));
    }
  }

  function openFireConfirm() {
    showError(null);
    const target = $("target").value;
    if (!target) {
      showError("Select a configured target before firing.");
      return;
    }
    const { count } = currentRunParams();
    $("fire-confirm-target").textContent = target;
    $("fire-confirm-count").textContent = String(count);
    $("fire-confirm").classList.remove("hidden");
  }

  function closeFireConfirm() {
    $("fire-confirm").classList.add("hidden");
  }

  async function confirmFire() {
    showError(null);
    const { count, seed, mix } = currentRunParams();
    const target = $("target").value;
    try {
      const res = await fetch("/api/fire", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, mix, count, seed, confirm: true }),
      });
      const payload = await res.json();
      closeFireConfirm();
      if (!res.ok) {
        showError(payload.error || "fire failed");
        return;
      }
      render(payload);
    } catch (err) {
      closeFireConfirm();
      showError(String(err));
    }
  }

  async function init() {
    try {
      const cfg = await fetchConfig();
      state.targets = cfg.targets || [];
      state.categories = cfg.categories || [];
      renderMixOptions(state.categories);
      renderTargetOptions(state.targets);
    } catch (err) {
      showError(String(err));
    }

    $("run-form").addEventListener("submit", runDryRun);
    $("fire-btn").addEventListener("click", openFireConfirm);
    $("fire-confirm-no").addEventListener("click", closeFireConfirm);
    $("fire-confirm-yes").addEventListener("click", confirmFire);
  }

  document.addEventListener("DOMContentLoaded", init);

  // Exposed for tests / debugging in a browser console. Names functions that
  // exist: the previous version exported classifyRecord, which had been removed
  // by the refactor above, and referencing it here was a ReferenceError that
  // broke the page on every load.
  window.__testingHQBlast = {
    summarizeRun: R.summarizeRun,
    buildRows: R.buildRows,
  };
})();
