/* Campaign page: tabs, the sequence canvas (pan/zoom flow editor) and the lead picker. No build step. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null && c !== false) node.append(c);
    return node;
  };
  const pct = (x) => `${(x * 100).toFixed(1)}%`;
  const VAR = (name) => "{" + "{" + name + "}" + "}";

  /* ------------------------------------------------------------------ tabs */

  const TABS = ["sequence", "leads", "results", "settings"];
  function showTab() {
    const name = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "sequence";
    $$(".tab-panel").forEach((p) => (p.hidden = p.id !== `panel-${name}`));
    $$(".tabs a").forEach((a) => a.setAttribute("aria-selected", a.getAttribute("href") === `#${name}`));
    if (name === "sequence" && flow) flow.fitIfNeeded(); // layout is read synchronously; rAF never fires in background tabs
  }
  window.addEventListener("hashchange", showTab);

  /* ------------------------------------------------------------------ server calls */

  const data = JSON.parse($("#flow-data").textContent);
  const csrf = data.csrf;

  async function post(url, fields = {}) {
    const body = new FormData();
    body.append("csrf", csrf);
    for (const [k, v] of Object.entries(fields)) body.append(k, v);
    const res = await fetch(url, { method: "POST", body, headers: { "X-Requested-With": "fetch" } });
    let json = null;
    try { json = await res.json(); } catch { /* not JSON */ }
    if (!res.ok || !json || json.ok === false) throw new Error(json?.error || `Request failed (${res.status})`);
    return json;
  }

  /* ------------------------------------------------------------------ canvas */

  const root = $("#flow");
  const flow = root ? createFlow(root) : null;

  function createFlow(root) {
    const viewport = $(".flow-viewport", root);
    const world = $(".flow-world", root);
    const svg = $(".flow-edges", root);
    const drawer = $(".drawer", root);
    const toast = $(".toast", root);
    const live = root.dataset.live === "1";

    let steps = data.steps;
    let stats = data.stats;
    let templates = data.templates;
    let selected = null; // variant id
    let dirty = false;
    let view = { x: 40, y: 0, k: 1 };
    let fitted = false;

    const NODE_W = 280, SMALL_W = 196, GAP = 110;

    function stepName(step) {
      return step.position === 1 ? "First email" : `Follow-up ${step.position - 1}`;
    }
    function findVariant(id) {
      for (const step of steps) for (const v of step.variants) if (v.id === id) return { step, variant: v };
      return null;
    }

    /* ---- toast */
    let toastTimer;
    function say(message, kind = "good") {
      toast.textContent = message;
      toast.className = `toast ${kind}`;
      toast.hidden = false;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => (toast.hidden = true), 2600);
    }
    async function run(promise, okMessage) {
      try {
        const res = await promise;
        if (res.steps) steps = res.steps;
        if (res.stats) stats = res.stats;
        if (res.templates) templates = res.templates;
        render();
        if (okMessage !== null) say(okMessage || res.message || "Saved.");
        return res;
      } catch (err) {
        say(err.message, "bad");
        return null;
      }
    }

    /* ---- rendering */
    function render() {
      $$(".node, .node-group", world).forEach((n) => n.remove());
      let x = 0;
      const place = (node, width) => {
        node.style.left = `${x}px`;
        node.style.width = `${width}px`;
        world.append(node);
        x += width + GAP;
        return node;
      };

      const trigger = place(
        el("button", { type: "button", class: "node node-trigger", onclick: () => (location.hash = "#leads") },
          el("div", { class: "node-kicker" }, icon("leads"), "Start"),
          el("div", { class: "node-title", text: "Leads enrolled" }),
          stats.enrolled
            ? el("div", { class: "node-live" },
                el("span", {}, el("b", { text: stats.enrolled }), " enrolled"),
                el("span", {}, el("b", { text: stats.active }), " in progress"))
            : el("div", { class: "node-sub", text: "No leads yet — click to add" }),
          steps[0]?.queued ? el("div", { style: "margin-top:6px" }, el("span", { class: "queued", text: `${steps[0].queued} waiting for email 1` })) : null),
        SMALL_W);
      const groups = steps.map((step, i) => place(stepGroup(step, i), NODE_W));
      const add = place(
        el("button", { type: "button", class: "node node-add", title: "Add a follow-up email", onclick: addStep },
          icon("plus"), el("span", { text: "Add follow-up" })),
        SMALL_W - 40);
      const end = place(
        el("div", { class: "node node-end" },
          el("div", { class: "node-kicker" }, icon("flag"), "End"),
          stats.enrolled
            ? el("div", { class: "node-live" },
                el("span", {}, el("b", { text: stats.replied }), " replied"),
                el("span", {}, el("b", { text: stats.completed }), " finished"),
                el("span", {}, el("b", { text: stats.bounced + stats.unsubscribed }), " bounced/unsub."),
                el("span", {}, el("b", { text: stats.stopped }), " stopped"))
            : el("div", { class: "node-sub", text: "Sequence ends. Stops early on reply, bounce or unsubscribe." })),
        SMALL_W);

      for (const n of [trigger, ...groups, add, end]) n.style.top = `${-n.offsetHeight / 2}px`;
      drawEdges(trigger, add, end);
      if (selected && !findVariant(selected)) closeDrawer();
      $$(".node-email", world).forEach((n) => n.classList.toggle("selected", Number(n.dataset.id) === selected));
    }

    function icon(name) {
      const tpl = document.getElementById(`icon-${name}`);
      if (tpl) return tpl.content.firstElementChild.cloneNode(true);
      return el("span", { class: "dot" });
    }

    function waitBar(step) {
      const input = el("input", { type: "number", min: 0, max: 365, value: step.delay_days, "aria-label": "Days to wait" });
      input.addEventListener("change", () => {
        const days = Math.max(0, Math.min(365, parseInt(input.value || "0", 10)));
        run(post(`/steps/${step.id}`, { delay_days: days }), `Wait set to ${days} day${days === 1 ? "" : "s"}.`);
      });
      return el("label", { class: "wait-bar" },
        icon("clock"), "Wait", input, step.delay_days === 1 ? "day" : "days",
        el("span", { class: "muted", text: "without a reply" }),
        step.queued ? el("span", { class: "queued", text: `${step.queued} waiting` }) : null);
    }

    // Follow-ups a lead who got previous variant `parentId` can receive (mirrors campaigns.targets).
    // parentId null = the first email.
    function targets(step, parentId) {
      const active = step.variants.filter((v) => v.active);
      if (parentId == null) return active;
      const linked = active.filter((v) => v.parents.includes(parentId));
      return linked.length ? linked : active.filter((v) => !v.parents.length);
    }
    // [{variant, weight, pct}] for leads coming from parentId (mirrors campaigns.split).
    function split(step, parentId) {
      const rows = targets(step, parentId).map((v) => ({
        variant: v,
        weight: parentId != null && v.parents.includes(parentId) ? v.link_weights[String(parentId)] : v.weight,
      }));
      const pcts = percents(rows.map((r) => r.weight));
      rows.forEach((r, i) => (r.pct = pcts[i]));
      return rows;
    }
    // Whole percentages summing to 100 (largest remainder), like campaigns._percents.
    function percents(weights) {
      const total = weights.reduce((a, b) => a + b, 0) || weights.length;
      const w = weights.reduce((a, b) => a + b, 0) ? weights : weights.map(() => 1);
      const raw = w.map((x) => (100 * x) / total);
      const out = raw.map(Math.floor);
      const order = raw.map((r, i) => [r - out[i], i]).sort((a, b) => b[0] - a[0]);
      for (let k = 0; k < 100 - out.reduce((a, b) => a + b, 0); k++) out[order[k][1]] += 1;
      return out;
    }
    const isMulti = (variantId) => findVariant(variantId)?.step.variants.length > 1;
    const nameOf = (v) => (isMulti(v.id) ? `variant ${v.label}` : "this email");

    function stepGroup(step, index) {
      const active = step.variants.filter((v) => v.active).length;
      const ab = step.variants.length > 1;
      const linked = step.variants.some((v) => v.parents.length);
      const group = el("div", { class: "node-group", id: `step-${step.id}`, "data-step": step.id },
        index > 0 ? waitBar(step) : null,
        el("div", { class: "group-head" },
          el("span", { class: "step-num", text: step.position }),
          el("strong", { text: stepName(step) }),
          linked ? el("span", { class: "badge accent plain", text: "per-variant" })
            : ab ? el("span", { class: "badge accent plain", text: `A/B · ${active} active` }) : null,
          el("span", { class: "spacer" }),
          steps.length > 1 ? el("button", {
            type: "button", class: "icon-btn danger-text", "aria-label": `Delete ${stepName(step)}`,
            title: step.locked ? "Already sent to leads — can't be deleted" : `Delete ${stepName(step)} and all its variants`,
            disabled: step.locked, onclick: () => deleteStep(step),
          }, icon("trash")) : null));
      // Order follow-ups like their first parent so connecting lines cross as little as possible.
      const prev = steps[index - 1];
      const rank = (v) => (v.parents.length ? Math.min(...v.parents.map((id) => prev.variants.findIndex((p) => p.id === id))) : 99);
      const ordered = [...step.variants].sort((a, b) => (prev ? rank(a) - rank(b) : 0) || a.label.localeCompare(b.label));
      for (const v of ordered) group.append(emailNode(step, index, v, active));
      group.append(el("button", { type: "button", class: "group-add", onclick: () => addVariant(step) }, icon("plus"), ab ? "Add variant" : "A/B test this email"));
      if (step.uncovered.length) {
        group.append(el("div", { class: "group-warn", text: `Leads who got ${step.uncovered.join(", ")} end here. Drag their ● onto an email in this column (or onto the column) to give them a follow-up.` }));
      }
      for (const v of step.verdicts) {
        group.append(el("div", { class: "group-verdict", text: (v.after_label ? `After ${v.after_label}: ` : "") + v.verdict }));
      }
      return group;
    }

    function emailNode(step, index, v, activeCount) {
      const threaded = !v.subject.trim() && step.position > 1;
      const preview = v.body.replace(/\s+/g, " ").trim();
      const multi = step.variants.length > 1;
      const showStats = v.sent > 0 || stats.sent > 0;
      const hasNext = index < steps.length - 1;
      const linked = step.variants.some((x) => x.parents.length);
      // First email: its share of new leads. Follow-ups: the split is shown on each incoming line.
      const firstSplit = index === 0 && activeCount > 1 ? split(step, null).find((r) => r.variant.id === v.id) : null;
      const share = firstSplit ? `${firstSplit.pct}%` : "";
      // Delete in place: the variant, or the whole step if it's the step's only email.
      const only = step.variants.length === 1;
      const canDelete = only ? steps.length > 1 && !step.locked : !v.locked;
      return el("button", {
        type: "button",
        class: `node node-email${v.active ? "" : " inactive"}`,
        "data-id": v.id,
        onclick: () => openDrawer(v.id),
      },
        index > 0 ? el("span", { class: "port-handle in", "aria-hidden": "true" }) : null,
        canDelete ? el("span", {
          class: "card-delete", role: "button", tabindex: "0",
          title: only ? `Delete ${stepName(step)}` : `Delete variant ${v.label}`,
          "aria-label": only ? `Delete ${stepName(step)}` : `Delete variant ${v.label}`,
          onclick: (e) => { e.stopPropagation(); only ? deleteStep(step) : deleteVariant(step, v); },
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); only ? deleteStep(step) : deleteVariant(step, v); } },
        }, icon("trash")) : null,
        hasNext ? el("span", { class: `port-handle out v-${v.label}`, title: `Drag onto a follow-up to connect ${multi ? `variant ${v.label}` : "this email"} to it` }) : null,
        el("div", { class: "email-head" },
          multi ? el("span", { class: `variant-pill v-${v.label}`, text: v.label }) : el("span", { class: "node-kicker" }, icon("mail"), "Email"),
          v.parent_labels.map((label) => el("span", { class: `after-pill v-${label}`, text: `after ${label}` })),
          !v.parents.length && linked ? el("span", { class: "badge plain", text: "everyone else" }) : null,
          !v.active ? el("span", { class: "muted small", text: "inactive" }) : share ? el("span", { class: "muted small", text: share }) : null,
          v.winner ? el("span", { class: "badge good", text: "winner" }) : null),
        v.unreached ? el("div", { class: "unreached", text: "Nobody gets this email — every previous variant has its own follow-up. Drag a ● here to connect one." }) : null,
        el("div", { class: `email-subject${threaded ? " muted" : ""}`, text: threaded ? "Re: same thread" : v.subject || "No subject yet" }),
        el("div", { class: "email-body", text: preview || "Empty email" }),
        showStats ? el("div", { class: "email-stats" },
          el("span", { text: `${v.sent} ${live ? "sent" : "dry-run"}` }),
          el("span", { text: `${v.replies} repl${v.replies === 1 ? "y" : "ies"}` }),
          el("strong", { text: pct(v.rate) })) : null);
    }

    /* ---- edges: who actually flows where */
    const card = (id) => $(`.node-email[data-id="${id}"]`, world);
    function anchor(n, side) {
      let x = n.offsetLeft, y = n.offsetTop;
      for (let p = n.offsetParent; p && p !== world; p = p.offsetParent) { x += p.offsetLeft; y += p.offsetTop; }
      return { x: side === "out" ? x + n.offsetWidth : x, y: y + n.offsetHeight / 2 };
    }
    function curve(p, q) {
      const dx = Math.max(40, Math.abs(q.x - p.x) / 2);
      return `M${p.x} ${p.y} C${p.x + dx} ${p.y}, ${q.x - dx} ${q.y}, ${q.x} ${q.y}`;
    }
    function drawEdges(trigger, add, end) {
      let minX = 0, maxX = 0, minY = 0, maxY = 0;
      for (const n of world.children) {
        if (n === svg) continue;
        minX = Math.min(minX, n.offsetLeft); maxX = Math.max(maxX, n.offsetLeft + n.offsetWidth);
        minY = Math.min(minY, n.offsetTop); maxY = Math.max(maxY, n.offsetTop + n.offsetHeight);
      }
      svg.setAttribute("viewBox", `${minX - 20} ${minY - 20} ${maxX - minX + 40} ${maxY - minY + 40}`);
      Object.assign(svg.style, { left: `${minX - 20}px`, top: `${minY - 20}px`, width: `${maxX - minX + 40}px`, height: `${maxY - minY + 40}px` });
      world.dataset.bounds = JSON.stringify({ minX, maxX, minY, maxY });

      const paths = [], labels = [];
      const line = (a, b, cls, { child, parent, label, step: splitStep } = {}) => {
        if (!a || !b) return;
        const p = anchor(a, "out"), q = anchor(b, "in");
        const d = curve(p, q);
        if (child) paths.push(`<path class="edge-hit" data-child="${child}" data-parent="${parent}" d="${d}"><title>Click to disconnect</title></path>`);
        paths.push(`<path class="edge ${cls}" d="${d}"/>`);
        // Split label on the curve itself (t = 0.55), where lines from one email have fanned apart.
        if (label) {
          const t = 0.55, u = 1 - t, dx = Math.max(40, Math.abs(q.x - p.x) / 2);
          const x = u ** 3 * p.x + 3 * u * u * t * (p.x + dx) + 3 * u * t * t * (q.x - dx) + t ** 3 * q.x;
          const y = u ** 3 * p.y + 3 * u * u * t * p.y + 3 * u * t * t * q.y + t ** 3 * q.y;
          labels.push(`<g class="edge-label" role="button" tabindex="0" data-step="${splitStep}" data-parent="${parent ?? ""}"><title>Click to change the split</title><rect x="${x - 21}" y="${y - 10}" width="42" height="20" rx="10"/><text x="${x}" y="${y + 1}">${label}</text></g>`);
        }
      };
      steps.forEach((step, i) => {
        if (i === 0) {
          const rows = split(step, null);
          for (const v of step.variants) {
            const row = rows.find((r) => r.variant.id === v.id);
            line(trigger, card(v.id), v.active ? "" : " off", { label: row && rows.length > 1 ? `${row.pct}%` : "", step: step.id });
          }
          return;
        }
        // One line per (previous variant -> follow-up it can lead to); split shown when there's a choice.
        for (const p of steps[i - 1].variants) {
          const rows = split(step, p.id);
          for (const { variant: v, pct: share } of rows) {
            const explicit = v.parents.includes(p.id);
            const cls = `${explicit ? `branch v-${p.label}` : ""}${p.active ? "" : " off"}${share === 0 ? " zero" : ""}`;
            line(card(p.id), card(v.id), cls, {
              child: explicit ? v.id : null,
              parent: p.id,
              label: p.active && rows.length > 1 ? `${share}%` : "",
              step: step.id,
            });
          }
          // Inactive follow-ups still linked to this variant: show the link, greyed out.
          for (const v of step.variants) {
            if (!v.active && v.parents.includes(p.id)) line(card(p.id), card(v.id), `branch v-${p.label} off`, { child: v.id, parent: p.id });
          }
        }
      });
      for (const v of steps[steps.length - 1]?.variants ?? []) line(card(v.id), add, "dashed");
      line(add, end, "dashed");
      svg.innerHTML = paths.join("") + labels.join("");
    }

    /* ---- drag a ● onto a follow-up to connect */
    let link = null, swallowClick = false;
    const toWorld = (e) => {
      const r = viewport.getBoundingClientRect();
      return { x: (e.clientX - r.left - view.x) / view.k, y: (e.clientY - r.top - view.y) / view.k };
    };
    world.addEventListener("pointerdown", (e) => {
      const handle = e.target.closest(".port-handle.out");
      if (!handle || e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      const from = handle.closest(".node-email");
      const found = findVariant(Number(from.dataset.id));
      const next = steps[steps.indexOf(found.step) + 1];
      const zone = $(`.node-group[data-step="${next.id}"]`, world);
      const addButton = $(".group-add", zone);
      link = { parent: found.variant, next, zone, addButton, addText: addButton.lastChild.textContent, start: anchor(from, "out"), drop: null };
      zone.classList.add("drop-zone");
      root.classList.add("linking");
      svg.insertAdjacentHTML("beforeend", `<path class="edge branch link-preview v-${found.variant.label}" d=""/>`);
    });
    // What is under the pointer: a follow-up card (connect) or the follow-up column (create one).
    function dropAt(x, y) {
      const under = document.elementFromPoint(x, y);
      if (under?.closest(".node-group") !== link.zone) return null;
      const target = under.closest(".node-email");
      return target ? { variant: Number(target.dataset.id), el: target } : { create: true };
    }
    window.addEventListener("pointermove", (e) => {
      if (!link) return;
      $(".link-preview", svg).setAttribute("d", curve(link.start, toWorld(e)));
      $$(".drop-target", world).forEach((n) => n.classList.remove("drop-target"));
      link.drop = dropAt(e.clientX, e.clientY);
      link.drop?.el?.classList.add("drop-target");
      link.zone.classList.toggle("drop-new", !!link.drop?.create);
      link.addButton.lastChild.textContent = link.drop?.create ? `Drop: new follow-up for ${nameOf(link.parent)}` : link.addText;
    });
    async function endLink(e) {
      if (!link) return;
      // Decide on the release position; a cancelled pointer falls back to whatever was last hovered.
      const drop = e.type === "pointerup" ? dropAt(e.clientX, e.clientY) : link.drop;
      const { parent, next, zone, addButton, addText } = link;
      link = null;
      swallowClick = true;
      setTimeout(() => (swallowClick = false), 0);
      $(".link-preview", svg)?.remove();
      zone.classList.remove("drop-zone", "drop-new");
      root.classList.remove("linking");
      addButton.lastChild.textContent = addText;
      $$(".drop-target", world).forEach((n) => n.classList.remove("drop-target"));
      if (!drop || !(await confirmDiscard())) return;
      const who = nameOf(parent);
      if (drop.create) {
        const before = new Set(next.variants.map((v) => v.id));
        const res = await run(post(`/steps/${next.id}/variants`, { parent_id: parent.id }), `New follow-up for leads who got ${who} — write it on the right.`);
        const fresh = res && steps.find((s) => s.id === next.id)?.variants.find((v) => !before.has(v.id));
        if (fresh) { setDirty(false); openDrawer(fresh.id); }
        return;
      }
      const child = next.variants.find((v) => v.id === drop.variant);
      if (child.parents.includes(parent.id)) return say("Already connected.", "info");
      const res = await run(post(`/variants/${child.id}/links`, { parent_id: parent.id, action: "connect" }), null);
      if (!res) return;
      const fresh = steps.find((s) => s.id === next.id);
      const split = targets(fresh, parent.id).length;
      say(split > 1
        ? `Connected. Leads who got ${who} are now split ${Math.round(100 / split)}% across ${split} follow-ups (an A/B test).`
        : `Connected: leads who got ${who} get ${stepName(next)} ${child.label}.`);
    }
    window.addEventListener("pointerup", endLink);
    window.addEventListener("pointercancel", endLink);
    // The pointerup of a drag ends on the card's button; don't let it open the editor.
    world.addEventListener("click", (e) => { if (swallowClick) { e.stopPropagation(); e.preventDefault(); } }, true);

    // Click a coloured line to remove just that connection.
    svg.addEventListener("click", async (e) => {
      const label = e.target.closest(".edge-label");
      if (label) return openSplit(Number(label.dataset.step), label.dataset.parent ? Number(label.dataset.parent) : null, e.clientX, e.clientY);
      const hit = e.target.closest(".edge-hit");
      if (!hit) return;
      const child = findVariant(Number(hit.dataset.child));
      const parent = findVariant(Number(hit.dataset.parent));
      if (!child || !parent) return;
      const rest = child.variant.parents.length - 1;
      const after = rest ? `It stays connected to ${rest} other variant${rest === 1 ? "" : "s"}.` : "With no connections left it goes to everyone who doesn't have a follow-up of their own.";
      const ok = await ask(`Disconnect ${nameOf(parent.variant)} from ${stepName(child.step)} ${child.variant.label}? ${after}`, { ok: "Disconnect" });
      if (ok) await run(post(`/variants/${child.variant.id}/links`, { parent_id: parent.variant.id, action: "disconnect" }), "Disconnected.");
    });

    /* ---- pan & zoom */
    function apply() {
      world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.k})`;
      viewport.style.backgroundPosition = `${view.x}px ${view.y}px`;
      viewport.style.backgroundSize = `${20 * view.k}px ${20 * view.k}px`;
    }
    function zoomAt(factor, cx, cy) {
      const k = Math.min(1.6, Math.max(0.35, view.k * factor));
      view.x = cx - ((cx - view.x) * k) / view.k;
      view.y = cy - ((cy - view.y) * k) / view.k;
      view.k = k;
      apply();
    }
    function fit() {
      const b = JSON.parse(world.dataset.bounds || "null");
      if (!b || !viewport.clientWidth) return;
      const reserved = drawer.hidden ? 0 : Math.min(drawer.offsetWidth, viewport.clientWidth * 0.6);
      const w = viewport.clientWidth - reserved, h = viewport.clientHeight;
      // Fit when it can be read; otherwise keep a readable zoom and start at the left (pan for the rest).
      const k = Math.min(1, (w - 80) / (b.maxX - b.minX), (h - 80) / (b.maxY - b.minY));
      view.k = Math.max(0.7, k);
      const width = (b.maxX - b.minX) * view.k;
      view.x = (width + 80 > w ? 40 : (w - width) / 2) - b.minX * view.k;
      view.y = h / 2 - ((b.minY + b.maxY) / 2) * view.k;
      apply();
      fitted = true;
      revealSelected();
    }
    function scrollIntoView(node) {
      // Centre the node in the part of the canvas the drawer doesn't cover, if it isn't fully visible.
      const r = node.getBoundingClientRect(), v = viewport.getBoundingClientRect();
      const left = v.left + 16, right = (drawer.hidden ? v.right : drawer.getBoundingClientRect().left) - 16;
      if (r.left >= left && r.right <= right) return;
      view.x += (left + right) / 2 - (r.left + r.right) / 2;
      apply();
    }
    function revealSelected() {
      const node = selected && $(`.node-email[data-id="${selected}"]`, world);
      if (node && !drawer.hidden) scrollIntoView(node);
    }

    let drag = null;
    viewport.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest(".node, .node-group button, input, .edge-hit, .edge-label")) return;
      drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
      viewport.setPointerCapture(e.pointerId);
      viewport.classList.add("panning");
    });
    viewport.addEventListener("pointermove", (e) => {
      if (!drag) return;
      view.x = drag.vx + e.clientX - drag.x;
      view.y = drag.vy + e.clientY - drag.y;
      apply();
    });
    const endDrag = () => { drag = null; viewport.classList.remove("panning"); };
    viewport.addEventListener("pointerup", endDrag);
    viewport.addEventListener("pointercancel", endDrag);
    viewport.addEventListener("wheel", (e) => {
      const r = viewport.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        zoomAt(Math.exp(-e.deltaY * 0.01), e.clientX - r.left, e.clientY - r.top);
      } else if (root.classList.contains("full") || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault();
        view.x -= e.deltaX; view.y -= e.deltaY; apply();
      }
    }, { passive: false });

    $$("[data-zoom]", root).forEach((b) => b.addEventListener("click", () => {
      const mode = b.dataset.zoom;
      const cx = viewport.clientWidth / 2, cy = viewport.clientHeight / 2;
      if (mode === "in") zoomAt(1.2, cx, cy);
      else if (mode === "out") zoomAt(1 / 1.2, cx, cy);
      else if (mode === "fit") fit();
      else if (mode === "full") { root.classList.toggle("full"); document.body.classList.toggle("no-scroll"); fit(); }
    }));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && root.classList.contains("full") && drawer.hidden) {
        root.classList.remove("full"); document.body.classList.remove("no-scroll"); fit();
      }
    });

    /* ---- actions */
    async function addStep() {
      if (!(await confirmDiscard())) return;
      const res = await run(post(root.dataset.addStepUrl), "Follow-up added.");
      if (res) {
        const step = steps[steps.length - 1];
        openDrawer(step.variants[0].id);
      }
    }
    async function addVariant(step) {
      if (!(await confirmDiscard())) return;
      const before = new Set(step.variants.map((v) => v.id));
      const res = await run(post(`/steps/${step.id}/variants`), "Variant added — change something to test it.");
      if (res) {
        const fresh = steps.find((s) => s.id === step.id)?.variants.find((v) => !before.has(v.id));
        if (fresh) openDrawer(fresh.id);
      }
    }

    // Delete straight from the canvas. Editing the deleted email? Its unsaved changes go too.
    async function deleteVariant(step, v) {
      const what = `variant ${v.label} of ${stepName(step)}`;
      const links = v.parents.length ? ` Its connections to ${v.parent_labels.join(", ")} are removed too.` : "";
      if (!(await ask(`Delete ${what}?${links}`, { ok: "Delete", danger: true }))) return;
      if (selected === v.id) { setDirty(false); drawer.hidden = true; selected = null; }
      await run(post(`/variants/${v.id}/delete`), `Deleted ${what}.`);
    }
    async function deleteStep(step) {
      if (!(await ask(`Delete “${stepName(step)}” and all its variants? Later follow-ups move up one place.`, { ok: "Delete", danger: true }))) return;
      if (step.variants.some((v) => v.id === selected)) { setDirty(false); drawer.hidden = true; selected = null; }
      await run(post(`/steps/${step.id}/delete`), `Deleted ${stepName(step)}.`);
    }

    /* ---- edit a split: click a % label */
    const popover = el("div", { class: "split-pop", hidden: true, role: "dialog", "aria-label": "Edit split" });
    root.append(popover);
    function closeSplit() { popover.hidden = true; popover.replaceChildren(); }
    function openSplit(stepId, parentId, clientX, clientY) {
      const step = steps.find((s) => s.id === stepId);
      const prev = steps[steps.indexOf(step) - 1];
      const parent = parentId ? prev?.variants.find((p) => p.id === parentId) : null;
      const rows = split(step, parentId);
      if (rows.length < 2) return;
      const shared = parent && rows.some((r) => !r.variant.parents.includes(parentId));
      const inputs = rows.map((r) => el("input", { type: "number", min: 0, max: 100, step: 1, value: r.pct, "aria-label": `Share for ${r.variant.label}` }));
      const total = el("strong");
      const save = el("button", { type: "button", class: "btn sm primary", text: "Save" });
      const refresh = () => {
        const sum = inputs.reduce((a, i) => a + (parseInt(i.value, 10) || 0), 0);
        total.textContent = `${sum}%`;
        total.className = sum === 100 ? "" : "bad-text";
        save.disabled = sum !== 100 || inputs.some((i) => !/^\d+$/.test(i.value) || +i.value > 100);
      };
      inputs.forEach((i) => i.addEventListener("input", refresh));
      save.addEventListener("click", async () => {
        const fields = { parent_id: parentId ?? "" };
        rows.forEach((r, k) => (fields[`share_${r.variant.id}`] = inputs[k].value));
        if (await run(post(`/steps/${step.id}/split`, fields), "Split saved.")) closeSplit();
      });
      popover.replaceChildren(
        el("div", { class: "split-title" },
          el("strong", { text: parent ? `Leads who got ${isMulti(parent.id) ? `variant ${parent.label}` : stepName(prev)}` : "New leads (first email)" }),
          el("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: closeSplit }, icon("close"))),
        el("div", { class: "muted small", text: `How they're split across ${stepName(step)}:` }),
        ...rows.map((r, k) => el("label", { class: "split-row" },
          el("span", { class: `variant-pill v-${r.variant.label}`, text: r.variant.label }),
          el("span", { class: "split-subject", text: r.variant.subject || r.variant.body.replace(/\s+/g, " ").slice(0, 40) }),
          inputs[k], el("span", { text: "%" }))),
        el("div", { class: "split-foot" },
          el("span", { class: "small" }, "Total ", total),
          el("span", { class: "spacer" }),
          el("button", { type: "button", class: "btn sm ghost", text: "Even", onclick: () => { percents(rows.map(() => 1)).forEach((p, k) => (inputs[k].value = p)); refresh(); } }),
          save),
        ...(shared ? [el("p", { class: "muted small", style: "margin:6px 0 0", text: "“Everyone else” emails share one split: this also applies to other emails that fall back to them." })] : []));
      refresh();
      popover.hidden = false;
      const r = root.getBoundingClientRect();
      const x = Math.min(clientX - r.left + 8, r.width - popover.offsetWidth - 8);
      const y = Math.min(clientY - r.top + 8, r.height - popover.offsetHeight - 8);
      Object.assign(popover.style, { left: `${Math.max(8, x)}px`, top: `${Math.max(8, y)}px` });
      inputs[0].focus();
      inputs[0].select();
    }
    popover.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); closeSplit(); }
      if (e.key === "Enter") { e.preventDefault(); $(".btn.primary", popover)?.click(); }
    });
    viewport.addEventListener("pointerdown", () => { if (!popover.hidden) closeSplit(); });

    /* ---- drawer */
    const d = (name) => $(`[data-d="${name}"]`, drawer);
    const fields = { subject: d("subject"), body: d("body"), active: d("active") };
    let lastFocus = fields.body;
    [fields.subject, fields.body].forEach((f) => {
      f.addEventListener("focus", () => (lastFocus = f));
      f.addEventListener("input", () => setDirty(true));
    });
    fields.active.addEventListener("change", () => setDirty(true));

    function setDirty(value) {
      dirty = value;
      d("status").textContent = value ? "Unsaved changes" : "";
      d("save").disabled = !value;
    }
    function confirmDiscard() {
      return !dirty || ask("Discard unsaved changes to this email?", { ok: "Discard" });
    }
    function showView(name) {
      $$("[data-view]", drawer).forEach((v) => (v.hidden = v.dataset.view !== name));
      $$("[data-view-foot]", drawer).forEach((v) => (v.hidden = v.dataset.viewFoot !== name));
    }

    async function openDrawer(id) {
      if (id !== selected && !(await confirmDiscard())) return;
      const found = findVariant(id);
      if (!found) return;
      const { step, variant } = found;
      selected = id;
      const ab = step.variants.length > 1;
      d("kicker").textContent = `Step ${step.position}${ab ? ` · Variant ${variant.label}` : ""}`;
      d("title").textContent = stepName(step);
      fields.subject.value = variant.subject;
      fields.body.value = variant.body;
      fields.active.checked = !!variant.active;
      fields.subject.required = step.position === 1;
      fields.subject.placeholder = step.position === 1 ? "Subject line" : "Leave empty to reply in the same thread";
      d("subject-hint").textContent = step.position === 1
        ? "Required for the first email."
        : "Empty = sent as a reply (“Re: …”) in the same thread, which usually gets more replies.";
      d("active-wrap").hidden = !ab;
      d("delete-variant").hidden = !ab;
      d("delete-variant").disabled = variant.locked;
      d("delete-variant").title = variant.locked ? "Already sent — deactivate it instead to keep its results." : "";
      d("delete-step").hidden = steps.length < 2;
      d("delete-step").disabled = step.locked;
      d("delete-step").title = step.locked ? "Already sent to leads; it can't be deleted." : "";
      d("add-variant").disabled = step.variants.length >= 8;

      // Who gets this email (connections are made by dragging on the canvas).
      const prev = steps[steps.indexOf(step) - 1];
      d("branch-info").hidden = !prev;
      d("branch-info").textContent = variant.parent_labels.length
        ? `Sent to leads who got ${stepName(prev)} · variant ${variant.parent_labels.join(", ")}. Click a coloured line on the canvas to disconnect one.`
        : step.variants.some((v) => v.parents.length)
          ? "Sent to everyone whose previous email has no follow-up connected to it."
          : "To send a different follow-up per variant, drag the ● on the right of an email onto a follow-up.";
      showView("edit");
      setDirty(false);
      drawer.hidden = false;
      $$(".node-email", world).forEach((n) => n.classList.toggle("selected", Number(n.dataset.id) === id));
      revealSelected();
    }
    async function closeDrawer() {
      if (!(await confirmDiscard())) return;
      drawer.hidden = true;
      selected = null;
      setDirty(false);
      $$(".node-email.selected", world).forEach((n) => n.classList.remove("selected"));
    }

    async function save() {
      if (!selected) return;
      if (fields.subject.required && !fields.subject.value.trim()) {
        say("The first email needs a subject.", "bad");
        fields.subject.focus();
        return;
      }
      const res = await run(post(`/variants/${selected}`, {
        subject: fields.subject.value,
        body: fields.body.value,
        active: fields.active.checked || d("active-wrap").hidden ? "on" : "",
      }), "Email saved.");
      if (res) setDirty(false);
    }

    d("close").addEventListener("click", closeDrawer);
    d("save").addEventListener("click", save);
    drawer.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); save(); }
      if (e.key === "Escape") { e.stopPropagation(); closeDrawer(); }
    });
    d("add-variant").addEventListener("click", () => {
      const found = findVariant(selected);
      if (found) addVariant(found.step);
    });
    d("delete-variant").addEventListener("click", () => {
      const found = findVariant(selected);
      if (found) deleteVariant(found.step, found.variant);
    });
    d("delete-step").addEventListener("click", () => {
      const found = findVariant(selected);
      if (found) deleteStep(found.step);
    });

    // Variable chips
    $$(".chip[data-var]", drawer).forEach((chip) => chip.addEventListener("click", () => {
      lastFocus.setRangeText(VAR(chip.dataset.var), lastFocus.selectionStart, lastFocus.selectionEnd, "end");
      lastFocus.focus();
      setDirty(true);
    }));

    // Template picker
    function renderTemplates() {
      const q = d("tpl-search").value.trim().toLowerCase();
      const found = findVariant(selected);
      const list = d("tpl-list");
      list.replaceChildren();
      const matches = templates.filter((t) => !q || `${t.name} ${t.subject} ${t.body}`.toLowerCase().includes(q));
      // Suggest the matching category first: first email vs follow-ups.
      const wanted = found?.step.position === 1 ? "First email" : "Follow-up";
      matches.sort((a, b) => (b.category === wanted) - (a.category === wanted));
      if (!matches.length) list.append(el("p", { class: "muted small", text: "No templates match." }));
      for (const t of matches) {
        list.append(el("div", { class: "tpl-item" },
          el("div", { class: "row between", style: "gap:6px" },
            el("strong", { text: t.name }),
            el("span", { class: "badge plain", text: t.category })),
          t.subject ? el("div", { class: "small secondary", text: `Subject: ${t.subject}` }) : null,
          el("pre", { class: "tpl-body small", text: t.body }),
          el("div", { class: "row", style: "gap:6px" },
            el("button", { type: "button", class: "btn sm primary", onclick: () => useTemplate(t, "replace") }, "Use this"),
            el("button", { type: "button", class: "btn sm ghost", onclick: () => useTemplate(t, "insert") }, "Insert body at cursor"))));
      }
    }
    async function useTemplate(t, how) {
      const found = findVariant(selected);
      if (how === "replace") {
        if ((fields.body.value.trim() || fields.subject.value.trim()) && !(await ask("Replace the current subject and body with this template?", { ok: "Replace" }))) return;
        // Keep follow-ups threaded unless the template defines its own subject.
        if (t.subject || found?.step.position === 1) fields.subject.value = t.subject;
        fields.body.value = t.body;
      } else {
        fields.body.setRangeText(t.body, fields.body.selectionStart, fields.body.selectionEnd, "end");
      }
      setDirty(true);
      showView("edit");
      fields.body.focus();
      say("Template applied — review and save.", "info");
    }
    d("use-template").addEventListener("click", () => { d("tpl-search").value = ""; renderTemplates(); showView("templates"); d("tpl-search").focus(); });
    d("tpl-search").addEventListener("input", renderTemplates);
    $$('[data-d="back"]', drawer).forEach((b) => b.addEventListener("click", () => showView("edit")));
    d("save-template").addEventListener("click", () => {
      const found = findVariant(selected);
      d("tpl-name").value = "";
      d("tpl-cat").value = found?.step.position === 1 ? "First email" : "Follow-up";
      showView("save-template");
      d("tpl-name").focus();
    });
    d("tpl-save").addEventListener("click", async () => {
      const res = await run(post(root.dataset.templateUrl, {
        name: d("tpl-name").value,
        category: d("tpl-cat").value,
        subject: fields.subject.value,
        body: fields.body.value,
      }), "Saved to your template library.");
      if (res) showView("edit");
    });

    window.addEventListener("beforeunload", (e) => { if (dirty) e.preventDefault(); });
    window.addEventListener("resize", () => fitted && fit());

    render();
    apply();

    return {
      fitIfNeeded() { render(); fit(); },
    };
  }

  /* ------------------------------------------------------------------ lead picker */

  const pickerData = JSON.parse($("#picker-data")?.textContent || "null");
  const pickerRoot = $("#picker");
  if (pickerData && pickerRoot) createPicker(pickerRoot, pickerData);

  function createPicker(root, { leads, columns, capped }) {
    const form = $("#enroll-form", root);
    const p = (name) => $(`[data-p="${name}"]`, root);
    const table = $(".picker-table table", root);
    const chosen = new Set();
    const MAX_ROWS = 300;
    const shown = columns.slice(0, 4);
    let how = "pick";
    let matches = [];
    const newCount = leads.filter((l) => l.status === "new").length;

    leads.forEach((l) => (l.search = `${l.email} ${Object.values(l.fields).join(" ")}`.toLowerCase()));

    table.tHead.replaceChildren(el("tr", {},
      el("th", { style: "width:36px" }, el("input", { type: "checkbox", "aria-label": "Select all shown", onchange: (e) => {
        for (const l of matches.slice(0, MAX_ROWS)) e.target.checked ? chosen.add(l.id) : chosen.delete(l.id);
        draw();
      } })),
      el("th", { text: "Email" }),
      shown.map((c) => el("th", { text: c.replace(/_/g, " ") })),
      el("th", { text: "Status" })));

    function filter() {
      const q = p("search").value.trim().toLowerCase();
      const contacted = p("contacted").checked;
      matches = leads.filter((l) => (contacted || l.status === "new") && (!q || l.search.includes(q)));
    }
    function draw() {
      const rows = matches.slice(0, MAX_ROWS).map((l) => {
        const box = el("input", { type: "checkbox", "aria-label": `Select ${l.email}` });
        box.checked = chosen.has(l.id);
        box.addEventListener("change", () => { box.checked ? chosen.add(l.id) : chosen.delete(l.id); summary(); tr.classList.toggle("picked", box.checked); });
        const tr = el("tr", { class: chosen.has(l.id) ? "picked" : "" },
          el("td", {}, box),
          el("td", { class: "truncate", text: l.email }),
          shown.map((c) => el("td", { class: "truncate", text: l.fields[c] ?? "" })),
          el("td", {}, el("span", { class: `badge ${l.status === "contacted" ? "info" : ""}`, text: l.status })));
        tr.addEventListener("click", (e) => { if (e.target !== box) { box.checked = !box.checked; box.dispatchEvent(new Event("change")); } });
        return tr;
      });
      table.tBodies[0].replaceChildren(...rows);
      if (!rows.length) table.tBodies[0].append(el("tr", {}, el("td", { colspan: shown.length + 3, class: "muted", text: leads.length ? "No leads match." : "No eligible leads. Sync your sheet on the Leads page." })));
      const allBox = $("thead input", table);
      const visible = matches.slice(0, MAX_ROWS);
      allBox.checked = visible.length > 0 && visible.every((l) => chosen.has(l.id));
      p("note").textContent = (matches.length > MAX_ROWS ? `Showing ${MAX_ROWS} of ${matches.length} matches — search to narrow down. “Select all matches” selects all ${matches.length}. ` : "")
        + (capped ? "Only the first 5,000 eligible leads are listed." : "");
      summary();
    }
    function summary() {
      let text, n;
      if (how === "pick") {
        n = chosen.size;
        text = n ? `${n} lead${n === 1 ? "" : "s"} selected` : "Select leads in the table";
      } else {
        const input = $(`[data-how-panel="${how}"] [data-p="n"]`, root);
        const pool = form.include_contacted.checked ? leads.length : newCount;
        n = Math.min(parseInt(input.value || "0", 10) || (how === "first" ? pool : 0), pool);
        text = how === "random" ? (n ? `${n} random lead${n === 1 ? "" : "s"} out of ${pool}` : "Enter how many") : `${n} of ${pool} leads, top of the sheet first`;
      }
      p("summary").textContent = text;
      p("submit").textContent = n ? `Enroll ${n} lead${n === 1 ? "" : "s"}` : "Enroll leads";
      p("submit").disabled = !n;
    }

    $$("[data-how]", root).forEach((b) => b.addEventListener("click", () => {
      how = b.dataset.how;
      form.how.value = how;
      $$("[data-how]", root).forEach((x) => x.classList.toggle("on", x === b));
      $$("[data-how-panel]", root).forEach((x) => (x.hidden = x.dataset.howPanel !== how));
      p("contacted-opt").hidden = how === "pick" || leads.length === newCount;
      summary();
    }));
    $$('[data-p="n"]', root).forEach((i) => i.addEventListener("input", summary));
    form.include_contacted.addEventListener("change", summary);
    p("search").addEventListener("input", () => { filter(); draw(); });
    p("contacted").addEventListener("change", () => { filter(); draw(); });
    p("contacted").closest("label").hidden = leads.length === newCount;
    p("all").addEventListener("click", () => { matches.forEach((l) => chosen.add(l.id)); draw(); });
    p("none").addEventListener("click", () => { chosen.clear(); draw(); });
    form.addEventListener("submit", (e) => {
      if (how === "pick") {
        form.lead_ids.value = [...chosen].join(",");
      } else {
        const input = $(`[data-how-panel="${how}"] [data-p="n"]`, root);
        let limit = form.querySelector('input[name="limit"]');
        if (!limit) { limit = el("input", { type: "hidden", name: "limit" }); form.append(limit); }
        limit.value = input.value;
        if (how === "random" && !input.value) { e.preventDefault(); input.focus(); }
      }
    });

    filter();
    draw();
  }

  /* ------------------------------------------------------------------ enrolled list (remove leads) */

  const enrolledForm = $("#enrolled");
  if (enrolledForm && $("[data-e=all]", enrolledForm)) {
    const boxes = () => $$('tbody input[name="lead_id"]', enrolledForm).filter((b) => !b.closest("tr").hidden);
    const removeBtn = $("[data-e=remove]", enrolledForm);
    const sync = () => {
      const n = $$('tbody input[name="lead_id"]:checked', enrolledForm).length;
      removeBtn.disabled = !n;
      removeBtn.textContent = n ? `Remove ${n} lead${n === 1 ? "" : "s"}` : "Remove selected";
    };
    $("[data-e=all]", enrolledForm).addEventListener("change", (e) => { boxes().forEach((b) => (b.checked = e.target.checked)); sync(); });
    enrolledForm.addEventListener("change", (e) => { if (e.target.name === "lead_id") sync(); });
    $("[data-e=search]", enrolledForm).addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      $$("tbody tr", enrolledForm).forEach((tr) => (tr.hidden = q && !tr.dataset.search.includes(q)));
    });
  }

  showTab();
})();
