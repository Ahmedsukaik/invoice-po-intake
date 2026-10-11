/* Invoice and PO Intake demo page.
   A six step story (one action each), then free use. The server keeps nothing: this page holds
   the signed list of what you did (the "session") and sends it with every request. Each request
   replays it through the product's own reader, checks and review rules. See intake/demo_session.py. */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const icon = (name, cls = "i") => `<svg class="${cls}" aria-hidden="true" focusable="false"><use href="#${name}"/></svg>`;
  const squash = (t) => String(t || "").replace(/\s+/g, "").toLowerCase();
  const usd = (n) => "$" + Number(n || 0).toFixed(4);
  const isPhone = () => window.matchMedia("(max-width: 900px)").matches;
  const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const STEPS = 6;
  const STORY = { 1: "01", 3: "04", 4: "06", 5: "10" };      // the sample each step brings in
  const STORY_DOC = { 1: "01", 2: "01", 3: "04", 4: "06", 5: "10" };  // the document each step is about
  const REJECT_04 = "New bank details on the invoice. Calling Cobalt on the number in the vendor master.";
  const NOTE_06 = "Buyer agreed the extra 40 plates by phone; a new PO follows.";
  const AI_WORDS = {
    off: "You switched the AI reader off.",
    unavailable: "The AI reader is not switched on in this demo right now.",
    visitor_limit: "You have used this demo's AI reads for the next few minutes.",
    busy: "The AI reader is busy right now.",
    budget: "The demo's AI budget is used up for now.",
    failed: "The AI reader could not read it this time.",
  };
  const LOCK_WORDS = { bank_changed: "New bank details", vendor_unknown: "Unknown supplier", duplicate_invoice: "Duplicate",
    duplicate_po: "Duplicate PO", bank_invalid: "Invalid IBAN", currency_not_allowed: "Currency" };
  const TYPE_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "currency", "vendor_iban",
    "vendor_sort_code", "vendor_account", "subtotal", "tax_rate", "tax_amount", "total"];
  const LINE_KEYS = ["sku", "description", "quantity", "unit_price", "line_total"];

  const S = {
    config: null, previews: {}, session: null, view: null, step: 0, selected: null, reviewer: "sara", ai: true,
    busy: false, wait: "", main: "doc", pane: "review", acks: {}, notes: {}, reasons: {}, rejecting: null, errors: {},
    typing: null, typed: {}, hover: null, stamps: {}, keyboard: false, flash: null,
  };

  // ------------------------------------------------------------------ small helpers
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode: fine */ } },
  };
  function save() {
    store.set("intake-demo", { session: S.session, step: S.step, selected: S.selected, reviewer: S.reviewer, main: S.main, ai: S.ai });
  }
  let toastTimer = null;
  function toast(msg, ms = 4200) {
    const el = $("#toast"); el.textContent = msg; el.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, ms);
  }
  let announceTimer = null;
  function announce(msg) {
    const el = $("#announce"); el.textContent = "";
    clearTimeout(announceTimer); announceTimer = setTimeout(() => { el.textContent = msg; }, 60);
  }
  async function post(path, body) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const err = new Error(data.message || "Something went wrong."); err.kind = data.error; throw err; }
    return data;
  }
  const docs = () => (S.view ? S.view.documents : []);
  const byKey = (key) => docs().find((d) => d.key === key) || null;
  const byId = (id) => docs().find((d) => d.id === id) || null;
  const current = () => byId(S.selected);
  const reviewerName = (id) => (S.config?.reviewers || []).find((r) => r.id === id)?.name || id;
  const other = (id) => (id === "sara" ? "omar" : "sara");
  const sample = (key) => (S.config?.samples || []).find((s) => s.key === key);
  const inStory = () => S.step !== "free";

  // ------------------------------------------------------------------ talking to the server
  async function newSession() {
    const data = await post("/api/new");
    S.session = data.session; S.view = data.view; S.selected = null; S.acks = {}; S.notes = {}; S.reasons = {};
    S.errors = {}; S.rejecting = null; S.typing = null; S.typed = {}; S.stamps = {};
    save();
  }
  async function step(action) {
    let data;
    try {
      data = await post("/api/step", { session: S.session, action, ai: S.ai });
    } catch (err) {
      if (err.kind === "session") { await newSession(); render(); toast("That demo session could not be continued, so a new one started."); return null; }
      throw err;
    }
    S.session = data.session; S.view = data.view;
    save();
    return data;
  }
  function setBusy(b, wait = "") {
    S.busy = b; S.wait = b ? wait : "";
    document.body.classList.toggle("busy", b);
    $$("#story-actions button, .decision .btn, [data-export], [data-add]").forEach((x) => { x.disabled = b || x.dataset.off === "1"; });
    if (b) renderStory();
  }
  async function act(action, wait) {
    if (S.busy) return null;
    setBusy(true, wait);
    try {
      const data = await step(action);
      return data;
    } catch (err) {
      toast(err.message); return null;
    } finally { setBusy(false); render(); }
  }
  async function addSample(key) {
    const s = sample(key);
    const wait = s && s.layout === "other" && S.ai && S.config.live ? "The AI reader is reading it. Usually 5 to 15 seconds."
      : "Reading the PDF and running the checks.";
    const data = await act({ type: "add", sample: key }, wait);
    if (data && data.result) {
      select(data.result.doc);
      if (!data.result.created) toast(data.result.message);
      const d = byId(data.result.doc);
      if (d) announce(`${d.vendor || d.file}: ${d.status_label}. ${d.summary}.`);
    }
    return data;
  }

  // ------------------------------------------------------------------ the story
  const bars = (n) => `<span class="story-bars" aria-hidden="true">${Array.from({ length: STEPS }, (_, i) => `<i class="${i < n ? "on" : ""}"></i>`).join("")}</span>`;
  const head = (n, title) => `<div class="story-kicker">${bars(n)}<span>Step ${n} of ${STEPS}</span></div><h2 class="story-title" id="story-title">${esc(title)}</h2>`;
  const text = (html) => `<p class="story-text">${html}</p>`;
  const primary = (go, label, extra = "") => `<button type="button" class="btn btn-primary btn-story" data-go="${go}"${extra}>${esc(label)}${icon("arrow-right", "i i-sm")}</button>`;
  const decided = (d) => d && !d.waiting;

  function storyContent() {
    const v = S.view, wait = S.busy && S.wait ? `<p class="story-wait"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>${esc(S.wait)}</p>` : "";
    switch (S.step) {
      case 0: return {
        main: `<span class="eyebrow">Live demo on fictional documents</span>
          <h2 class="story-headline" id="story-title">Invoices read, checked against the PO, and approved by a person.</h2>
          <p class="story-sub">Six short steps on real demo PDFs. You are the reviewer. About two minutes.</p>
          <ul class="hero-points"><li>${icon("check")}Each value linked to the line it was read from</li>
            <li>${icon("check")}New bank details cannot be approved</li><li>${icon("check")}Nothing is exported until a person approves it</li></ul>
          <p class="story-fine">The demo PDFs are read with fixed rules that know one layout. Other layouts need the AI reader. Without it, they go to a person. The date is fixed at 11 October 2026.</p>`,
        actions: `${primary("start", "Start the demo")}<button type="button" class="btn-link" data-go="skip">Skip to free use</button>`,
        focus: "review" };
      case 1: return {
        main: head(1, "An invoice arrives.") + text("Acme emails invoice INV-A-1001. The clerk drops the PDF into the inbox. Watch what happens to it.") + wait,
        actions: S.busy ? "" : primary("bring", "Bring in invoice 01"), focus: "paper" };
      case 2: {
        const d = byKey("01");
        if (decided(d)) return {
          main: head(2, d.status === "rejected" ? "Rejected, with your reason logged." : "Approved, stamped and logged.") +
            text(d.status === "rejected" ? "It will never reach the export. Your reason is in the audit trail."
              : "Your approval is in the audit trail. The invoice now waits for the export, and nobody can change it."),
          actions: primary("next", "Next: new bank details"), focus: "paper" };
        return {
          main: head(2, "Read, checked, matched to its PO. Approve it.") +
            text(`Every value on the right came from the line marked on the page. ${isPhone() ? "" : "Hover one to see where. "}<b>Nothing is wrong, and PO-2026-0101 covers it.</b> Approve it in the review panel.`),
          actions: isPhone() && S.pane !== "review" ? primary("show-review", "Open the review") : "", focus: "review" };
      }
      case 3: {
        const d = byKey("04");
        if (!d) return {
          main: head(3, "A supplier asks to be paid to a new account.") + text("Cobalt's invoice says it has changed banks and asks you to pay the new account.") + wait,
          actions: S.busy ? "" : primary("bring", "Bring in invoice 04"), focus: "paper" };
        if (decided(d)) return {
          main: head(3, "Rejected, and it never reaches the export.") + text("Changed bank details are a common invoice fraud. The reason you gave is in the audit trail."),
          actions: primary("next", "Next: an invoice over its order"), focus: "paper" };
        return {
          main: head(3, "New bank details: Approve is locked.") +
            text(`The IBAN on the PDF is not the one in the vendor master, so <b>nobody can approve it here</b>. A real bank change goes through the vendor master's own process. Reject it.`),
          actions: isPhone() && S.pane !== "review" ? primary("show-review", "Open the review") : "", focus: "review" };
      }
      case 4: {
        const d = byKey("06");
        if (!d) return {
          main: head(4, "More than was ordered.") + text("Acme bills 140 mounting plates. The purchase order was for 100.") + wait,
          actions: S.busy ? "" : primary("bring", "Bring in invoice 06"), focus: "paper" };
        if (decided(d)) return {
          main: head(4, d.status === "rejected" ? "Rejected. That works too." : "Approved, with two findings signed off.") +
            text("If a number on an invoice changes later, old ticks stop counting and it must be checked again. The note travels with the invoice to the export."),
          actions: primary("next", "Next: a layout it has never seen"), focus: "paper" };
        return {
          main: head(4, "Tick each finding, say why, then approve.") +
            text("Two checks failed, circled in red pen. You can still approve it, but only after ticking each one and writing a note."),
          actions: isPhone() && S.pane !== "review" ? primary("show-review", "Open the review") : "", focus: "review" };
      }
      case 5: {
        const d = byKey("10");
        const live = S.config?.live && S.ai;
        if (!d) return {
          main: head(5, "A layout the rules have never seen.") + text(`Brightline sends its own invoice design. ${live
            ? "The AI reader, Claude Haiku 5.5, reads it."
            : "The demo PDFs are read with fixed rules that know one layout. Watch where this one goes."}`) + wait,
          actions: S.busy ? "" : primary("bring", "Bring in invoice 10"), focus: "paper" };
        const ok = d.ai && d.ai.status === "ok";
        return {
          main: ok ? head(5, "Read by AI, each value with its source line.") +
              text("Claude copied every value with the line it came from. The code parsed the amounts and ran the same checks as on any invoice. A person still approves.")
            : head(5, "Unknown layout: it goes to a person.") +
              text(`Other layouts need the AI reader. Without it, they go to a person, who types the values in. ${esc((d.ai && AI_WORDS[d.ai.status]) || "")}`),
          actions: primary("next", "Next: export and audit trail"), focus: ok ? "paper" : "review" };
      }
      case 6: {
        const exported = (v?.exports || []).length > 0;
        if (!exported) return {
          main: head(6, "Export what was approved.") + text("Approved invoices go to accounting as CSV and JSON. <b>Export creates files in your browser.</b>"),
          actions: isPhone() && S.pane !== "books" ? primary("show-books", "Open the export") : "", focus: "books" };
        const m = v.metrics || {};
        const figures = m.ai_reads ? `<div class="story-figures"><div><span>AI reading of invoice 10</span><strong>${usd(m.cost_usd)}</strong></div>
            <div><span>1,000 invoices like it</span><strong>$${(m.cost_usd * 1000).toFixed(2)}</strong></div></div>`
          : `<p class="story-text">This run used the rules reader only, so it cost nothing. The AI reader is estimated at about $0.001 to $0.003 an invoice, before any measurement on real documents.</p>`;
        return {
          main: head(6, "What you just did, all logged.") + figures + `<ul class="story-list">
            <li>${icon("check-circle")}<span>Invoice 01 read, matched to its PO and approved by you</span></li>
            <li>${icon("check-circle")}<span>New bank details locked, then rejected</span></li>
            <li>${icon("check-circle")}<span>Over the PO: approved only with each finding signed off</span></li>
            <li>${icon("check-circle")}<span>An unknown layout ${byKey("10")?.ai?.status === "ok" ? "read by AI, with source lines" : "sent to a person"}</span></li>
            <li>${icon("check-circle")}<span>Exported once as CSV and JSON; every step in the audit trail</span></li></ul>
            <p class="story-note">Your version reads your suppliers' PDFs and posts to your accounting system.</p>`,
          actions: `${primary("free", "Try it yourself")}<button type="button" class="btn-link" data-go="replay">Replay the demo</button>`, focus: "books" };
      }
      default: return {
        main: `<div class="story-kicker"><span>Free use</span></div><h2 class="story-title" id="story-title">Bring in any sample and review it as Sara or Omar.</h2>
          <p class="story-text">Try <button type="button" class="link" data-add="05">05, a duplicate</button>, <button type="button" class="link" data-add="08">08, a scan</button> (one reviewer types it in, the other approves) and <button type="button" class="link" data-add="09">09, a price above the PO</button>.</p>`,
        actions: "", focus: "inbox" };
    }
  }

  function renderStory() {
    const c = storyContent();
    $("#story-main").innerHTML = c.main;
    $("#story-actions").innerHTML = c.actions;
    $("#story-art").innerHTML = S.step === 0 ? heroArt() : "";
    document.body.dataset.step = String(S.step);
    document.body.dataset.focus = c.focus;
    document.body.classList.toggle("in-story", inStory());
    $("#replay").hidden = S.step === 0 || S.step === 1;
    $$("#story-actions button").forEach((x) => { x.disabled = S.busy; });
    $("#layout").inert = S.step === 0;
    $$("[data-add]").forEach((x) => { x.disabled = S.busy || !!byKey(x.dataset.add); x.dataset.off = byKey(x.dataset.add) ? "1" : ""; });
  }
  function storyFocus() { return storyContent().focus; }
  function setStep(n) {
    const before = S.step;
    S.step = n;
    const c = storyContent();
    S.main = c.focus === "books" ? "books" : "doc";
    if (STORY_DOC[n] && byKey(STORY_DOC[n])) S.selected = byKey(STORY_DOC[n]).id;
    save(); render();
    if (before !== n) {
      announce($("#story-title")?.textContent || "");
      if (isPhone()) setPane(c.focus === "inbox" ? "inbox" : c.focus, false);
      if (S.keyboard) $("#story-actions .btn-primary")?.focus({ preventScroll: true });
    }
    revealTarget();
  }
  // After each step renders, bring the thing it asks for into view inside its pane.
  function revealTarget() {
    requestAnimationFrame(() => {
      const target = $(".ring") || (S.step === 3 && $(".lockbox")) || null;
      if (!target) return;
      const pane = target.closest(".pane");
      if (!pane) return;
      const p = pane.getBoundingClientRect(), box = target.closest(".decision");
      const b = box && box.getBoundingClientRect(), t = target.getBoundingClientRect();
      if (b && b.height <= p.height - 16 && (b.top < p.top + 8 || b.bottom > p.bottom - 8)) pane.scrollTop += b.top - p.top - 8;
      else if (!(b && b.height <= p.height - 16) && (t.top < p.top + 8 || t.bottom > p.bottom - 8)) pane.scrollTop += t.top - p.top - Math.max(12, (p.height - t.height) / 3);
      if (S.keyboard) target.focus?.({ preventScroll: true });
    });
  }
  function advance() {
    if (S.step === 1 && byKey("01")) setStep(2);
    else renderStory();
  }

  async function go(what) {
    if (S.busy) return;
    try {
      if (what === "start" || what === "replay") { await newSession(); setStep(1); }
      else if (what === "skip") { await newSession(); setStep("free"); setPane("inbox", false); }
      else if (what === "free") { setStep("free"); S.main = "doc"; render(); if (isPhone()) setPane("inbox", false); }
      else if (what === "bring") {
        const key = STORY[S.step];
        await addSample(key);
        if (S.step === 3) S.reasons[byKey("04")?.id] = REJECT_04;
        if (S.step === 1) { advance(); return; }
        const c = storyContent();
        S.main = "doc"; render();
        if (isPhone()) setPane(c.focus, true);
        revealTarget();
      } else if (what === "next") setStep(S.step + 1);
      else if (what === "show-review") { setPane("review", true); renderStory(); revealTarget(); }
      else if (what === "show-books") { setPane("books", true); renderStory(); revealTarget(); }
      else if (what === "books") { S.main = "books"; save(); render(); if (isPhone()) setPane("books", true); }
      else if (what === "back") { S.main = "doc"; save(); render(); if (isPhone()) setPane("inbox", true); }
    } catch (err) { toast(err.message); }
  }

  // ------------------------------------------------------------------ the paper sheet
  function previewFor(d) { return d ? S.previews[d.file] || null : null; }
  function lineIndex(preview, quote) {
    if (!preview || !quote) return -1;
    const lines = preview.lines, want = squash(quote);
    let i = lines.findIndex((l) => l.text === quote);
    if (i < 0) i = lines.findIndex((l) => squash(l.text) === want);
    if (i < 0 && want.length >= 6) i = lines.findIndex((l) => squash(l.text).includes(want) || want.includes(squash(l.text)));
    return i;
  }
  function problemKeys(d) {
    const keys = new Set();
    for (const f of d.findings) {
      if (f.severity !== "error" && f.severity !== "blocking") continue;
      if (f.field === "lines") {
        const m = /^Line (\d+)/.exec(f.message);
        if (m) keys.add("line:" + (Number(m[1]) - 1)); else d.lines.forEach((_, j) => keys.add("line:" + j));
      } else if (f.field) keys.add(f.field);
    }
    return keys;
  }
  function marksFor(d) {
    const p = previewFor(d);
    if (!p) return [];
    const found = new Map();
    const add = (quote, key, label) => {
      const i = lineIndex(p, quote);
      if (i < 0) return;
      const m = found.get(i) || { i, box: p.lines[i].box, keys: [], labels: [], pen: false };
      m.keys.push(key); m.labels.push(label); found.set(i, m);
    };
    d.fields.forEach((f) => { if (f.source && f.value && f.name !== "doc_type") add(f.source.text, f.name, f.label); });
    d.lines.forEach((l, j) => { if (l.source) add(l.source.text, "line:" + j, `Line ${j + 1}`); });
    if (d.waiting) {
      const bad = problemKeys(d);
      for (const m of found.values()) if (m.keys.some((k) => bad.has(k))) m.pen = true;
    }
    return [...found.values()];
  }
  function stampFor(d, small = false) {
    if (!d || !d.stamp) return "";
    const cls = small ? " stamp-sm" : "";
    const landing = !small && S.stamps[d.id] !== undefined && S.stamps[d.id] !== d.stamp && !reduced() ? " landing" : "";
    const by = d.decided ? `${esc(d.decided.by)} · ${esc(d.decided.date)}` : "";
    if (d.stamp === "locked") {
      const code = (d.findings.find((f) => f.severity === "error" && LOCK_WORDS[f.code]) || {}).code;
      return `<div class="stamp stamp-red${cls}${landing}" role="img" aria-label="Locked: ${esc(LOCK_WORDS[code] || "cannot be approved")}">${icon("lock", "i stamp-icon")}<span class="stamp-word">Locked</span><span class="stamp-meta">${esc(LOCK_WORDS[code] || "Cannot be approved")}</span></div>`;
    }
    if (d.stamp === "rejected") return `<div class="stamp stamp-red${cls}${landing}" role="img" aria-label="Rejected"><span class="stamp-word">Rejected</span><span class="stamp-meta">${by}</span></div>`;
    const word = d.stamp === "exported" ? "Exported" : "Approved";
    return `<div class="stamp stamp-green${cls}${landing}" role="img" aria-label="${word} by ${esc(d.decided?.by || "")}"><span class="stamp-word">${word}</span><span class="stamp-meta">${by}</span></div>`;
  }
  function sheet(d, { art = false } = {}) {
    const p = previewFor(d);
    if (!p) return `<div class="sheet sheet-missing"><p>No preview for this file.</p></div>`;
    const marks = art ? marksFor(d).filter((m) => m.keys.some((k) => ["invoice_number", "po_number", "total", "line:0", "line:1", "subtotal"].includes(k))) : marksFor(d);
    const W = p.width, H = p.height;
    const pen = marks.filter((m) => m.pen).map((m) => {
      const [x0, y0, x1, y1] = m.box;
      return `<ellipse class="pen-ring" pathLength="100" cx="${((x0 + x1) / 2) * W}" cy="${((y0 + y1) / 2) * H}" rx="${((x1 - x0) / 2) * W + 22}" ry="${((y1 - y0) / 2) * H + 15}" transform="rotate(-1.5 ${((x0 + x1) / 2) * W} ${((y0 + y1) / 2) * H})"/>`;
    }).join("");
    const boxes = marks.map((m) => {
      const [x0, y0, x1, y1] = m.box;
      const active = !art && S.hover && m.keys.includes(S.hover) ? " active" : "";
      return `<span class="mark${m.pen ? " is-pen" : ""}${active}" data-mark="${esc(m.keys.join(" "))}" style="left:${(x0 * 100).toFixed(2)}%;top:${(y0 * 100).toFixed(2)}%;width:${((x1 - x0) * 100).toFixed(2)}%;height:${((y1 - y0) * 100).toFixed(2)}%"></span>`;
    }).join("");
    const alt = `Page 1 of ${d.file}${d.vendor ? ", from " + d.vendor : ""}`;
    return `<figure class="sheet" style="--ratio:${(W / H).toFixed(4)}">
      <img src="${esc(p.png)}" width="${W}" height="${H}" alt="${esc(alt)}" draggable="false">
      <div class="marks" aria-hidden="true">${boxes}</div>
      <svg class="pen" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${pen}</svg>
      ${art ? `<div class="stamp stamp-green stamp-art" aria-hidden="true"><span class="stamp-word">Approved</span><span class="stamp-meta">Sara · 11 Oct 2026</span></div>` : stampFor(d)}
    </figure>`;
  }
  function heroArt() {
    const fake = { file: "01_clean_invoice.pdf", vendor: "Acme Demo Components Ltd", waiting: false, findings: [],
      fields: [["invoice_number", "Invoice number: INV-A-1001"], ["po_number", "PO number: PO-2026-0101"], ["subtotal", "Subtotal 1,500.00"], ["total", "Total due 1,800.00"]]
        .map(([name, t]) => ({ name, label: name, value: "x", source: { text: t } })),
      lines: [{ source: { text: "AC-100 Steel bracket, 40 mm 200 4.50 900.00" } }, { source: { text: "AC-220 Hinge set, zinc 50 12.00 600.00" } }] };
    return previewFor(fake) ? `<div class="hero-sheet">${sheet(fake, { art: true })}</div>` : "";
  }
  function renderPaper() {
    const pane = $("#paper-pane"), d = current();
    if (!d) {
      pane.innerHTML = `<div class="desk"><div class="desk-empty">${icon("ledger", "i i-xl")}<p>${inStory() ? "The PDF shows here once it arrives." : "Pick a document in the inbox, or bring in a sample."}</p></div></div>`;
      return;
    }
    const s = sample(d.key);
    const caption = d.reader.startsWith("claude") ? "Lines the AI reader quoted, marked on the page."
      : d.reader === "none" ? "A scan: no text to read. A person reads the page."
      : d.reader === "rules" && d.status === "needs_manual_entry" ? "Not read: the rules reader does not know this layout."
      : "Each line a value was read from, marked on the page.";
    pane.innerHTML = `<div class="desk">
      <div class="sheet-wrap">${sheet(d)}</div>
      <p class="sheet-caption"><span>${esc(caption)}${marksFor(d).some((m) => m.pen) ? ` <span class="pen-key">Red pen: a problem.</span>` : ""}</span>
        ${s ? `<a href="${esc(s.pdf)}" target="_blank" rel="noopener">${icon("external", "i i-sm")}Open the PDF</a>` : ""}</p></div>`;
    S.stamps[d.id] = d.stamp;
  }

  // ------------------------------------------------------------------ the inbox
  function pillFor(d) {
    if (d.waiting && d.hard_lock) return `<span class="pill pill-red">${icon("lock", "i i-xs")}Locked</span>`;
    const cls = { needs_review: "pill-amber", needs_manual_entry: "pill-amber", approved: "pill-green", exporting: "pill-green", exported: "pill-ink", rejected: "pill-red" }[d.status] || "";
    return `<span class="pill ${cls}">${esc(d.status_label)}</span>`;
  }
  function renderInbox() {
    const pane = $("#inbox-pane"), v = S.view || { documents: [], counts: {} };
    const rows = v.documents.map((d) => `<li><button type="button" class="q-row${d.id === S.selected && S.main === "doc" ? " is-selected" : ""}" data-doc="${esc(d.id)}" aria-current="${d.id === S.selected && S.main === "doc"}">
        <span class="q-key mono">${esc(d.key || "")}</span>
        <span class="q-main"><span class="q-vendor">${esc(d.vendor || sample(d.key)?.title || d.file)}</span>
          <span class="q-sub"><span class="mono">${esc(d.number || "not read yet")}</span>${d.total ? `<span class="mono">${esc(d.total)}</span>` : ""}</span>
          <span class="q-status">${pillFor(d)}</span></span></button></li>`).join("");
    const waiting = v.counts.waiting || 0, approved = v.counts.approved || 0;
    const sc = $("#switch-count"); sc.hidden = !waiting; sc.textContent = waiting;
    const add = !inStory() ? `<div class="add-list"><h3 class="pane-sub">Bring in a sample PDF</h3><ul>${(S.config?.samples || []).map((s) => {
      const there = !!byKey(s.key);
      return `<li><button type="button" class="add-row" data-add="${esc(s.key)}"${there ? ' data-off="1" disabled' : ""}>
        <span class="q-key mono">${esc(s.key)}</span><span class="q-main"><span class="q-vendor">${esc(s.title)}</span><span class="q-sub">${esc(there ? "In the inbox" : s.note)}</span></span>
        ${there ? icon("check", "i i-sm add-done") : icon("plus", "i i-sm")}</button></li>`;
    }).join("")}</ul></div>` : `<p class="pane-hint">During the story, each step brings in its own invoice. Afterwards you can bring in all ten samples.</p>`;
    pane.innerHTML = `<div class="pane-head"><h2 class="pane-title">${icon("inbox")}Inbox</h2><span class="pane-count">${waiting} to review</span></div>
      ${rows ? `<ol class="queue">${rows}</ol>` : `<p class="pane-empty">Nothing has arrived yet.</p>`}
      <button type="button" class="books-link${S.main === "books" ? " is-selected" : ""}" data-go="books">${icon("book")}<span><b>Export and audit trail</b><small>${approved} approved, waiting for export</small></span>${icon("arrow-right", "i i-sm")}</button>
      ${add}`;
  }

  // ------------------------------------------------------------------ the review panel
  function readerBadge(d) {
    if (d.reader.startsWith("claude")) return `<span class="badge badge-ai">${icon("spark", "i i-xs")}Read by AI, Claude Haiku 5.5</span>`;
    if (d.reader === "rules" && d.status !== "needs_manual_entry") return `<span class="badge">Read by the rules reader</span>`;
    return `<span class="badge">${icon("pen", "i i-xs")}${d.corrections.length ? "Typed in by a person" : "Not read: needs a person"}</span>`;
  }
  function findingLine(f, withTick, ticked) {
    const sev = f.severity === "error" ? "is-error" : f.severity === "blocking" ? "is-blocking" : f.severity === "warning" ? "is-warning" : "is-info";
    let msg = f.message;
    if (msg.toLowerCase().startsWith(f.label.toLowerCase()) && /^[:.]\s*\S/.test(msg.slice(f.label.length))) {
      const rest = msg.slice(f.label.length).replace(/^[:.]\s*/, "");
      msg = rest[0].toUpperCase() + rest.slice(1);
    }
    const body = `<span class="f-label">${esc(f.label)}</span><span class="f-msg">${esc(msg)}</span>`;
    if (withTick) return `<li class="finding ${sev}"><label class="tick-row"><input type="checkbox" data-ack="${esc(f.id)}"${ticked ? " checked" : ""}><span class="tick-box" aria-hidden="true">${icon("check", "i i-xs")}</span><span class="f-body">${body}</span></label></li>`;
    return `<li class="finding ${sev}"><span class="f-mark" aria-hidden="true">${f.severity === "info" ? icon("check", "i i-xs") : icon(f.severity === "warning" ? "alert" : "x", "i i-xs")}</span><span class="f-body">${body}</span></li>`;
  }
  function whyManual(d) {
    if (d.findings.some((f) => f.code === "reader_unknown_layout")) {
      const why = d.ai && d.ai.status !== "ok" ? " " + (AI_WORDS[d.ai.status] || "") : "";
      return "The rules reader knows only the demo layout, and this is another one." + why;
    }
    return d.manual_reason || "It could not be read.";
  }
  function reviewerSwitch(d) {
    if (inStory()) return "";
    return `<div class="who-row"><span>Reviewing as</span><div class="seg" role="group" aria-label="Reviewing as">${(S.config?.reviewers || []).map((r) =>
      `<button type="button" data-reviewer="${esc(r.id)}" aria-pressed="${S.reviewer === r.id}">${icon("user", "i i-xs")}${esc(r.name)}</button>`).join("")}</div></div>`;
  }
  function rejectForm(d) {
    if (S.rejecting !== d.id) return "";
    const err = S.errors["reject:" + d.id];
    return `<div class="reject-form"><label class="field-label" for="reason-${esc(d.id)}">Reason for rejecting</label>
      <textarea class="input${err ? " has-error" : ""}" id="reason-${esc(d.id)}" data-reason="${esc(d.id)}" rows="2" maxlength="300" placeholder="For example: new bank details; calling the supplier on the number we have.">${esc(S.reasons[d.id] || "")}</textarea>
      ${err ? `<p class="field-error">${icon("alert", "i i-xs")}${esc(err)}</p>` : ""}</div>`;
  }
  function decision(d) {
    const ring = (on) => (on ? " ring" : "");
    if (!d.waiting) {
      const x = d.decided || {};
      const ok = d.status !== "rejected";
      const acked = x.acknowledged && x.acknowledged.length ? `<p>Signed off: ${x.acknowledged.map(esc).join("; ")}.</p>` : "";
      const note = x.note ? `<p class="quote">“${esc(x.note)}”</p>` : "";
      const exported = d.status === "exported" ? `<p>Exported to accounting. It can never be exported twice.</p>` : ok ? `<p>Waiting for the next export.</p>` : "";
      return `<section class="decision decided ${ok ? "ok" : "no"}" aria-label="Decision">
        <div class="decided-head">${icon(ok ? "check-circle" : "x-circle")}<b>${ok ? "Approved" : "Rejected"} by ${esc(x.by)}</b><span class="muted">at ${esc(x.at)} on ${esc(x.date)}</span></div>
        ${note}${acked}${exported}</section>`;
    }
    const errors = d.findings.filter((f) => f.severity === "error");
    const blocking = d.findings.filter((f) => f.severity === "blocking");
    const me = S.reviewer, blockedForMe = d.four_eyes.includes(me);
    const err = S.errors[d.id];
    const storyRing = inStory() && STORY_DOC[S.step] === d.key;
    const rejectBtn = `<button type="button" class="btn btn-secondary btn-danger${ring(storyRing && S.step === 3)}" data-reject="${esc(d.id)}">${icon("x")}${S.rejecting === d.id ? "Send rejection" : "Reject"}</button>`;
    const errorLine = err ? `<p class="field-error" role="alert">${icon("alert", "i i-xs")}${esc(err)}</p>` : "";
    if (d.status === "needs_manual_entry" && !d.corrections.length && S.typing !== d.id) {
      return `<section class="decision typebox" aria-label="Decision">
        <div class="decision-head">${icon("pen")}<b>Needs typing in</b></div>
        <p>${esc(whyManual(d))} A person types the values in from the page; another person approves.</p>
        ${reviewerSwitch(d)}${errorLine}
        <div class="decision-actions">${rejectBtn}<button type="button" class="btn btn-primary" data-type="${esc(d.id)}">${icon("pen")}Type it in</button></div>${rejectForm(d)}</section>`;
    }
    if (S.typing === d.id) return typeForm(d);
    if (errors.length) {
      const hard = d.hard_lock;
      return `<section class="decision lockbox" aria-label="Decision">
        <div class="decision-head">${icon("lock")}<b>Approve is locked</b>${stampFor(d, true)}</div>
        <ul class="findings">${[...errors, ...blocking].map((f) => findingLine(f, false)).join("")}</ul>
        <p class="lock-note">${hard ? "Nobody can approve this here, whoever is reviewing. Reject it, with a reason." : "Fix the typed values first."}</p>
        ${reviewerSwitch(d)}${errorLine}
        <div class="decision-actions">${rejectBtn}${!hard && d.corrections.length ? `<button type="button" class="btn btn-secondary" data-type="${esc(d.id)}">${icon("pen")}Change typed values</button>` : ""}
          <button type="button" class="btn btn-primary btn-locked" disabled aria-disabled="true">${icon("lock", "i i-sm")}Approve</button></div>${rejectForm(d)}</section>`;
    }
    const acks = S.acks[d.id] || [];
    // In step 4 the highlight walks through what is left to do: each tick, the note, then Approve.
    const guide = storyRing && S.step === 4;
    const nextTick = guide ? blocking.find((f) => !acks.includes(f.id)) : null;
    const noteShort = (S.notes[d.id] || "").trim().length < 10;
    const sign = blocking.length ? `<p class="decision-lead">Tick each finding you checked, then say why it is fine to pay.</p>
      <ul class="findings">${blocking.map((f) => findingLine(f, true, acks.includes(f.id)).replace('<label class="tick-row">', `<label class="tick-row${ring(nextTick && f.id === nextTick.id)}">`)).join("")}</ul>
      <label class="field-label" for="note-${esc(d.id)}">Why it is fine <span class="muted">(10 characters or more; it goes with the invoice)</span></label>
      <textarea class="input" id="note-${esc(d.id)}" data-note="${esc(d.id)}" rows="2" maxlength="500" placeholder="For example: buyer agreed the extra quantity by phone.">${esc(S.notes[d.id] || "")}</textarea>
      ${inStory() && S.step === 4 && !(S.notes[d.id] || "").trim() ? `<button type="button" class="chip${ring(guide && !nextTick && noteShort)}" data-suggest="${esc(d.id)}">${icon("pen", "i i-xs")}Use: “${esc(NOTE_06)}”</button>` : ""}`
      : `<p class="decision-lead">${icon("check-circle", "i i-sm ok")}No problems found. ${esc((d.findings.find((f) => f.code === "po_matched" || f.code === "po_in_register") || {}).message || "")}</p>`;
    const four = blockedForMe ? `<p class="four-eyes">${icon("user", "i i-sm")}<span>You typed in values on this document, so another reviewer must approve it (four eyes). <button type="button" class="link" data-reviewer="${other(me)}">Switch to ${esc(reviewerName(other(me)))}</button></span></p>` : "";
    const ready = !blocking.length || (blocking.every((f) => acks.includes(f.id)) && (S.notes[d.id] || "").trim().length >= 10);
    const approveRing = storyRing && (S.step === 2 || (S.step === 4 && ready));
    return `<section class="decision${blocking.length ? " signoff" : " clean"}" aria-label="Decision">
      <div class="decision-head">${blocking.length ? icon("pen") : icon("check-circle")}<b>${blocking.length ? `Sign off ${blocking.length} finding${blocking.length > 1 ? "s" : ""} to approve` : "Ready for a decision"}</b></div>
      ${sign}${reviewerSwitch(d)}${four}${errorLine}
      <div class="decision-actions">${rejectBtn}<button type="button" class="btn btn-primary${ring(approveRing)}" data-approve="${esc(d.id)}"${blockedForMe ? " disabled" : ""}>${icon("check")}Approve${inStory() ? "" : " as " + esc(reviewerName(me))}</button></div>
      ${rejectForm(d)}</section>`;
  }
  function typeForm(d) {
    const t = S.typed[d.id] || {};
    const val = (k) => esc(t[k] ?? (d.fields.find((f) => f.name === k)?.value || ""));
    const lines = t.lines || (d.lines.length ? d.lines.map((l) => Object.fromEntries(LINE_KEYS.map((k) => [k, l[k] || ""]))) : [{}]);
    const label = (k) => ({ vendor_name: "Vendor", invoice_number: "Invoice number", invoice_date: "Invoice date", due_date: "Due date", po_number: "PO number", currency: "Currency",
      vendor_iban: "IBAN", vendor_sort_code: "Sort code", vendor_account: "Account number", subtotal: "Subtotal", tax_rate: "Tax rate %", tax_amount: "Tax", total: "Total" }[k]);
    const err = S.errors[d.id];
    return `<section class="decision typeform" aria-label="Type it in">
      <div class="decision-head">${icon("pen")}<b>Type in what the page shows</b></div>
      <p class="muted">Dates as YYYY-MM-DD, amounts as 1500.00. Each value is recorded as typed by ${esc(reviewerName(S.reviewer))}.</p>
      ${d.type_in ? `<button type="button" class="chip" data-fill="${esc(d.id)}">${icon("scan", "i i-xs")}Fill in from the page</button>` : ""}
      <div class="type-grid">${TYPE_FIELDS.map((k) => `<label class="type-field"><span>${label(k)}</span><input class="input mono" data-tf="${k}" value="${val(k)}" maxlength="120" autocomplete="off"></label>`).join("")}</div>
      <div class="type-lines"><div class="type-line type-line-head" aria-hidden="true"><span>Item</span><span>Description</span><span>Qty</span><span>Unit price</span><span>Amount</span></div>
        ${lines.map((l, i) => `<div class="type-line">${LINE_KEYS.map((k) => `<input class="input mono" data-tl="${i}:${k}" aria-label="Line ${i + 1} ${k.replace("_", " ")}" value="${esc(l[k] || "")}" maxlength="120" autocomplete="off">`).join("")}</div>`).join("")}
        <button type="button" class="btn-link" data-addline="${esc(d.id)}">${icon("plus", "i i-xs")}Add a line</button></div>
      ${err ? `<p class="field-error" role="alert">${icon("alert", "i i-xs")}${esc(err)}</p>` : ""}
      <div class="decision-actions"><button type="button" class="btn btn-secondary" data-canceltype="${esc(d.id)}">Cancel</button>
        <button type="button" class="btn btn-primary" data-savetype="${esc(d.id)}">${icon("check")}Save typed values</button></div></section>`;
  }
  function renderReview() {
    const pane = $("#review-pane"), d = current();
    if (!d) {
      pane.innerHTML = `<div class="review-empty">${icon("inbox", "i i-xl")}<h2>Nothing to review yet</h2><p>${inStory() ? "The story brings in the first invoice." : "Pick a document in the inbox, or bring in a sample."}</p></div>`;
      return;
    }
    const docType = d.doc_type === "purchase_order" ? "Purchase order" : d.doc_type === "invoice" ? "Invoice" : "Document";
    const notes = d.findings.filter((f) => (f.severity === "warning" || f.severity === "info") && f.code !== "manual_entry");
    const shown = d.fields.filter((f) => f.value || f.source || f.corrected);
    const fieldRows = shown.map((f) => `<div class="f-row${S.hover === f.name ? " active" : ""}${problemKeys(d).has(f.name) && d.waiting ? " is-problem" : ""}" data-key="${esc(f.name)}" tabindex="0" role="button" aria-label="${esc(f.label)}: ${esc(f.shown || "empty")}${f.source ? ". Read from: " + esc(f.source.text) : ""}">
        <span class="f-name">${esc(f.label)}</span><span class="f-value mono">${esc(f.shown || "empty")}</span>
        <span class="f-source">${f.corrected ? `<span class="typed">${icon("pen", "i i-xs")}Typed in${f.was ? `, was ${esc(f.was)}` : ""}</span>` : f.source ? `<span class="src-mark" aria-hidden="true"></span><span class="mono">${esc(f.source.text)}</span>` : "<span class='muted'>Not printed</span>"}</span></div>`).join("");
    const lineRows = d.lines.map((l, j) => `<tr class="${S.hover === "line:" + j ? "active" : ""}${problemKeys(d).has("line:" + j) && d.waiting ? " is-problem" : ""}" data-key="line:${j}" tabindex="0">
        <td class="mono">${esc(l.sku || "")}</td><td>${esc(l.description || "")}</td><td class="num mono">${esc(l.shown.quantity || "")}</td>
        <td class="num mono">${esc(l.shown.unit_price || "")}</td><td class="num mono">${esc(l.shown.line_total || "")}</td></tr>`).join("");
    const ai = d.ai && d.ai.status === "ok" ? `<section class="block ai-box"><h3 class="block-title">${icon("spark", "i i-sm")}AI reader</h3>
        <p>Claude Haiku 5.5 read this in ${(d.ai.ms / 1000).toFixed(1)} s: ${Number(d.ai.input_tokens || 0).toLocaleString("en-US")} tokens in, ${Number(d.ai.output_tokens || 0).toLocaleString("en-US")} out, <b>${usd(d.ai.cost_usd)}</b>. Code checked that each quoted line is on the page.</p></section>`
      : d.ai ? `<section class="block ai-box muted-box"><h3 class="block-title">${icon("spark", "i i-sm")}AI reader</h3><p>${esc(AI_WORDS[d.ai.status] || "")} Layouts the rules do not know go to a person instead.</p></section>` : "";
    const typed = d.corrections.length ? `<p class="typed-by">${icon("pen", "i i-xs")}Typed in by ${esc(d.corrections[0].by)} at ${esc(d.corrections[0].at)}: ${d.corrections.length} value${d.corrections.length > 1 ? "s" : ""}.</p>` : "";
    pane.innerHTML = `<header class="doc-head">
        <div class="doc-kicker"><span class="q-key mono">${esc(d.key || "")}</span><span>${docType}</span><span class="mono doc-file">${esc(d.file)}</span></div>
        <div class="doc-title-row"><h2 class="doc-vendor">${esc(d.vendor || sample(d.key)?.title || d.file)}</h2>${pillFor(d)}</div>
        ${d.number || d.total ? `<dl class="doc-figures">
          <div><dt>${d.doc_type === "purchase_order" ? "PO" : "Invoice"}</dt><dd class="mono">${esc(d.number || "not read")}</dd></div>
          ${d.doc_type !== "purchase_order" ? `<div><dt>PO</dt><dd class="mono">${esc(d.po_number || "none")}</dd></div>` : ""}
          <div class="doc-total"><dt>Total</dt><dd>${esc(d.total || "not read")}</dd></div></dl>` : ""}
        <div class="doc-badges">${readerBadge(d)}${typed}</div>
      </header>
      ${decision(d)}
      ${shown.length ? `<section class="block"><h3 class="block-title">Read from the PDF</h3><p class="block-hint">${isPhone() ? "Tap a value to see the line it came from." : "Hover a value to see the line it came from on the page."}</p><div class="fields">${fieldRows}</div></section>` : ""}
      ${d.lines.length ? `<section class="block"><h3 class="block-title">Lines</h3><div class="table-wrap"><table class="lines-table"><thead><tr><th>Item</th><th>Description</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Amount</th></tr></thead><tbody>${lineRows}</tbody></table></div></section>` : ""}
      ${notes.length ? `<section class="block"><h3 class="block-title">Also noted</h3><ul class="findings">${notes.map((f) => findingLine(f, false)).join("")}</ul></section>` : ""}
      ${ai}`;
  }

  // ------------------------------------------------------------------ export and audit trail
  function renderBooks() {
    const pane = $("#books-pane"), v = S.view || { exports: [], audit: [], counts: {}, metrics: {} };
    const n = v.counts.approved || 0, m = v.metrics || {};
    const storyRing = S.step === 6 && !(v.exports || []).length ? " ring" : "";
    const batches = (v.exports || []).slice().reverse().map((b, bi) => {
      const idx = v.exports.length - 1 - bi;
      const inv = b.files.find((f) => f.name === "invoices.csv");
      const rows = inv ? csvRows(inv.content).slice(1) : [];
      const headRow = inv ? csvRows(inv.content)[0] : [];
      const col = (r, name) => r[headRow.indexOf(name)] || "";
      return `<div class="batch"><div class="batch-head"><span class="mono">${esc(b.batch)}</span><span class="muted">${esc(b.created_at.slice(11, 16))} · ${b.count} document${b.count > 1 ? "s" : ""}</span></div>
        <ul class="files">${b.files.filter((f) => f.rows || f.name.endsWith(".json")).map((f) => `<li><button type="button" class="file-chip" data-download="${idx}:${esc(f.name)}">${icon("download", "i i-sm")}<span class="mono">${esc(f.name)}</span><small>${f.name.endsWith(".json") ? "all details" : f.rows + " row" + (f.rows > 1 ? "s" : "")}</small></button></li>`).join("")}</ul>
        ${rows.length ? `<div class="table-wrap"><table class="csv-table"><thead><tr><th>Invoice</th><th>Vendor</th><th class="num">Total</th><th>Approved by</th></tr></thead><tbody>${rows.map((r) => `<tr><td class="mono">${esc(col(r, "invoice_number"))}</td><td>${esc(col(r, "vendor_name"))}</td><td class="num mono">${esc(col(r, "total"))}</td><td>${esc(col(r, "approved_by"))}</td></tr>`).join("")}</tbody></table></div>` : ""}</div>`;
    }).join("");
    const audit = (v.audit || []).map((a) => `<li class="${a.outcome === "refused" ? "refused" : ""}"><time class="mono">${esc(a.time)}</time>
      <span class="who">${esc(a.who)}</span><span class="what">${esc(a.text)}${a.doc_label ? ` <span class="mono doc-ref">${esc(a.doc_label)}</span>` : ""}</span></li>`).join("");
    const cost = m.ai_reads ? `<div class="cost-figures"><div><span>AI reads</span><strong>${m.ai_reads}</strong></div><div><span>Tokens in / out</span><strong class="mono">${Number(m.input_tokens).toLocaleString("en-US")} / ${Number(m.output_tokens).toLocaleString("en-US")}</strong></div><div><span>Cost</span><strong>${usd(m.cost_usd)}</strong></div></div>`
      : `<p>No AI reading in this session, so no AI cost. The demo PDFs are read with fixed rules; only a layout the rules do not know uses the AI reader.</p>`;
    const toggle = S.config?.live ? `<div class="who-row"><span>Unknown layouts</span><div class="seg" role="group" aria-label="How unknown layouts are read">
        <button type="button" data-ai="on" aria-pressed="${S.ai}">${icon("spark", "i i-xs")}AI reader</button><button type="button" data-ai="off" aria-pressed="${!S.ai}">Rules only</button></div></div>`
      : `<p class="muted">The AI reader is not switched on in this demo right now. It runs once the demo is deployed with its key.</p>`;
    pane.innerHTML = `<div class="books">
      <button type="button" class="btn-link back-link" data-go="back">${icon("chev-left", "i i-sm")}Back to the inbox</button>
      <section class="card export-card" aria-labelledby="export-title">
        <h2 class="card-title" id="export-title">Export to accounting</h2>
        <p>${n ? `<b>${n} approved document${n > 1 ? "s" : ""}</b> waiting.` : "Nothing approved is waiting."} The export writes CSV and JSON for an accounting import, each document at most once. <b>Export creates files in your browser.</b></p>
        <button type="button" class="btn btn-primary${storyRing}" data-export="1">${icon("download")}${n ? `Export ${n} approved` : "Run the export"}</button>
        ${batches}
      </section>
      <section class="card audit-card" aria-labelledby="audit-title">
        <h2 class="card-title" id="audit-title">Audit trail</h2>
        <p class="muted">Every upload, reading, check, decision and export, with who and when. Refused attempts too.</p>
        ${audit ? `<ol class="audit">${audit}</ol>` : `<p class="pane-empty">Nothing yet.</p>`}
      </section>
      <section class="card cost-card" aria-labelledby="cost-title">
        <h2 class="card-title" id="cost-title">AI reading and cost</h2>${cost}${toggle}
      </section></div>`;
  }
  function csvRows(text) {
    const rows = []; let row = [], cell = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (q) { if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c; }
      else if (c === '"') q = true;
      else if (c === ",") { row.push(cell); cell = ""; }
      else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
      else cell += c;
    }
    if (cell || row.length) { row.push(cell); rows.push(row); }
    return rows;
  }
  function download(spec) {
    const [idx, name] = [Number(spec.split(":")[0]), spec.slice(spec.indexOf(":") + 1)];
    const file = (S.view.exports[idx]?.files || []).find((f) => f.name === name);
    if (!file) return;
    const blob = new Blob([file.content], { type: name.endsWith(".json") ? "application/json" : "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${S.view.exports[idx].batch}-${name}`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  // ------------------------------------------------------------------ rendering and events
  function render() {
    document.body.dataset.main = S.main;
    renderStory(); renderInbox(); renderPaper(); renderReview(); renderBooks();
  }
  function select(id) {
    if (!id) return;
    S.selected = id; S.main = "doc"; S.hover = null; save();
  }
  function setPane(pane, announceIt) {
    S.pane = pane;
    document.body.dataset.pane = pane;
    $$(".switch-row button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pane === pane || (pane === "books" && b.dataset.pane === "inbox"))));
    if (announceIt && isPhone()) announce({ inbox: "Inbox", paper: "The invoice", review: "Review", books: "Export and audit trail" }[pane]);
    renderStory();
  }
  function setHover(key) {
    S.hover = key;
    $$(".mark").forEach((m) => m.classList.toggle("active", !!key && m.dataset.mark.split(" ").includes(key)));
    $$("[data-key]").forEach((r) => r.classList.toggle("active", r.dataset.key === key));
  }
  function readTyped(id) {
    const t = S.typed[id] || {};
    $$("[data-tf]").forEach((x) => { t[x.dataset.tf] = x.value; });
    const lines = [];
    $$("[data-tl]").forEach((x) => { const [i, k] = x.dataset.tl.split(":"); (lines[i] = lines[i] || {})[k] = x.value; });
    t.lines = lines;
    S.typed[id] = t;
    return t;
  }
  async function approveDoc(id) {
    const d = byId(id);
    if (!d) return;
    const blocking = d.findings.filter((f) => f.severity === "blocking").map((f) => f.id);
    const acks = (S.acks[id] || []).filter((a) => blocking.includes(a));
    const note = (S.notes[id] || "").trim();
    delete S.errors[id];
    if (blocking.length && acks.length < blocking.length) { S.errors[id] = "Tick each finding first: each one is a check you looked at."; render(); $(`[data-ack]:not(:checked)`)?.focus(); return; }
    if (blocking.length && note.length < 10) { S.errors[id] = "Write why it is fine to pay, in 10 characters or more."; render(); $(`#note-${id}`)?.focus(); return; }
    const data = await act({ type: "approve", doc: id, by: S.reviewer, ack: acks, note, version: d.version }, "Approving.");
    if (!data) return;
    if (!data.result.ok) { S.errors[id] = data.result.message; render(); announce(data.result.message); return; }
    announce(`Approved by ${reviewerName(S.reviewer)}.`);
    if (inStory()) { if (isPhone()) setPane(storyFocus(), true); renderStory(); revealStory(); }
  }
  async function rejectDoc(id) {
    if (S.rejecting !== id) { S.rejecting = id; render(); $(`#reason-${id}`)?.focus(); return; }
    const reason = (S.reasons[id] || "").trim();
    delete S.errors["reject:" + id];
    if (reason.length < 3) { S.errors["reject:" + id] = "Write a short reason first."; render(); $(`#reason-${id}`)?.focus(); return; }
    const data = await act({ type: "reject", doc: id, by: S.reviewer, reason }, "Rejecting.");
    if (!data) return;
    if (!data.result.ok) { S.errors[id] = data.result.message; render(); return; }
    S.rejecting = null; render();
    announce("Rejected.");
    if (inStory()) { if (isPhone()) setPane(storyFocus(), true); revealStory(); }
  }
  async function saveTyped(id) {
    const d = byId(id), t = readTyped(id);
    const fields = { doc_type: "invoice" };
    TYPE_FIELDS.forEach((k) => { const x = (t[k] || "").trim(); if (x) fields[k] = x; });
    const lines = (t.lines || []).map((l) => Object.fromEntries(LINE_KEYS.map((k) => [k, (l[k] || "").trim() || null]))).filter((l) => Object.values(l).some((x) => x));
    delete S.errors[id];
    const data = await act({ type: "correct", doc: id, by: S.reviewer, fields, lines, note: "Typed in from the page.", version: d.version }, "Saving.");
    if (!data) return;
    if (!data.result.ok) { S.errors[id] = data.result.message; render(); return; }
    S.typing = null; delete S.typed[id]; render();
    announce("Saved. Another reviewer approves it.");
  }
  function revealStory() {
    if (isPhone()) $("#story")?.scrollIntoView({ block: "nearest" });
    if (S.keyboard) $("#story-actions .btn-primary")?.focus({ preventScroll: true });
  }

  function wire() {
    document.addEventListener("keydown", (e) => { if (e.key === "Tab" || e.key === "Enter" || e.key === " ") S.keyboard = true; }, true);
    document.addEventListener("pointerdown", () => { S.keyboard = false; }, true);
    document.addEventListener("click", (e) => {
      const t = e.target.closest("button, [data-key]");
      if (!t || t.disabled) return;
      const ds = t.dataset;
      if (ds.go) go(ds.go);
      else if (ds.doc) { select(ds.doc); render(); if (isPhone()) setPane("review", true); }
      else if (ds.add) { (async () => { await addSample(ds.add); render(); if (isPhone()) setPane("review", true); })(); }
      else if (ds.approve) approveDoc(ds.approve);
      else if (ds.reject) rejectDoc(ds.reject);
      else if (ds.type) { S.typing = ds.type; render(); $("[data-tf]")?.focus(); }
      else if (ds.canceltype) { S.typing = null; delete S.errors[ds.canceltype]; render(); }
      else if (ds.savetype) saveTyped(ds.savetype);
      else if (ds.fill) { const d = byId(ds.fill); if (d?.type_in) { S.typed[d.id] = { ...d.type_in.fields, lines: d.type_in.lines.map((l) => ({ ...l })) }; render(); } }
      else if (ds.addline) { const t2 = readTyped(ds.addline); t2.lines.push({}); render(); }
      else if (ds.suggest) { S.notes[ds.suggest] = NOTE_06; render(); revealTarget(); }
      else if (ds.reviewer) { S.reviewer = ds.reviewer; delete S.errors[S.selected]; save(); render(); }
      else if (ds.ai) { S.ai = ds.ai === "on"; save(); renderBooks(); }
      else if (ds.export) { (async () => { const data = await act({ type: "export" }, "Exporting."); if (data) { toast(data.result.message); announce(data.result.message); if (inStory()) revealStory(); } })(); }
      else if (ds.download) download(ds.download);
      else if (ds.pane) { setPane(ds.pane, false); if (ds.pane !== "books" && S.main === "books" && ds.pane !== "inbox") { S.main = "doc"; render(); } }
      else if (ds.key && t.closest("#review-pane")) {
        setHover(S.hover === ds.key ? null : ds.key);
        if (isPhone() && S.hover) { setPane("paper", true); flashMark(ds.key); }
      }
    });
    document.addEventListener("change", (e) => {
      const box = e.target.closest("[data-ack]");
      if (!box) return;
      const id = S.selected, list = new Set(S.acks[id] || []);
      box.checked ? list.add(box.dataset.ack) : list.delete(box.dataset.ack);
      S.acks[id] = [...list]; delete S.errors[id];
      renderReview(); $(`[data-ack="${box.dataset.ack}"]`)?.focus({ preventScroll: true });
      if (inStory()) revealTarget();
    });
    document.addEventListener("input", (e) => {
      const x = e.target;
      if (x.dataset.note) {
        S.notes[x.dataset.note] = x.value;
        const approveBtn = $(`[data-approve="${x.dataset.note}"]`), d = byId(x.dataset.note);
        if (approveBtn && d && inStory() && S.step === 4) {
          const ready = d.findings.filter((f) => f.severity === "blocking").every((f) => (S.acks[d.id] || []).includes(f.id)) && x.value.trim().length >= 10;
          approveBtn.classList.toggle("ring", ready);
          $("[data-suggest]")?.classList.toggle("ring", !ready && !x.value.trim());
        }
      }
      else if (x.dataset.reason) S.reasons[x.dataset.reason] = x.value;
    });
    document.addEventListener("keydown", (e) => {
      const row = e.target.closest?.("[data-key]");
      if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); row.click(); }
    });
    const review = $("#review-pane");
    review.addEventListener("mouseover", (e) => { const r = e.target.closest("[data-key]"); if (r && !isPhone()) setHover(r.dataset.key); });
    review.addEventListener("mouseleave", () => { if (!isPhone()) setHover(null); });
    review.addEventListener("focusin", (e) => { const r = e.target.closest("[data-key]"); if (r) setHover(r.dataset.key); });
    $("#paper-pane").addEventListener("mouseover", (e) => {
      const m = e.target.closest(".mark");
      if (m && !isPhone()) setHover(m.dataset.mark.split(" ")[0]);
    });
    $("#replay").addEventListener("click", () => go("replay"));
    let resizeTimer = null;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { renderStory(); setPane(S.pane, false); }, 120); });
  }
  function flashMark(key) {
    requestAnimationFrame(() => {
      const m = $$(".mark").find((x) => x.dataset.mark.split(" ").includes(key));
      if (!m) return;
      m.scrollIntoView({ block: "center", behavior: reduced() ? "auto" : "smooth" });
    });
  }

  async function init() {
    wire();
    try {
      const [config, previews] = await Promise.all([fetch("/api/config").then((r) => r.json()), fetch("samples/previews.json").then((r) => r.json()).catch(() => ({}))]);
      S.config = config; S.previews = previews;
    } catch { toast("The demo server is not reachable."); return; }
    const saved = store.get("intake-demo");
    if (saved && saved.session) {
      Object.assign(S, { session: saved.session, step: saved.step ?? 0, selected: saved.selected, reviewer: saved.reviewer || "sara", main: saved.main || "doc", ai: saved.ai !== false });
      try { await step({ type: "view" }); } catch { S.view = null; }
    }
    if (!S.view) { await newSession(); S.step = 0; }
    if (inStory() && S.step !== 0) S.reviewer = "sara";
    for (const d of docs()) S.stamps[d.id] = d.stamp;
    render();
    setPane(isPhone() ? (S.main === "books" ? "books" : storyFocus() === "inbox" ? "inbox" : storyFocus()) : "review", false);
    document.body.classList.add("ready");
  }
  init();
})();
