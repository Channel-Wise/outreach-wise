// AI personalization pages: playbook editor (field rows, view tabs, test run) and review shortcuts.
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  // --- playbook editor ---------------------------------------------------------------------
  const form = $("#pb-form");
  if (form) {
    const rows = $("#field-rows");
    $("#add-field")?.addEventListener("click", () => {
      rows.append($("#field-template").content.cloneNode(true));
      $$(".field-row:last-child input", rows)[0]?.focus();
    });
    rows?.addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove-field]");
      if (button && $$(".field-row:not(.head)", rows).length > 1) button.closest(".field-row").remove();
    });

    const mode = $("#pb-mode");
    $$(".tabs [data-view]").forEach((tab) => tab.addEventListener("click", (event) => {
      event.preventDefault();
      if (tab.dataset.view === "form" && !$("#view-form").children.length) return;
      mode.value = tab.dataset.view;
      $("#view-form").hidden = tab.dataset.view !== "form";
      $("#view-raw").hidden = tab.dataset.view !== "raw";
      $$(".tabs [data-view]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    }));

    const run = $("#run-test");
    run?.addEventListener("click", async () => {
      const status = $("#test-status");
      const output = $("#test-output");
      run.disabled = true;
      status.textContent = "Researching and writing… (up to a minute)";
      output.innerHTML = "";
      try {
        const response = await fetch(run.dataset.url, { method: "POST", body: new FormData(form) });
        const data = await response.json();
        status.textContent = data.cost_usd ? `≈ $${data.cost_usd.toFixed(3)} · ${data.model}` : "";
        output.innerHTML = renderOutcome(data);
      } catch (error) {
        status.textContent = "";
        output.innerHTML = `<div class="out note"><div class="k">error</div>${esc(error)}</div>`;
      } finally {
        run.disabled = false;
      }
    });
  }

  function renderOutcome(data) {
    if (data.status === "error" || data.status === "failed" || data.status === "retry") {
      return `<div class="out note"><div class="k">couldn't run</div>${esc(data.error)}</div>`;
    }
    let html = "";
    if (data.status === "skipped") {
      html += `<div class="out note"><div class="k">skipped</div>${esc(data.skip_reason)}</div>`;
    }
    for (const [name, value] of Object.entries(data.vals || {})) {
      const alts = (data.alternatives || {})[name] || [];
      html += `<div class="out"><div class="k">ai_${esc(name)}</div>${esc(value) || '<span class="muted">(empty)</span>'}`;
      if (alts.length) html += `<div class="ev">Options: ${alts.map(esc).join(" · ")}</div>`;
      html += "</div>";
    }
    if (data.evidence?.length) {
      html += `<div class="out"><div class="k">evidence</div>${data.evidence.map((e) => `<div class="ev">${esc(e.claim)}: ${esc(e.source)}</div>`).join("")}</div>`;
    }
    if (data.notes) html += `<div class="out note"><div class="k">notes for reviewer</div>${esc(data.notes)}</div>`;
    if (data.checks?.length) {
      html += `<ul class="check-list">${data.checks.map((c) => `<li class="${c.ok ? "" : "bad"}">${esc(c.text)}</li>`).join("")}</ul>`;
    }
    return html;
  }

  // --- review ------------------------------------------------------------------------------
  const review = $("#review-form");
  if (review) {
    $$("[data-alt]").forEach((button) => button.addEventListener("click", () => {
      const target = $(`#val_${button.dataset.field}`);
      target.value = button.dataset.alt;
      target.focus();
    }));
    document.addEventListener("keydown", (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.target.closest("input, textarea, select")) return;
      const key = event.key.toLowerCase();
      const button = $(`[data-key="${key}"]`);
      if (button) {
        event.preventDefault();
        button.click();
      }
    });
  }
})();
