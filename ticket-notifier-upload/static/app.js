/* Ticket Watch — single-page client. No framework, no build step. */

const CLIENT_ID = (() => {
  let id = localStorage.getItem("tw-client-id");
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
    localStorage.setItem("tw-client-id", id);
  }
  return id;
})();

const api = async (path, options = {}) => {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID, ...(options.headers || {}) },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
  return body;
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = {
  tab: "active",
  monitors: [],
  logs: new Map(),      // monitor id → events[]
  lastEventId: 0,
  editingId: null,
  city: null,
  theater: null,
  seeded: new Set(),    // monitors whose history we already backfilled
};

/* ── views & tabs ───────────────────────────────────────────────────────── */

function showTab(tab) {
  state.tab = tab;
  ["active", "past", "tech", "form"].forEach((v) => { $(`view-${v}`).hidden = v !== tab; });
  document.querySelectorAll(".tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  if (tab === "tech") refreshTechLog();
}

document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => showTab(btn.dataset.tab)));

/* ── combo box (city + theater) ─────────────────────────────────────────── */

function combo({ input, list, fetcher, render, onPick, onClear }) {
  let items = [], cursor = -1, timer = null, token = 0;

  const close = () => { list.hidden = true; cursor = -1; };
  const draw = () => {
    list.innerHTML = "";
    items.forEach((item, i) => {
      const li = document.createElement("li");
      li.innerHTML = render(item);
      li.className = i === cursor ? "active" : "";
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(item); });
      list.appendChild(li);
    });
    list.hidden = items.length === 0;
  };
  const pick = (item) => { input.value = item.name; close(); onPick(item); };
  const lookup = async () => {
    const mine = ++token;
    try {
      const found = await fetcher(input.value.trim());
      if (mine !== token) return;          // a newer keystroke already won
      items = found; draw();
    } catch { close(); }
  };

  input.addEventListener("input", () => { onClear(); clearTimeout(timer); timer = setTimeout(lookup, 180); });
  input.addEventListener("focus", lookup);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { cursor = Math.min(cursor + 1, items.length - 1); draw(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { cursor = Math.max(cursor - 1, 0); draw(); e.preventDefault(); }
    else if (e.key === "Enter" && cursor >= 0) { pick(items[cursor]); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
}

combo({
  input: $("city-input"), list: $("city-list"),
  fetcher: async (q) => (await api(`/api/cities?q=${encodeURIComponent(q)}`)).cities,
  render: (c) => `${esc(c.name)}${c.popular ? ' <span class="meta">popular</span>' : ""}`,
  onPick: (c) => {
    state.city = c;
    const t = $("theater-input");
    t.disabled = false; t.value = ""; t.placeholder = `Search theaters in ${c.name}…`;
    state.theater = null;
    setTheaterHint("");
  },
  onClear: () => {
    state.city = null; state.theater = null;
    const t = $("theater-input");
    t.disabled = true; t.value = ""; t.placeholder = "Pick a city first";
  },
});

let theaterPoll = null;

combo({
  input: $("theater-input"), list: $("theater-list"),
  fetcher: async (q) => {
    if (!state.city) return [];
    const r = await api(`/api/theaters?city=${encodeURIComponent(state.city.slug)}&q=${encodeURIComponent(q)}`);

    // A city the background crawl has not reached loads on first use. The
    // request returns straight away, so poll rather than hanging the field.
    clearTimeout(theaterPoll);
    if (r.status === "ready") {
      setTheaterHint("");
      $("theater-input").placeholder = `Search theaters in ${state.city.name}…`;
    } else {
      setTheaterHint(r.message || "Loading theaters…", r.status === "error" ? "err" : "");
      $("theater-input").placeholder = "Loading theaters…";
      theaterPoll = setTimeout(() => $("theater-input").dispatchEvent(new Event("focus")), 2500);
    }
    return r.theaters;
  },
  render: (t) => `${esc(t.name)}<span class="meta">${esc((t.address || "").slice(0, 60))}</span>`,
  onPick: (t) => { state.theater = t; clearTimeout(theaterPoll); setTheaterHint(""); },
  onClear: () => { state.theater = null; },
});

function setTheaterHint(text, kind = "") {
  const el = $("theater-hint");
  el.textContent = text;
  el.className = kind === "err" ? "warn-text" : "";
  el.hidden = !text;
}

/* ── date field: picker only, no typing, no past dates ──────────────────── */

const dateInput = $("date-input");
dateInput.min = new Date().toLocaleDateString("en-CA");      // local YYYY-MM-DD
// Open the native calendar on click/focus. The field stays editable in the
// DOM sense — `readonly` would remove Chrome's calendar button entirely — so
// typing is blocked by swallowing keys instead.
const openPicker = () => { try { dateInput.showPicker(); } catch { /* unsupported */ } };
dateInput.addEventListener("click", openPicker);
dateInput.addEventListener("focus", openPicker);
dateInput.addEventListener("keydown", (e) => {
  if (!["Tab", "Escape", "Enter"].includes(e.key)) e.preventDefault();
});

/* phone: digits only, 10 max, behind a fixed +91 */
$("phone-input").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/\D/g, "").slice(0, 10);
});

