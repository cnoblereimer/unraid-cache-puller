"use strict";

// ---- helpers -------------------------------------------------------------

// Build DOM nodes. Strings become text nodes, so file names can never inject HTML.
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const $ = (sel) => document.querySelector(sel);

function bytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : n < 10 ? 2 : 1)} ${units[i]}`;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 0) return "in " + span(-s);
  if (s < 45) return "just now";
  return span(s) + " ago";
}
function until(ts) {
  const s = Math.round(ts - Date.now() / 1000);
  return s <= 0 ? "any moment" : "in " + span(s);
}
function span(s) {
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 129600) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "";

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(msg, bad) {
  const t = el("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 8000 : 4500);
}

const STATE_LABELS = {
  pool: ["On cache", "ok"],
  candidate: ["Will move", "info"],
  cold: ["On array", ""],
  blocked: ["Won't move", "warn"],
  missing: ["Gone", ""],
  unmanaged: ["Not managed", ""],
};

// ---- state -----------------------------------------------------------------

const state = {
  tab: "overview",
  overview: null,
  files: { q: "", hot: false, offset: 0, limit: 100 },
  settings: null,
  pending: {},
};

// ---- header ----------------------------------------------------------------

function renderHeader(o) {
  const badge = $("#mode-badge");
  const blocked = o.gates.find((g) => !g.ok);
  if (o.dry_run) { badge.className = "badge warn"; badge.textContent = "Dry run"; }
  else if (blocked) { badge.className = "badge bad"; badge.textContent = "Paused"; badge.title = blocked.detail; }
  else { badge.className = "badge ok"; badge.textContent = "Active"; badge.title = ""; }

  const c = o.cycle;
  const parts = [];
  if (c.running) parts.push("Running now…");
  else {
    parts.push(c.last_end ? `Last run ${ago(c.last_end)}` : "No run yet");
    parts.push(`next ${until(c.next_at)}`);
  }
  $("#run-info").textContent = parts.join(" · ");
  $("#run-now").disabled = c.running;
  $("#run-now").textContent = c.running ? "Running…" : "Run now";
}

// ---- overview ----------------------------------------------------------------

function renderOverview(o) {
  const root = $("#tab-overview");
  const nodes = [];

  if (o.dry_run) {
    nodes.push(el("div", { class: "banner" },
      el("div", {},
        el("strong", {}, "Dry run: nothing is being moved yet."),
        "Let it watch your shares for a few days, check the plan on the Files tab, then switch dry run off in Settings."),
      el("div", { class: "row" },
        el("button", { class: "btn", type: "button", onclick: () => showTab("files", { hot: true }) }, "Review plan"),
        el("button", { class: "btn primary", type: "button", onclick: () => showTab("settings") }, "Open settings"))));
  }
  const managed = o.shares.filter((s) => s.status === "managed");
  if (!managed.length) {
    nodes.push(el("div", { class: "banner bad" }, el("div", {},
      el("strong", {}, "No shares to manage."),
      "Only shares with a pool as primary storage and the array as secondary storage are managed. Check the share list below and the Shares settings.")));
  }

  // Safety + tracking + last run
  const gates = el("ul", { class: "gates" }, o.gates.map((g) => el("li", {},
    el("span", { class: "gate-icon " + (g.ok ? "ok" : "bad") }, g.ok ? "✓" : "✕"),
    el("div", {}, el("div", { class: "gate-label" }, g.label), el("div", { class: "gate-detail" }, g.detail)))));
  const allOk = o.gates.every((g) => g.ok);

  const t = o.tracker;
  const tracking = el("div", { class: "stats" },
    stat(o.counts.hot.toLocaleString(), `frequently used (score ≥ ${o.min_score})`),
    stat(o.counts.tracked.toLocaleString(), "files seen"),
    stat(o.counts.promoted.toLocaleString(), `moved to cache (${bytes(o.counts.promoted_bytes)})`),
    trackingStat(t));
  const trackerWarn = [];
  if (t.active && t.scanning) {
    const dirs = t.scan_progress.reduce((n, p) => n + p.dirs, 0);
    trackerWarn.push(el("p", { class: "hint" },
      `Scanning folders so accesses in them can be counted: ${dirs.toLocaleString()} so far on ${t.scan_progress.length} share folder(s). `,
      t.fanotify_available ? "" : "Give the container the SYS_ADMIN and DAC_READ_SEARCH capabilities to skip this scan entirely."));
  }
  if (t.active && t.mode === "inotify" && !t.scanning && !t.fanotify_available) {
    trackerWarn.push(el("p", { class: "hint" }, "Tip: with the SYS_ADMIN and DAC_READ_SEARCH capabilities, whole disks are watched instantly instead of folder by folder."));
  }
  if (t.limit_reached) trackerWarn.push(el("p", { class: "hint" }, "⚠ The inotify watch limit was reached, so some folders aren't watched. Raise fs.inotify.max_user_watches on the host."));
  if (t.overflows) trackerWarn.push(el("p", { class: "hint" }, `⚠ ${t.overflows} event queue overflow(s): some accesses were missed.`));

  const r = o.cycle.last_report;
  let last;
  if (o.cycle.last_error) last = el("p", {}, el("span", { class: "badge bad" }, "Error"), " ", o.cycle.last_error);
  else if (!r) last = el("p", { class: "hint" }, `The first run starts ${until(o.cycle.next_at)}.`);
  else {
    const verb = o.dry_run ? "Would move" : "Moved";
    last = el("div", {},
      el("div", { class: "stats" },
        stat(r.promoted.length.toLocaleString(), `${verb.toLowerCase()} to cache (${bytes(r.bytes_promoted)})`),
        stat(r.demoted.length.toLocaleString(), `${o.dry_run ? "would move" : "moved"} back to array (${bytes(r.bytes_demoted)})`),
        stat(r.failed.length.toLocaleString(), "failed"),
        stat(r.ignore_list_entries.toLocaleString(), "in mover ignore list")),
      r.skipped_reason ? el("p", { class: "hint" }, "Stopped early: ", r.skipped_reason) : null,
      el("p", { class: "hint" }, `Finished ${ago(o.cycle.last_end)} (${when(o.cycle.last_end)}).`));
  }

  nodes.push(el("div", { class: "grid" },
    el("div", { class: "card" },
      el("h2", {}, allOk ? "Safe to move files" : "Moving is paused"), gates),
    el("div", { class: "card" }, el("h2", {}, "Usage tracking"), tracking, trackerWarn),
    el("div", { class: "card" }, el("h2", {}, "Last run"), last)));

  // Pools
  if (o.pools.length) {
    nodes.push(el("div", { class: "card" }, el("h2", {}, "Cache pools"), o.pools.map(renderPool),
      el("div", { class: "legend" },
        el("span", {}, el("i", { style: { background: "var(--accent)" } }), "moved here by Cache Puller"),
        el("span", {}, el("i", { style: { background: "var(--bar-pool)" } }), "other data"),
        el("span", {}, el("i", { style: { background: "var(--text)", width: "3px" } }), "maximum usage setting"))));
  }

  // Shares
  const rows = o.shares.map((s) => el("tr", {},
    el("td", {}, el("strong", {}, s.name)),
    el("td", {}, s.status === "managed" ? el("span", { class: "badge ok" }, "Managed")
      : s.status === "excluded" ? el("span", { class: "badge warn" }, "Excluded in settings")
      : el("span", { class: "badge" }, "Not applicable")),
    el("td", {}, storageText(s)),
    el("td", { class: "num" }, s.status === "managed" ? s.hot_files.toLocaleString() : "")));
  nodes.push(el("div", { class: "card" }, el("h2", {}, "Shares"),
    el("p", { class: "hint" }, "Shares that use a pool as primary storage and the array as secondary storage can be managed."),
    el("div", { class: "table-wrap", style: { boxShadow: "none" } }, el("table", { class: "data" },
      el("thead", {}, el("tr", {}, el("th", {}, "Share"), el("th", {}, "Status"), el("th", {}, "Storage"), el("th", { class: "num" }, "Frequently used"))),
      el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: 4, class: "empty" }, "No share configs found.")))))));

  nodes.push(el("p", { class: "hint" }, `Cache Puller ${o.version} · array disks: ${o.disks.join(", ") || "none mounted"}`));
  root.replaceChildren(...nodes);
}

function storageText(s) {
  if (s.use_cache === "no") return "Array only";
  if (s.use_cache === "only") return `${s.pool} only`;
  const dir = s.use_cache === "yes" ? `${s.pool} → ${s.secondary}` : `${s.secondary} → ${s.pool}`;
  return `${s.pool} + ${s.secondary} (mover ${dir})`;
}

function trackingStat(t) {
  if (!t.active) return stat("–", "watching");
  if (t.mode === "fanotify") return stat(t.filesystems.toLocaleString(), `disk${t.filesystems === 1 ? "" : "s"} watched (whole disk)`);
  if (t.mode === "mixed") return stat(`${t.filesystems} + ${t.watches.toLocaleString()}`, "whole disks + single folders watched");
  if (t.scanning) return stat(t.watches.toLocaleString(), "folders watched (scanning…)");
  return stat(t.watches.toLocaleString(), "folders watched");
}

function stat(value, label) {
  return el("div", { class: "stat" }, el("div", { class: "value" }, value), el("div", { class: "label" }, label));
}

function renderPool(p) {
  if (!p.mounted || !p.total) {
    return el("div", { class: "pool" }, el("div", { class: "pool-head" },
      el("span", { class: "pool-name" }, p.name), el("span", { class: "badge bad" }, p.error || "not mounted")));
  }
  const pct = (n) => Math.max(0, Math.min(100, (n / p.total) * 100));
  const mine = Math.min(p.promoted_bytes, p.used);
  return el("div", { class: "pool" },
    el("div", { class: "pool-head" },
      el("span", { class: "pool-name" }, p.name),
      el("span", { class: "hint", style: { margin: 0 } },
        `${bytes(p.used)} of ${bytes(p.total)} used (${pct(p.used).toFixed(0)}%) · ${bytes(p.avail)} free · limit ${p.limit_percent}%`)),
    el("div", { class: "bar", title: `${bytes(mine)} moved here by Cache Puller` },
      el("div", { class: "fill other", style: { width: pct(p.used) + "%" } }),
      el("div", { class: "fill mine", style: { left: pct(p.used - mine) + "%", width: pct(mine) + "%" } }),
      el("div", { class: "limit", style: { left: p.limit_percent + "%" }, title: `maximum usage: ${p.limit_percent}%` })));
}

// ---- files -------------------------------------------------------------------

async function loadFiles() {
  const f = state.files;
  const qs = new URLSearchParams({ q: f.q, hot: f.hot ? "1" : "0", limit: f.limit, offset: f.offset });
  const data = await api(`/api/files?${qs}`);
  renderFiles(data);
}

function renderCleanupBar(c) {
  const bar = $("#cleanup-bar");
  if (!c) { bar.replaceChildren(); return; }
  const parts = [];
  if (c.running) parts.push(el("strong", {}, "Checking for deleted files…"));
  else if (!c.interval) parts.push("Deleted files are only removed from this list when you click the button, or right after they're deleted.");
  else parts.push(`Deleted files are removed from this list within a minute of being deleted, and by a full check every ${span(c.interval)}.`);
  const last = c.last;
  if (last && !c.running) {
    parts.push(" ");
    if (last.skipped_reason) parts.push(el("strong", {}, `Last check ${ago(last.finished)} was skipped: ${last.skipped_reason}.`));
    else parts.push(el("strong", {}, `Last check ${ago(last.finished)}: ${last.removed.toLocaleString()} removed.`));
    if (last.skipped_shares && last.skipped_shares.length) parts.push(` Skipped: ${last.skipped_shares.join("; ")}.`);
  }
  if (c.removed_on_delete) parts.push(` ${c.removed_on_delete.toLocaleString()} removed right after deletion since the container started.`);
  if (c.next_at && !c.running) parts.push(` Next check ${until(c.next_at)}.`);
  bar.replaceChildren(el("span", {}, parts),
    el("button", { class: "btn small", type: "button", disabled: c.running, onclick: cleanupNow }, "Clean up now"));
}

async function cleanupNow(e) {
  e.target.disabled = true;
  try {
    const res = await api("/api/cleanup", {});
    toast(res.message);
  } catch (err) { showError(err); }
  // The check runs in the background; poll until it's done.
  const poll = async () => {
    await refresh();
    const c = state.overview && state.overview.cleanup;
    if (c && c.running) setTimeout(poll, 1500);
    else if (c && c.last) {
      toast(c.last.skipped_reason ? `Check skipped: ${c.last.skipped_reason}`
        : `Removed ${c.last.removed.toLocaleString()} deleted file(s) from the list.`, !!c.last.skipped_reason);
    }
  };
  setTimeout(poll, 800);
}

async function forget(r, btn) {
  btn.disabled = true;
  try {
    const res = await api("/api/forget", { share: r.share, relpath: r.relpath });
    toast(res.message, !res.ok);
  } catch (e) { showError(e); }
  refresh();
}

function renderFiles(data) {
  $("#files-hint").textContent =
    `Score ≈ number of separate recent accesses (older ones count less). Files scoring ${data.min_score} or more are moved to the cache.`;
  renderCleanupBar(state.overview && state.overview.cleanup);
  const table = $("#files-table");
  const head = el("thead", {}, el("tr", {},
    el("th", {}, "File"), el("th", { class: "num" }, "Score"), el("th", { class: "num hide-sm" }, "Accesses"),
    el("th", { class: "hide-sm" }, "Last used"), el("th", { class: "hide-sm" }, "Where"), el("th", {}, "Status"),
    el("th", {}, "")));
  const rows = data.rows.map((r) => {
    const [label, cls] = STATE_LABELS[r.state] || [r.state, ""];
    const pct = Math.min(100, (r.score / (data.min_score * 2)) * 100);
    const actions = [];
    if (r.state === "cold" || r.state === "candidate") {
      actions.push(el("button", { class: "btn small", type: "button", title: "Move this file to the cache now (all safety checks still apply)",
        onclick: (e) => promote(r, e.target) }, "Move to cache"));
    }
    if (r.state === "missing") {
      actions.push(el("button", { class: "btn small", type: "button", title: "This file no longer exists: remove it from the list",
        onclick: (e) => forget(r, e.target) }, "Remove from list"));
    }
    if (r.state !== "unmanaged" && r.state !== "missing" && r.reason !== "matches an exclude pattern") {
      actions.push(el("button", { class: "btn small link", type: "button", title: "Add this file to the exclude patterns",
        onclick: (e) => exclude(r, e.target) }, "Never move"));
    }
    return el("tr", {},
      el("td", { class: "path" }, el("div", { class: "share" }, r.share), el("div", { class: "name" }, r.relpath)),
      el("td", { class: "num" }, el("div", { class: "score" }, r.score.toFixed(1),
        el("div", { class: "mini" }, el("span", { class: r.hot ? "hot" : "", style: { width: pct + "%" } })))),
      el("td", { class: "num hide-sm" }, r.hits.toLocaleString()),
      el("td", { class: "nowrap hide-sm", title: when(r.last_access) }, ago(r.last_access)),
      el("td", { class: "nowrap hide-sm" }, r.location || "–", r.size ? el("div", { class: "reason" }, bytes(r.size)) : null),
      el("td", {}, el("span", { class: "badge " + cls }, label), el("div", { class: "reason" }, r.reason)),
      el("td", {}, el("div", { class: "actions" }, actions)));
  });
  const emptyText = state.files.q ? "No files match your search."
    : state.files.hot ? "No frequently used files yet. Files show up here as they get opened."
    : "No file accesses recorded yet. Open some files on a managed share and they'll appear here.";
  table.replaceChildren(head, el("tbody", {}, rows.length ? rows
    : el("tr", {}, el("td", { colspan: 7, class: "empty" }, emptyText))));

  const f = state.files;
  const pager = $("#files-pager");
  const from = data.total ? data.offset + 1 : 0;
  const to = Math.min(data.total, data.offset + data.rows.length);
  pager.replaceChildren(
    el("span", {}, `${from.toLocaleString()}–${to.toLocaleString()} of ${data.total.toLocaleString()}`),
    el("button", { class: "btn small", type: "button", disabled: f.offset === 0,
      onclick: () => { f.offset = Math.max(0, f.offset - f.limit); loadFiles().catch(showError); } }, "Previous"),
    el("button", { class: "btn small", type: "button", disabled: to >= data.total,
      onclick: () => { f.offset += f.limit; loadFiles().catch(showError); } }, "Next"));
}

async function promote(r, btn) {
  btn.disabled = true;
  btn.textContent = "Moving…";
  try {
    const res = await api("/api/promote", { share: r.share, relpath: r.relpath });
    toast(res.message, !res.ok);
  } catch (e) { showError(e); }
  refresh();
}

async function exclude(r, btn) {
  if (!confirm(`Never move "${r.share}/${r.relpath}"?\n\nIt will be added to the exclude patterns in Settings, where you can remove it again.`)) return;
  btn.disabled = true;
  try {
    const res = await api("/api/exclude", { share: r.share, relpath: r.relpath });
    toast(res.message);
    state.settings = null;
  } catch (e) { showError(e); }
  refresh();
}

// ---- activity ----------------------------------------------------------------

async function loadActivity() {
  const data = await api("/api/activity?limit=200");
  const RESULT = { ok: ["Done", "ok"], skipped: ["Skipped", "warn"], error: ["Failed", "bad"] };
  const rows = data.rows.map((r) => {
    const [label, cls] = RESULT[r.result] || [r.result, ""];
    return el("tr", {},
      el("td", { class: "nowrap", title: when(r.ts) }, ago(r.ts)),
      el("td", { class: "nowrap" }, r.action === "promote" ? "Array → cache" : "Cache → array"),
      el("td", { class: "path" }, el("div", { class: "share" }, r.share), el("div", { class: "name" }, r.relpath)),
      el("td", { class: "num" }, bytes(r.size)),
      el("td", {}, el("span", { class: "badge " + cls }, label), r.message ? el("div", { class: "reason" }, r.message) : null));
  });
  $("#activity-table").replaceChildren(
    el("thead", {}, el("tr", {}, el("th", {}, "When"), el("th", {}, "Direction"), el("th", {}, "File"),
      el("th", { class: "num" }, "Size"), el("th", {}, "Result"))),
    el("tbody", {}, rows.length ? rows : el("tr", {}, el("td", { colspan: 5, class: "empty" },
      state.overview && state.overview.dry_run
        ? "Nothing has been moved yet: dry run is on. Planned moves are listed on the Files tab."
        : "Nothing has been moved yet."))));
}

// ---- settings ------------------------------------------------------------------

async function loadSettings(force) {
  if (!state.settings || force) {
    state.settings = await api("/api/settings");
    state.pending = {};
  }
  renderSettings();
}

function currentValue(f) {
  return Object.prototype.hasOwnProperty.call(state.pending, f.key) ? state.pending[f.key] : f.value;
}

function setPending(f, value) {
  const same = JSON.stringify(value) === JSON.stringify(f.value);
  if (same) delete state.pending[f.key]; else state.pending[f.key] = value;
  updateSavebar();
  const row = document.querySelector(`[data-field="${f.key}"]`);
  if (row) row.classList.toggle("changed", !same);
}

function updateSavebar() {
  const n = Object.keys(state.pending).length;
  $("#savebar").hidden = n === 0;
  $("#savebar-text").textContent = `${n} unsaved change${n === 1 ? "" : "s"}`;
}

function control(f) {
  const v = currentValue(f);
  switch (f.kind) {
    case "bool":
      return el("label", { class: "switch" },
        el("input", { type: "checkbox", checked: !!v, id: f.key, onchange: (e) => setPending(f, e.target.checked) }),
        el("span", {}));
    case "number":
      return el("div", { class: "row" },
        el("input", { type: "number", id: f.key, value: v, step: "any", min: 0,
          oninput: (e) => { if (e.target.value !== "") setPending(f, Number(e.target.value)); } }),
        f.unit ? el("span", { class: "unit" }, f.unit) : null);
    case "size":
      return el("div", { class: "row" },
        el("input", { type: "text", id: f.key, value: v, placeholder: "e.g. 50G",
          oninput: (e) => setPending(f, e.target.value.trim()) }),
        el("span", { class: "unit" }, "K / M / G / T"));
    case "hours":
      return el("input", { type: "text", id: f.key, value: v, placeholder: "any time",
        oninput: (e) => setPending(f, e.target.value.trim()) });
    case "choice":
      return el("select", { id: f.key, onchange: (e) => setPending(f, e.target.value) },
        f.choices.map((c) => el("option", { value: c, selected: c === v }, c)));
    case "list":
      return el("textarea", { id: f.key, spellcheck: "false",
        oninput: (e) => setPending(f, e.target.value.split("\n").map((x) => x.trim()).filter(Boolean)) },
        (v || []).join("\n"));
    case "multichoice":
    case "shares": {
      const options = f.kind === "shares" ? state.settings.available_shares : f.choices;
      const all = [...new Set([...options, ...(v || [])])];
      if (!all.length) return el("span", { class: "hint" }, "No shares with the array as secondary storage found.");
      return el("div", { class: "chips" }, all.map((c) => {
        const on = (v || []).includes(c);
        return el("label", { class: "chip" + (on ? " on" : "") },
          el("input", { type: "checkbox", checked: on, onchange: (e) => {
            const cur = new Set(currentValue(f) || []);
            e.target.checked ? cur.add(c) : cur.delete(c);
            e.target.parentElement.classList.toggle("on", e.target.checked);
            setPending(f, all.filter((x) => cur.has(x)));
          } }), c);
      }));
    }
    default:
      return el("input", { type: "text", id: f.key, value: v, oninput: (e) => setPending(f, e.target.value) });
  }
}

function renderSettings() {
  const s = state.settings;
  const groups = s.groups.map((g) => {
    const fields = s.fields.filter((f) => f.group === g.key);
    return el("div", { class: "card settings-group" }, el("h2", {}, g.label),
      fields.map((f) => el("div", { class: "field" + (f.key in state.pending ? " changed" : ""), "data-field": f.key },
        el("div", {},
          el("label", { class: "name", for: f.key }, f.label,
            f.customized ? el("span", { class: "dot", title: "Changed from the container default" }) : null)),
        el("div", { class: "control" }, control(f), el("div", { class: "help" }, f.help)))));
  });
  const footer = el("div", { class: "settings-footer" },
    el("span", {}, el("span", { class: "dot" }), " = changed here (saved in /config/settings.json, overrides the container's environment variables)"),
    el("button", { class: "btn", type: "button", onclick: resetSettings }, "Reset all to container defaults"));
  $("#settings-form").replaceChildren(...groups, footer);
  updateSavebar();
}

async function saveSettings() {
  const btn = $("#save");
  btn.disabled = true;
  try {
    state.settings = await api("/api/settings", { changes: state.pending });
    state.pending = {};
    renderSettings();
    toast("Settings saved and applied.");
    refresh();
  } catch (e) { showError(e); }
  btn.disabled = false;
}

async function resetSettings() {
  if (!confirm("Reset every setting changed here back to the container's defaults?")) return;
  try {
    state.settings = await api("/api/settings/reset", {});
    state.pending = {};
    renderSettings();
    toast("Settings reset.");
    refresh();
  } catch (e) { showError(e); }
}

// ---- navigation & refresh ----------------------------------------------------------

function showTab(tab, opts) {
  state.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${tab}`));
  if (opts && opts.hot !== undefined) {
    state.files.hot = opts.hot;
    $("#hot-only").checked = opts.hot;
    state.files.offset = 0;
  }
  if (location.hash !== `#${tab}`) history.replaceState(null, "", `#${tab}`);
  refresh();
}

