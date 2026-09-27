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
// The `outcomeOf` fallback below only covers a response from a server older
// than the annotation. It is deliberately not a reimplementation: if it ever
// runs, the UI is visibly misconfigured and should not quietly agree with a
// stale server.

(function () {
  "use strict";

  const state = {
    targets: [],
    categories: [],
  };

  function $(id) {
    return document.getElementById(id);
  }

  function outcomeOf(record) {
    if (record.outcome) {
      return record.outcome;
    }
    return "unknown";
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

    const records = artifact.records || [];
    const cleanFailed = records.filter(
      (r) => outcomeOf(r) === "clean_failed"
    );
    const degenerateFailed = records.filter(
      (r) => outcomeOf(r) === "degenerate_failed"
    );

    $("count-clean-failed").textContent = cleanFailed.length;
    $("count-degenerate-failed").textContent = degenerateFailed.length;

    const byStatus = artifact.summary.by_status_class || {};
    renderMiniTable(
      $("status-class-table"),
      Object.keys(byStatus).map((k) => [k, byStatus[k]])
    );

    const byCategory = artifact.summary.by_category || {};
    renderMiniTable(
      $("category-table"),
      Object.keys(byCategory).map((k) => [k, byCategory[k]])
    );

    const flagsList = $("flags-list");
    flagsList.innerHTML = "";
    const flags = artifact.summary.flags || [];
    if (flags.length === 0) {
      const li = document.createElement("li");
      li.textContent = "No flags. Every clean payload 2xx'd; every degenerate payload was rejected cleanly.";
      flagsList.appendChild(li);
    } else {
      flags.forEach((flag) => {
        const li = document.createElement("li");
        li.textContent = flag;
        flagsList.appendChild(li);
      });
    }
  }

  function rowClassFor(outcome) {
    if (outcome === "clean_failed") return "row-clean-failed";
    if (outcome === "degenerate_failed") return "row-degenerate-failed";
    return "row-ok";
  }

  function badgeFor(outcome) {
    if (outcome === "ok") {
      return '<span class="badge badge-ok">pass</span>';
    }
    return '<span class="badge badge-fail">fail</span>';
  }

  // "Streaming" results table: append rows with a short stagger instead of
  // dumping the whole table at once, so a run reads as it arrives.
  function streamResults(records) {
    $("results-empty").classList.add("hidden");
    $("results-table-wrap").classList.remove("hidden");
    const tbody = $("results-tbody");
    tbody.innerHTML = "";

    const delayPerRow = records.length > 60 ? 0 : 12;

    records.forEach((record, i) => {
      window.setTimeout(() => {
        const outcome = outcomeOf(record);
        const tr = document.createElement("tr");
        tr.className = rowClassFor(outcome);
        const status = record.response ? record.response.status : null;
        const latency = record.response ? record.response.latency_ms : null;
        tr.innerHTML =
          "<td>" + record.id + "</td>" +
          "<td>" + record.category + "</td>" +
          "<td>" + (status === null || status === undefined ? "(timeout)" : status) + "</td>" +
          "<td>" + (latency === null || latency === undefined ? "-" : latency) + "</td>" +
          "<td>" + badgeFor(outcome) + "</td>";
        tbody.appendChild(tr);
      }, i * delayPerRow);
    });
  }

  function render(artifact) {
    renderSummary(artifact);
    streamResults(artifact.records || []);
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

  // Exposed for tests / debugging in a browser console.
  window.__testingHQBlast = { classifyRecord };
})();