/* ── form ───────────────────────────────────────────────────────────────── */

function openForm(monitor = null) {
  state.editingId = monitor?.id ?? null;
  $("form-title").textContent = monitor ? "Edit alert" : "New alert";
  $("save-btn").textContent = monitor ? "Save changes" : "Start monitoring";
  $("form-error").textContent = "";

  if (monitor) {
    state.city = { slug: monitor.city_slug, name: monitor.city_name };
    state.theater = { code: monitor.venue_code, name: monitor.venue_name };
    $("city-input").value = monitor.city_name;
    $("theater-input").value = monitor.venue_name;
    $("theater-input").disabled = false;
    $("movie-input").value = monitor.movie;
    const d = monitor.target_date;
    dateInput.value = `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}`;
    $("interval-input").value = String(monitor.interval_sec);
    $("phone-input").value = monitor.phone.replace(/^\+91/, "");
  } else {
    state.city = null; state.theater = null;
    $("city-input").value = ""; $("theater-input").value = "";
    $("theater-input").disabled = true;
    $("movie-input").value = ""; dateInput.value = ""; $("phone-input").value = "";
  }
  showTab("form");
}

$("new-btn").addEventListener("click", () => openForm());
$("cancel-btn").addEventListener("click", () => showTab("active"));

$("watch-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("form-error");
  err.textContent = "";
  if (!state.city) return (err.textContent = "Choose a city from the list.");
  if (!state.theater) return (err.textContent = "Choose a theater from the list.");
  if (!dateInput.value) return (err.textContent = "Pick a booking date.");
  if (!/^[6-9]\d{9}$/.test($("phone-input").value))
    return (err.textContent = "Enter a 10-digit mobile number starting with 6-9.");

  const btn = $("save-btn");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Saving…";
  const payload = {
    city_slug: state.city.slug,
    venue_code: state.theater.code,
    movie: $("movie-input").value.trim(),
    target_date: dateInput.value,
    interval_sec: Number($("interval-input").value),
    phone: $("phone-input").value,
  };
  try {
    if (state.editingId) {
      await api(`/api/monitors/${state.editingId}`, { method: "PATCH", body: JSON.stringify(payload) });
      state.seeded.delete(state.editingId);
    } else {
      await api("/api/monitors", { method: "POST", body: JSON.stringify(payload) });
    }
    await refreshMonitors();
    showTab("active");
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false; btn.textContent = label;
  }
});

/* ── monitor cards ──────────────────────────────────────────────────────── */