function showError(e) { toast(e.message || String(e), true); }

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state.overview = await api("/api/overview");
    renderHeader(state.overview);
    if (state.tab === "overview") renderOverview(state.overview);
    else if (state.tab === "files") await loadFiles();
    else if (state.tab === "activity") await loadActivity();
    else if (state.tab === "settings") await loadSettings(false);
  } catch (e) {
    $("#run-info").textContent = "Can't reach the service: " + e.message;
  } finally {
    refreshing = false;
  }
}

function init() {
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#run-now").addEventListener("click", async () => {
    try { await api("/api/run", {}); toast("Run started."); } catch (e) { showError(e); }
    setTimeout(refresh, 800);
  });
  let timer;
  $("#file-search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.files.q = e.target.value.trim(); state.files.offset = 0; loadFiles().catch(showError); }, 250);
  });
  $("#hot-only").addEventListener("change", (e) => { state.files.hot = e.target.checked; state.files.offset = 0; loadFiles().catch(showError); });
  $("#save").addEventListener("click", saveSettings);
  $("#discard").addEventListener("click", () => { state.pending = {}; renderSettings(); });
  window.addEventListener("beforeunload", (e) => { if (Object.keys(state.pending).length) e.preventDefault(); });

  const TABS = ["overview", "files", "activity", "settings"];
  window.addEventListener("hashchange", () => {
    const t = location.hash.slice(1);
    if (TABS.includes(t) && t !== state.tab) showTab(t);
  });
  const initial = location.hash.slice(1);
  showTab(TABS.includes(initial) ? initial : "overview");
  // Keep the page live, but never re-render the settings form under the user's cursor.
  setInterval(() => { if (!document.hidden && state.tab !== "settings") refresh(); }, 10000);
}

init();