const EMPTY_ACTIVE = `
  <div class="empty-head">
    <svg class="hero-art" viewBox="0 0 230 124" aria-hidden="true">
      <ellipse cx="112" cy="110" rx="92" ry="8" class="hero-shadow"/>
      <g transform="rotate(-6 74 64)">
        <!-- ticket drawn as one path: notches are cut out of the outline -->
        <path class="hero-ticket" d="M26 34h92a10 10 0 0 1 10 10v8a9 9 0 0 0 0 18v8a10 10 0 0 1-10 10H26a10 10 0 0 1-10-10v-8a9 9 0 0 0 0-18v-8a10 10 0 0 1 10-10Z"/>
        <path class="hero-perf" d="M96 40v8M96 54v8M96 68v8M96 82v6"/>
        <path class="hero-line" d="M32 52h40M32 64h26"/>
        <path class="hero-line faint" d="M32 76h33"/>
      </g>
      <g>
        <rect x="160" y="28" width="46" height="76" rx="10" class="hero-phone"/>
        <rect x="167" y="38" width="32" height="48" rx="5" class="hero-screen"/>
        <circle cx="183" cy="95" r="3" class="hero-dot"/>
        <path d="M213 44c6 7.5 6 20 0 27.5" class="hero-ring"/>
        <path d="M222 35c10 12.5 10 32 0 44.5" class="hero-ring faint"/>
      </g>
    </svg>
    <h2 class="empty-title">No alerts running right now</h2>
    <p class="empty-close">
      <strong>Go do your thing. The agent calls when tickets open.</strong>
      Start one with <em>+ New alert</em>, top right.
    </p>
  </div>

  <div class="cmp-wrap">
    <svg class="cmp" viewBox="0 0 644 190" role="img"
         aria-label="Without an alert you refresh all night and it sells out; with an alert on duty your phone rings and you book.">
      <rect class="cmp-pane" x="1" y="1" width="306" height="186" rx="12"/>
      <text class="cmp-label" x="18" y="28">On your own</text>
      <text class="cmp-time" x="18" y="53">11:02 PM</text>
      <use class="cmp-icon" href="#i-refresh" x="80" y="44" width="12" height="12"/>
      <text class="cmp-act" x="97" y="53">you refresh</text>
      <rect class="cmp-chip plain" x="238" y="42" width="50" height="17" rx="8.5"/>
      <text class="cmp-chip-text plain" x="263.0" y="53.5" text-anchor="middle">not yet</text>
      <line class="cmp-rule" x1="18" y1="68" x2="288" y2="68"/>
      <text class="cmp-time" x="18" y="87">02:14 AM</text>
      <use class="cmp-icon" href="#i-refresh" x="80" y="78" width="12" height="12"/>
      <text class="cmp-act" x="97" y="87">you refresh<tspan class="cmp-aside"> (again)</tspan></text>
      <rect class="cmp-chip plain" x="238" y="76" width="50" height="17" rx="8.5"/>
      <text class="cmp-chip-text plain" x="263.0" y="87.5" text-anchor="middle">not yet</text>
      <line class="cmp-rule" x1="18" y1="102" x2="288" y2="102"/>
      <text class="cmp-time" x="18" y="121">07:30 AM</text>
      <use class="cmp-icon" href="#i-refresh" x="80" y="112" width="12" height="12"/>
      <text class="cmp-act" x="97" y="121">you refresh<tspan class="cmp-aside"> (still)</tspan></text>
      <rect class="cmp-chip plain" x="238" y="110" width="50" height="17" rx="8.5"/>
      <text class="cmp-chip-text plain" x="263.0" y="121.5" text-anchor="middle">not yet</text>
      <line class="cmp-rule" x1="18" y1="136" x2="288" y2="136"/>
      <text class="cmp-time" x="18" y="155">09:12 AM</text>
      <text class="cmp-act" x="80" y="155">you're in a meeting</text>
      <rect class="cmp-chip bad" x="234" y="144" width="54" height="17" rx="8.5"/>
      <text class="cmp-chip-text bad" x="261.0" y="155.5" text-anchor="middle">sold out</text>
      <rect class="cmp-pane win" x="338" y="1" width="306" height="186" rx="12"/>
      <use class="cmp-icon win" href="#i-bolt" x="356" y="19" width="11" height="11"/>
      <text class="cmp-label win" x="371" y="28">Agent on duty</text>
      <text class="cmp-time" x="356" y="53">11:02 PM</text>
      <text class="cmp-act" x="418" y="53">agent checks</text>
      <rect class="cmp-chip plain" x="576" y="42" width="50" height="17" rx="8.5"/>
      <text class="cmp-chip-text plain" x="601.0" y="53.5" text-anchor="middle">not yet</text>
      <line class="cmp-rule win" x1="356" y1="68" x2="626" y2="68"/>
      <text class="cmp-time" x="356" y="87">02:14 AM</text>
      <text class="cmp-act" x="418" y="87">agent checks</text>
      <rect class="cmp-chip plain" x="576" y="76" width="50" height="17" rx="8.5"/>
      <text class="cmp-chip-text plain" x="601.0" y="87.5" text-anchor="middle">not yet</text>
      <line class="cmp-rule win" x1="356" y1="102" x2="626" y2="102"/>
      <text class="cmp-time" x="356" y="121">07:30 AM</text>
      <text class="cmp-act" x="418" y="121">agent checks</text>
      <rect class="cmp-chip plain" x="576" y="110" width="50" height="17" rx="8.5"/>
      <text class="cmp-chip-text plain" x="601.0" y="121.5" text-anchor="middle">not yet</text>
      <line class="cmp-rule win" x1="356" y1="136" x2="626" y2="136"/>
      <text class="cmp-time" x="356" y="155">09:12 AM</text>
      <use class="cmp-icon" href="#i-phone" x="418" y="146" width="12" height="12"/>
      <text class="cmp-act strong" x="435" y="155">your phone rings</text>
      <rect class="cmp-chip good" x="578" y="144" width="48" height="17" rx="8.5"/>
      <text class="cmp-chip-text good" x="602.0" y="155.5" text-anchor="middle">booked</text>
    </svg>
  </div>`;

const prettyDate = (yyyymmdd) =>
  new Date(`${yyyymmdd.slice(0, 4)}-${yyyymmdd.slice(4, 6)}-${yyyymmdd.slice(6)}`)
    .toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

function logHtml(mid) {
  const events = state.logs.get(mid) || [];
  if (!events.length) return '<div class="line quiet">No activity yet.</div>';
  return events.map((e) =>
    `<div class="line"><time>${esc(e.ts.slice(11, 16))}</time>` +
    `<span class="${esc(e.level)}">${esc(e.message)}</span></div>`).join("");
}

function cardHtml(m) {
  const mins = Math.round(m.interval_sec / 60);
  const every = mins >= 60 ? `${mins / 60} h` : `${mins} min`;
  const chips = [
    ["theater",  `<strong>${esc(m.venue_name)}</strong>`],
    ["pin",      esc(m.city_name)],
    ["calendar", prettyDate(m.target_date)],
    ["clock",    `every ${every} · ${m.checks} check${m.checks === 1 ? "" : "s"}`],
    ["phone",    esc(m.phone)],
  ];
  if (m.matched_info) chips.push(["bell", `found: <strong>${esc(m.matched_info)}</strong>`]);
  return `
    <div class="watch" data-id="${m.id}">
      <div class="watch-head">
        <div>
          <div class="watch-title">${esc(m.movie)} <span class="pill ${esc(m.status)}">${esc(m.status)}</span></div>
          <div class="watch-meta">${chips.map(([icon, text]) =>
            `<span class="mchip"><svg class="i"><use href="#i-${icon}"/></svg>${text}</span>`).join("")}</div>
        </div>
        <div class="watch-actions">
          ${m.status === "running"
            ? `<button class="ghost" data-act="edit">Edit</button>
               <button class="ghost" data-act="stop">Stop</button>`
            : `<button class="ghost" data-act="reuse">Reuse</button>`}
          <button class="ghost danger" data-act="delete">Delete</button>
        </div>
      </div>
      <div class="watch-log" data-log="${m.id}">${logHtml(m.id)}</div>
    </div>`;
}

function renderLists() {
  const active = state.monitors.filter((m) => m.status === "running");
  const past = state.monitors.filter((m) => m.status !== "running");
  $("count-active").textContent = active.length;
  $("count-past").textContent = past.length;

  $("list-active").innerHTML = active.length ? active.map(cardHtml).join("") : EMPTY_ACTIVE;
  $("list-past").innerHTML = past.length
    ? past.map(cardHtml).join("")
    : `<div class="card empty-state"><svg class="empty-glyph"><use href="#i-bell"/></svg>
       <h2 class="empty-title">Nothing here yet</h2>
       <p class="muted-small">Alerts land here once they have caught your movie —
       or when you stop them yourself. The full log stays with each one.</p></div>`;

  document.querySelectorAll(".watch-log").forEach((el) => {
    el.scrollTop = el.scrollHeight;
    // fade the top edge only when there is history above the fold, so a
    // part-cut first line reads as "scroll up", not as a broken card
    el.classList.toggle("is-scrolled", el.scrollTop > 2);
  });
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const id = Number(btn.closest(".watch").dataset.id);
  const monitor = state.monitors.find((m) => m.id === id);
  const act = btn.dataset.act;

  if (act === "edit") return openForm(monitor);
  if (act === "reuse") return openForm({ ...monitor, id: null });
  if (act === "stop") {
    await api(`/api/monitors/${id}/stop`, { method: "POST" });
    return refreshMonitors();
  }
  if (act === "delete") {
    if (!confirm(`Delete the alert for “${monitor.movie}”? Its log goes too.`)) return;
    await api(`/api/monitors/${id}`, { method: "DELETE" });
    state.logs.delete(id); state.seeded.delete(id);
    return refreshMonitors();
  }
});

/* ── data refresh ───────────────────────────────────────────────────────── */

async function refreshMonitors() {
  const { monitors } = await api("/api/monitors");
  state.monitors = monitors;
  // backfill history for watches we have not seen before
  await Promise.all(monitors.filter((m) => !state.seeded.has(m.id)).map(async (m) => {
    state.seeded.add(m.id);
    try {
      const { events } = await api(`/api/monitors/${m.id}/events?limit=60`);
      state.logs.set(m.id, events);
      state.lastEventId = Math.max(state.lastEventId, ...events.map((e) => e.id), 0);
    } catch { /* monitor vanished */ }
  }));
  renderLists();
}

async function refreshEvents() {
  const { events } = await api(`/api/events?after=${state.lastEventId}`);
  if (!events.length) return;
  let touched = false;
  events.forEach((ev) => {
    state.lastEventId = Math.max(state.lastEventId, ev.id);
    if (ev.monitor_id == null) return;
    const bucket = state.logs.get(ev.monitor_id) || [];
    bucket.push(ev);
    state.logs.set(ev.monitor_id, bucket.slice(-80));
    touched = true;
  });
  if (touched) await refreshMonitors();     // statuses and check counts move too
}

async function refreshTechLog() {
  if (state.tab !== "tech") return;
  const { lines } = await fetch("/api/debug/logs?n=120").then((r) => r.json());
  const box = $("tech-log");
  box.textContent = lines.join("\n");
  box.scrollTop = box.scrollHeight;
}

/* ── boot ───────────────────────────────────────────────────────────────── */

(async function boot() {
  const cfg = await api("/api/config");
  $("interval-input").innerHTML = cfg.intervals
    .map((i) => `<option value="${i.value}"${i.value === 300 ? " selected" : ""}>${i.label}</option>`)
    .join("");
  if (!cfg.calls_configured) $("call-warning").hidden = false;

  await refreshMonitors();
  await refreshEvents();
  setInterval(refreshEvents, 3000);
  setInterval(refreshTechLog, 5000);
})();
