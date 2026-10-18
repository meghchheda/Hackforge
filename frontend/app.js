/* Reserve Juggler AI — single-page front end (no build step). Rupee palette, decluttered layout. */
const S = { meta: null, t: 0, user: null, charts: [], page: null, replayTimer: null, follow: false, touched: false, live: null };
const can = (...roles) => !!S.user && (S.user.roles.includes("admin") || roles.some((r) => S.user.roles.includes(r)));

const PAGES = [
  { id: "overview", title: "Command Center", head: "Treasury Terminal", render: pageOverview, asof: true },
  { id: "forecast", title: "Forecast", render: pageForecast },
  { id: "ladder", title: "Funding Ladder", render: pageLadder, asof: true },
  { id: "nightlock", title: "Night-Lock", render: pageNightlock, asof: true },
  { id: "allocation", title: "Allocation", render: pageAllocation, asof: true },
  { id: "stress", title: "Stress Lab", render: pageStress, asof: true },
  { id: "backtest", title: "Backtest", render: pageBacktest },
  { id: "replay", title: "Replay", render: pageReplay },
  { id: "trust", title: "Trust", render: pageTrust },
  { id: "data", title: "Data", head: "Data Sources", render: pageData },
];

/* ---------- helpers ---------- */
const $ = (s, el = document) => el.querySelector(s);
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function money(x, d = 1) {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  const a = Math.abs(x), s = x < 0 ? "−" : "";
  if (a >= 1e9) return `${s}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${s}$${(a / 1e6).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d })}M`;
  if (a >= 1e3) return `${s}$${(a / 1e3).toFixed(1)}K`;
  return `${s}$${a.toFixed(0)}`;
}
const pct = (x, d = 1) => (x === null || x === undefined ? "—" : `${(x * 100).toFixed(d)}%`);
const badge = (k) => `<span class="badge b-${k.toLowerCase()}" title="${{ O: "Observed in dataset", M: "Modeled estimate", A: "Assumption or policy" }[k]}">${k}</span>`;
const ts = (t) => S.meta.ts[t];
const nice = (s) => { const [d, h] = s.split(" "); const m = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+d.slice(5, 7) - 1]; return `${+d.slice(8)} ${m} · ${h}`; };

async function api(path, opts = {}) {
  // same-origin cookie session; the custom header lets the server refuse cross-site form posts
  const r = await fetch(path, { credentials: "same-origin", ...opts,
    headers: { "Content-Type": "application/json", "X-Requested-With": "rj", ...(opts.headers || {}) } });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    if (r.status === 401 && S.user) { S.user = null; Auth.expired(); }
    const err = new Error(msg); err.status = r.status; throw err;
  }
  return r.json();
}
function toast(msg) {
  const el = $("#toast"); el.textContent = msg; el.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.remove("show"), 2800);
}
function chart(id, cfg) { const c = new Chart(document.getElementById(id), cfg); S.charts.push(c); return c; }
function killCharts() { S.charts.forEach((c) => c.destroy()); S.charts = []; }
function loading(el, text = "Computing…") { el.innerHTML = `<div class="loading"><span class="spin"></span>${text}</div>`; }
function setupChartDefaults() {
  Chart.defaults.font.family = '"JetBrains Mono", ui-monospace, monospace';
  Chart.defaults.font.size = 11;
  Chart.defaults.font.weight = 600;
  Chart.defaults.color = "#000";
  Chart.defaults.borderColor = "rgba(0,0,0,0.12)";
  Chart.defaults.plugins.legend.display = false;
  Chart.defaults.plugins.tooltip.mode = "index";
  Chart.defaults.plugins.tooltip.intersect = false;
  Chart.defaults.plugins.tooltip.backgroundColor = "#fff";
  Chart.defaults.plugins.tooltip.titleColor = "#000";
  Chart.defaults.plugins.tooltip.bodyColor = "#000";
  Chart.defaults.plugins.tooltip.borderColor = "#000";
  Chart.defaults.plugins.tooltip.borderWidth = 2;
  Chart.defaults.plugins.tooltip.cornerRadius = 0;
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.animation = false;
  Chart.defaults.elements.point.radius = 0;
  Chart.defaults.elements.line.borderWidth = 2.5;
  Chart.defaults.elements.bar.borderWidth = 2;
  Chart.defaults.elements.bar.borderColor = "#000";
}
// In the light theme the "main series" is drawn in ink, since poster yellow is too faint as a thin line.
const C = () => ({ ink: css("--ink"), ink2: css("--ink-2"), ink3: "#8A8A8A", line: css("--line"), accent: css("--chart-main"), accentFill: css("--accent"), accentSoft: css("--accent-soft"),
  good: css("--good"), goodFill: css("--good-fill"), goodSoft: css("--good-soft"), warn: css("--warn"), warnSoft: css("--warn-soft"), bad: css("--bad"), badSoft: css("--bad-soft"),
  d1: css("--data1"), d1Fill: css("--data1-fill"), d1Soft: css("--data1-soft"), d2: css("--data2"), d2Fill: css("--data2-fill"), d3: css("--data3"), grey: css("--grey-soft") });
const axis = (extra = {}) => ({ grid: { color: "rgba(0,0,0,0.1)" }, border: { display: true, color: "#000", width: 2 }, ...extra });
const xAxis = (n = 8) => ({ grid: { display: false }, border: { display: true, color: "#000", width: 2 }, ticks: { maxTicksLimit: n } });
const legend = (items) => `<div class="legend-row" style="font-size:12px">${items.map(([c, n, dash]) => `<span><span style="width:16px;height:${dash ? 0 : 4}px;${dash ? `border-top:3px dashed ${c}` : `background:${c}`}"></span>${n}</span>`).join("")}</div>`;
const hero = (eyebrow, title, lead, side = "") => `<section class="hero"><div class="copy"><div class="eyebrow">${eyebrow}</div><h1>${title}</h1>${lead ? `<p>${lead}</p>` : ""}</div>${side ? `<div class="row">${side}</div>` : ""}</section>`;
const fig = (k, v, d = "", cls = "") => `<div class="fig"><div class="k">${k}</div><div class="v ${cls}">${v}</div>${d ? `<div class="d">${d}</div>` : ""}</div>`;
const label = (k) => ({ expected_peak_outflow: "Expected peak outflow", uncertainty_addon: "Uncertainty add-on", momentum: "Flow momentum",
  committed_payments: "Committed payments", operational_buffer: "Operational buffer", minimum_cash: "Minimum cash" })[k] || k;

/* ---------- shell ---------- */
/* ---------- money-rain loader: visible only while something is really loading ---------- */
const NOTE_SVG = `<svg viewBox="0 0 100 56" aria-hidden="true"><path d="M4 10 C30 3 62 13 96 4 L96 46 C62 55 32 45 4 52 Z" fill="#8DD630" stroke="#000" stroke-width="3.5" stroke-linejoin="round"></path>
  <path d="M13 15 C34 10 61 18 87 11 L87 41 C62 48 36 40 13 46 Z" fill="none" stroke="#3FAE34" stroke-width="4" stroke-linejoin="round"></path>
  <ellipse cx="50" cy="28.5" rx="11.5" ry="12.5" fill="#3FAE34"></ellipse><circle cx="27" cy="28" r="3.6" fill="#fff"></circle><circle cx="73" cy="29" r="3.6" fill="#fff"></circle></svg>`;
const Boot = {
  token: 0,
  el: () => document.getElementById("boot"),
  rain() {
    const box = document.getElementById("bootRain");
    if (!box || box.childElementCount) return;
    const n = window.innerWidth < 640 ? 12 : 24, frag = document.createDocumentFragment(), r = (a, b) => a + Math.random() * (b - a);
    for (let i = 0; i < n; i++) {
      const depth = Math.random(), note = document.createElement("div");      // nearer notes: bigger, faster, sharper
      note.className = "bill";
      const v = { "--x": `${r(-4, 96).toFixed(1)}%`, "--y": `${r(4, 88).toFixed(1)}%`, "--s": (0.55 + depth * 0.75).toFixed(2), "--o": (0.55 + depth * 0.45).toFixed(2),
        "--dur": `${(11 - depth * 5 + r(-1, 1)).toFixed(2)}s`, "--delay": `${(-r(0, 11)).toFixed(2)}s`, "--fl": `${r(0.9, 1.8).toFixed(2)}s`,
        "--r0": `${r(-60, 60).toFixed(0)}deg`, "--r1": `${r(-240, 240).toFixed(0)}deg`, "--dx": `${r(-14, 14).toFixed(1)}vw` };
      Object.entries(v).forEach(([k, val]) => note.style.setProperty(k, val));
      note.innerHTML = `<i>${NOTE_SVG}</i>`;
      frag.appendChild(note);
    }
    box.appendChild(frag);
  },
  show(msg, soft = false) {
    const el = Boot.el(); if (!el) return;
    Boot.rain();
    Boot.status(msg);
    el.classList.toggle("soft", soft);
    el.classList.remove("done");
    $("#page").setAttribute("aria-busy", "true");
  },
  status(msg) { const m = document.getElementById("bootMsg"); if (m && msg) m.textContent = msg; },
  hide() {
    const el = Boot.el(); if (!el) return;
    el.classList.add("done");
    $("#page").removeAttribute("aria-busy");
  },
  fail(msg, retry) {
    Boot.status(msg);
    const card = Boot.el().querySelector(".boot-card");
    if (!card.querySelector(".retry")) card.insertAdjacentHTML("beforeend", `<button class="btn sm retry" type="button">Try again</button>`);
    card.querySelector(".retry").onclick = () => { card.querySelector(".retry").remove(); retry(); };
  },
};
Boot.rain();   // the loader is in the HTML, so it shows before any script runs; fill in the notes at once

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- sign-in: the form lives on the loader card, over the money rain ---------- */
const Auth = {
  async status() {
    for (let i = 0; ; i++) {
      try { const r = await fetch("/api/auth/status", { credentials: "same-origin" }); if (r.ok) return r.json(); }
      catch (_) { /* server not up yet */ }
      if (i >= 60) throw new Error("The server is not answering");
      Boot.status(i < 2 ? "Connecting to the engine…" : "Waiting for the server…");
      await sleep(1500);
    }
  },
  async gate() {
    const st = await Auth.status();
    if (st.user) { S.user = st.user; return; }
    S.user = await Auth.form(st.setup_needed);
  },
  form(setup) {
    Boot.show("", false);
    const card = Boot.el().querySelector(".boot-card");
    const keep = card.innerHTML;
    card.classList.add("form");
    card.innerHTML = `<div class="logo">RESERVE JUGGLER <small>AI</small></div>
      <form class="auth" id="authForm" autocomplete="on">
        <h2>${setup ? "Create the first admin" : "Sign in"}</h2>
        <p class="hint">${setup ? "No accounts exist yet. This account can add the rest of the team and their roles." : "Use the account your admin gave you."}</p>
        <label>Username<input name="username" autocomplete="username" required minlength="2" maxlength="40" autocapitalize="none" spellcheck="false"></label>
        <label>Password<input name="password" type="password" autocomplete="${setup ? "new-password" : "current-password"}" required ${setup ? 'minlength="10"' : ""}></label>
        ${setup ? '<label>Repeat password<input name="again" type="password" autocomplete="new-password" required minlength="10"></label><p class="hint">At least 10 characters, not containing the username.</p>' : ""}
        <div class="err" id="authErr" role="alert"></div>
        <button class="btn primary" type="submit">${setup ? "Create account and sign in" : "Sign in"}</button>
      </form>`;
    card.querySelector("input").focus();
    return new Promise((resolve) => {
      $("#authForm").onsubmit = async (e) => {
        e.preventDefault();
        const f = Object.fromEntries(new FormData(e.target));
        if (setup && f.password !== f.again) { $("#authErr").textContent = "The passwords don't match"; return; }
        const btn = e.target.querySelector("button"); btn.disabled = true; $("#authErr").textContent = "";
        try {
          const r = await api(setup ? "/api/auth/setup" : "/api/auth/login", { method: "POST", body: JSON.stringify({ username: f.username, password: f.password }) });
          card.classList.remove("form"); card.innerHTML = keep;
          resolve(r.user);
        } catch (err) { $("#authErr").textContent = err.message; btn.disabled = false; }
      };
    });
  },
  expired() { toast("Your session ended. Sign in again."); setTimeout(() => location.reload(), 1200); },
  async logout() { try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {} location.reload(); },
};

async function fetchMetaWithRetry() {
  // the engine builds in the background after a restart or a dataset switch; the API answers 503 until then
  for (let i = 0; ; i++) {
    try { return await api("/api/meta"); }
    catch (e) {
      if (e.status && e.status !== 503) throw e;
      if (i >= 240) throw e;
      Boot.status(e.status === 503 ? e.message : "Connecting to the engine…");
      await sleep(1500);
    }
  }
}

async function boot() {
  setupChartDefaults();
  await Auth.gate();
  Boot.show("Loading treasury data…");
  S.meta = await fetchMetaWithRetry();
  Boot.status("Opening the Command Center…");
  S.t = S.meta.default_t;
  S.follow = S.meta.live;
  renderWho();
  $("#nav").innerHTML = PAGES.map((p) => `<a href="#/${p.id}" data-id="${p.id}">${p.title}</a>`).join("");
  $("#asofHour").innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${String(h).padStart(2, "0")}">${String(h).padStart(2, "0")}:00</option>`).join("");
  syncAsof();
  const onAsof = () => {
    const i = S.meta.ts.indexOf(`${$("#asofDate").value} ${$("#asofHour").value}:00`);
    if (i >= 0) { S.t = i; S.follow = S.meta.live && i === S.meta.ts.length - 1; route(); }
    else toast(`Pick an hour between ${nice(S.meta.ts[0])} and ${nice(S.meta.ts[S.meta.ts.length - 1])}`);
  };
  $("#asofDate").addEventListener("change", onAsof);
  $("#asofHour").addEventListener("change", onAsof);
  window.addEventListener("hashchange", route);
  initCopilot();
  route();
  setInterval(pollLive, 8000);
}
function renderWho() {
  const u = S.user;
  $("#who").innerHTML = `<div><b>${esc(u.username)}</b><small>${esc(u.roles.join(", "))}</small></div><button class="btn sm" type="button" id="signOut">Sign out</button>`;
  $("#signOut").onclick = Auth.logout;
}
function syncAsof() {
  const [d, h] = ts(S.t).split(" ");
  $("#asofDate").min = S.meta.ts[0].slice(0, 10); $("#asofDate").max = S.meta.ts[S.meta.ts.length - 1].slice(0, 10);
  $("#asofDate").value = d; $("#asofHour").value = h.slice(0, 2);
}
const modeLabel = () => (S.meta.live ? "Live" : S.meta.source && S.meta.source.id !== "demo" ? "Your data" : "Simulation");

/* ---------- live mode: follow new rows as they arrive ---------- */
async function pollLive() {
  if (!S.user || document.hidden) return;
  let st;
  try { st = await api("/api/live/status"); } catch (_) { return; }
  S.live = st;
  if (st.version === S.meta.version) {
    if (S.meta.live) liveBadge(st);
    return;
  }
  const atEnd = S.t === S.meta.ts.length - 1;
  try { S.meta = await api("/api/meta"); } catch (_) { return; }
  if (S.follow || atEnd) S.t = S.meta.ts.length - 1;
  if (S.meta.source.id !== st.dataset?.id) return;
  const quiet = !document.activeElement || !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);
  if ((S.follow || atEnd) && !S.touched && quiet && ["overview", "nightlock", "ladder", "allocation"].includes(S.page)) {
    S.routeQuiet = true; route();
  } else {
    toast(`New data up to ${nice(S.meta.ts[S.meta.ts.length - 1])}. Reopen the screen to update.`);
    syncAsof();
  }
}
function liveBadge(st) {
  const page = PAGES.find((p) => p.id === S.page);
  if (st.stale) setEngine(false, "Feed stale · conservative", st.stale_reason);
  else if (page && page.asof) setEngine(true, st.building ? "Updating models" : "Live feed", `Live · as of ${nice(ts(S.t))}${st.last_ingest_age_s != null ? ` · last row ${Math.round(st.last_ingest_age_s)} s ago` : ""}`);
}
function route() {
  const id = (location.hash.replace("#/", "") || "overview").split("?")[0];
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  S.page = page.id;
  S.touched = false;
  const quiet = S.routeQuiet; S.routeQuiet = false;
  if (S.replayTimer) { cancelAnimationFrame(S.replayTimer); clearInterval(S.replayTimer); S.replayTimer = null; }
  killCharts();
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.id === page.id));
  const words = (page.head || page.title).split(" ");
  $("#title").innerHTML = `${esc(words[0])}${words.length > 1 ? ` <span>${esc(words.slice(1).join(" "))}</span>` : ""}`;
  setEngine(true, S.meta.live ? "Live feed" : "Engine active", page.asof ? `${modeLabel()} · as of ${nice(ts(S.t))}` : `${modeLabel()} · fixed window`);
  if (S.live && S.meta.live) liveBadge(S.live);
  $("#asof").style.opacity = page.asof ? "1" : "0.4";
  $("#asof").title = page.asof ? "" : "This screen covers a fixed window";
  $("#simTag").innerHTML = S.meta.live ? '<span class="live-pill">LIVE</span> NO MONEY MOVES BY ITSELF' : "DECISION SUPPORT · NO MONEY MOVES";
  syncAsof();
  const el = $("#page");
  if (!quiet) loading(el);
  // only a slow screen (over 0.7 s) brings the money rain back; quick ones never show it
  const my = ++Boot.token;
  const slow = setTimeout(() => { if (my === Boot.token && !quiet) Boot.show(`Crunching ${page.title}…`, true); }, 700);
  return page.render(el)
    .catch((e) => { el.innerHTML = `<div class="card bad">Error: ${esc(e.message)}</div>`; console.error(e); })
    .finally(() => { clearTimeout(slow); if (my === Boot.token) Boot.hide(); });
}
function goto(page) { location.hash = `#/${page}`; }
function setEngine(ok, title, sub) {
  $("#engine").classList.toggle("off", !ok);
  $("#engineTitle").textContent = title;
  $("#engineSub").textContent = sub;
}

/* ---------- 1. Command Center ---------- */
async function pageOverview(el) {
  const d = await api(`/api/overview?t=${S.t}`);
  const L = d.live, h = d.health;
  if (d.breaker.on) setEngine(false, "Circuit breaker on", d.breaker.reason || "Conservative mode");
  const q0 = Math.max(0, L.qs.findIndex((q) => Math.abs(q - L.q_default) < 1e-9));
  const V = { m: 1, d: 0, r: L.repo_rate, q: q0, tab: "dep", sel: L.week.length, step: 0, requested: null };
  const qLabel = (q) => `${+(q * 100).toFixed(1)}%`;
  el.innerHTML = `<div class="stack" style="gap:26px">
    <section class="hero" id="ccBanner"></section>
    <section class="card cream" style="padding:16px 20px">
      <div class="controls cc-controls">
        <label class="field"><span>Withdrawals <span class="val" id="vM"></span></span><input type="range" id="sM" min="0.5" max="3" step="0.1" value="1"></label>
        <label class="field"><span>Deposits <span class="val" id="vD"></span></span><input type="range" id="sD" min="-50" max="50" step="5" value="0"></label>
        <label class="field"><span>Repo rate <span class="val" id="vR"></span></span><input type="range" id="sR" min="2" max="6" step="0.05" value="${L.repo_rate.toFixed(2)}"></label>
        <div class="field"><span>Reserve confidence</span><div class="seg" id="sQ" style="margin-top:6px">${L.qs.map((q, i) => `<button type="button" data-q="${i}">${qLabel(q)}</button>`).join("")}</div></div>
        <div class="row" style="align-self:end"><button class="btn sm" id="ccLive" type="button">Go live</button><button class="btn sm" id="ccReset" type="button">Reset</button></div>
      </div>
      <p class="note" style="margin-top:10px">Starts from the ${S.meta.live ? "ledger at the newest hour," : "simulated ledger at"} ${esc(nice(d.ts))}. Flows use the dataset's average for each hour of the week ${badge("O")}; reserves come from the scenario engine at each confidence level ${badge("M")}. <span id="ccClock" class="num"></span></p>
    </section>
    <section class="grid g3" id="ccTiles"></section>
    <section class="grid cc-main">
      <div class="card"><div class="chart-head"><span>Cash flow · <span id="ccChartSub"></span></span><div class="tabs" id="ccTabs" style="box-shadow:none">
          <button type="button" data-tab="dep">Deposits</button><button type="button" data-tab="wd">Withdrawals</button><button type="button" data-tab="net">Net</button></div></div>
        <div class="bars" id="ccBars"></div><div class="bars-x" id="ccBarsX"></div>
        <p class="note">Last 7 days observed in the dataset ${badge("O")}. Striped bar: the next 24 hours under your settings.</p></div>
      <div class="stack" style="gap:24px"><div class="card mint" id="ccCash"></div><div class="card flush" id="ccMoves"></div></div>
    </section>
    <section><h2 style="margin-bottom:12px">Five-tier liquidity <span class="stamp b-o" style="margin-left:6px">observed rates · modeled balances</span></h2><div class="grid g5" id="ccTiers" style="gap:16px"></div></section>
    <section class="grid cc-bottom">
      <div class="card" id="ccDonut"></div>
      <div class="card flush" id="ccWave"></div>
      <div class="card sky" id="ccBreak"></div>
    </section>
  </div>`;

  const fmt = (v) => money(v * 1e6);
  const at = (k) => { const dt = new Date(d.ts.replace(" ", "T") + ":00"); dt.setHours(dt.getHours() + k); return dt; };
  const hh = (k) => `${String(at(k).getHours()).padStart(2, "0")}:00`;

  function model() {
    const stress = 0.5 + 0.5 * V.m;
    const reserve = L.fixed + (L.reserve_q[V.q] - L.fixed) * stress;
    const night = L.night_fixed + Math.max(0, L.night_q[V.q] - L.night_fixed) * stress;
    const dep = L.dep.map((x) => x * (1 + V.d / 100)), wd = L.wd.map((x) => x * V.m);
    let cash = L.cash, repo = L.repo, sweep = 0, k17 = -1, matured = 0;
    const path = [cash], repoAt = [repo];
    for (let k = 0; k < 24; k++) {
      const hour = L.hours[k];
      if (hour === 8 && repo > 0) { matured = repo * (1 + (V.r / 100) * 15 / 8760); cash += matured; repo = 0; }
      cash += dep[k] - wd[k];
      if (hour === 17) { sweep = Math.max(0, cash - night); cash -= sweep; repo = sweep; k17 = k; }
      path.push(cash); repoAt.push(repo);
    }
    const st = Math.min(24, V.step);
    const overnight = k17 >= 0 ? path.slice(k17 + 1, Math.min(25, k17 + 17)) : path;
    return { stress, reserve, night, dep, wd, path, repoAt, sweep, k17, st, cashNow: path[st], repoNow: repoAt[st],
      carry: sweep * 1e6 * (V.r / 100) * 15 / 8760, nightLow: Math.min(...overnight),
      depSoFar: d.flows.deposits / 1e6 + dep.slice(0, st).reduce((a, b) => a + b, 0),
      wdSoFar: d.flows.withdrawals / 1e6 + wd.slice(0, st).reduce((a, b) => a + b, 0) };
  }

  function draw() {
    const M = model(), st = M.st, cashNow = M.cashNow, od = Math.max(0, -cashNow) + L.od;
    const surplus = cashNow - M.reserve;
    const state = cashNow < -L.od_limit ? "gap" : cashNow < 0 ? "od" : surplus < 0 ? "low" : "safe";
    const stMap = { safe: ["p-safe", "Safe"], low: ["p-watch", "Below reserve"], od: ["p-watch", "On overdraft"], gap: ["p-breach", "Payout gap"] }[state];
    const swept = M.k17 >= 0 && st > M.k17;
    setEngine(state !== "gap" && !d.breaker.on, state === "gap" ? "Circuit breaker tripped" : d.breaker.on ? "Circuit breaker on" : "Circuit breaker armed",
      `${modeLabel()} · ${nice(`${at(st).toISOString().slice(0, 10)} ${hh(st)}`)}`);
    $("#vM").textContent = `${V.m.toFixed(1)}×`; $("#vD").textContent = `${V.d > 0 ? "+" : ""}${V.d}%`; $("#vR").textContent = `${V.r.toFixed(2)}%`;
    document.querySelectorAll("#sQ button").forEach((b) => b.classList.toggle("on", +b.dataset.q === V.q));
    $("#ccClock").textContent = st ? `Playing: +${st} h (${hh(st)})` : "";

    const win = M.k17 < 0 ? "No sweep window in the next 24 h" : st < M.k17 + 1 ? `Window opens ${hh(M.k17 + 1)} · in ${M.k17 + 1 - st} h` : "Locked until 08:00";
    $("#ccBanner").innerHTML = `<div class="copy"><div class="eyebrow"><span>Modeled · night-lock</span><span class="stamp" style="background:${st === M.k17 + 1 ? "var(--good-fill)" : "#fff"}">${win}</span><span class="pill ${stMap[0]}">${stMap[1]}</span></div>
        <h1>${swept ? "Swept" : "Sweep"} <span class="hl">${fmt(M.sweep)}</span> into 15-hour repo</h1>
        <p>Carry <strong>${money(M.carry)}</strong> · keeps <strong>${fmt(M.night)}</strong> overnight · lowest cash overnight <strong>${fmt(M.nightLow)}</strong></p>
        ${d.breaker.on ? `<p style="margin-top:6px"><span class="stamp" style="background:var(--bad);color:#fff">Circuit breaker on</span> ${esc(d.breaker.reason || "")}. The engine holds conservative mode; treat this panel as a what-if.</p>` : ""}</div>
      <div class="row"><button class="btn primary ${V.requested ? "on" : ""}" id="ccReq" type="button">${V.requested ? `Request #${V.requested.id} · ${V.requested.status.replace("_", " ")}` : "Request approval"}</button><a class="btn" href="#/nightlock">See the curve</a></div>`;
    $("#ccReq").onclick = async () => {
      if (!can("operator")) { toast("Requesting approval needs the operator role"); return; }
      try { V.requested = await api("/api/approvals", { method: "POST", body: JSON.stringify({ kind: "repo_sweep", amount: M.sweep * 1e6, detail: { at: d.ts, withdrawals: V.m, deposits: V.d, repo_rate: V.r, confidence: L.qs[V.q] } }) });
        toast(V.requested.status === "pending" ? `Request #${V.requested.id} needs a second approver` : `Request #${V.requested.id} approved automatically`); draw(); }
      catch (e) { toast(e.message); }
    };

    const odK = M.path.findIndex((v) => v < 0), gapK = M.path.findIndex((v) => v < -L.od_limit);
    const gauge = Math.max(0, Math.min(100, (cashNow / (M.reserve * 2)) * 100));
    $("#ccTiles").innerHTML = `
      <div class="card"><div class="head" style="margin-bottom:6px"><h2>Cash vs reserve</h2>${badge("M")}</div>
        <div class="num" style="font-size:30px">${fmt(cashNow)}</div><div class="dim" style="font-size:13px">Reserve floor ${fmt(M.reserve)} at ${qLabel(L.qs[V.q])}</div>
        <div class="gauge"><span style="width:${gauge.toFixed(1)}%;background:${state === "safe" ? "var(--good-fill)" : state === "gap" ? "var(--bad)" : "var(--warn)"}"></span><i></i></div>
        <div class="row between mono" style="font-size:11px;font-weight:700"><span>${surplus >= 0 ? `Cushion +${fmt(surplus)}` : `Short ${fmt(-surplus)}`}</span><span>marker = reserve</span></div></div>
      <div class="card cream"><div class="head" style="margin-bottom:10px"><h2>Runway, this scenario</h2>${badge("M")}</div>
        <div class="duo"><div><div class="k">Overdraft starts</div><div class="v">${odK < 0 ? "None" : hh(odK)}</div></div><div><div class="k">Payout gap</div><div class="v" style="color:${gapK < 0 ? "#000" : "var(--bad)"}">${gapK < 0 ? "None" : hh(gapK)}</div></div></div>
        <div style="margin-top:12px;border:2px solid #000;background:#fff;padding:8px 10px;font-size:13px">Lowest cash in the next 24 h: <strong class="num">${fmt(Math.min(...M.path))}</strong></div></div>
      <div class="card"><div class="head" style="margin-bottom:6px"><h2>Circuit breaker</h2>${badge("A")}</div><div class="list mono" style="font-size:12px">
        <div class="it"><span class="l">Band coverage</span><span class="v">${pct(h.band_coverage)} · min ${pct(h.min_coverage, 0)}</span></div>
        <div class="it"><span class="l">Drift, worst month (PSI)</span><span class="v">${h.psi_max.toFixed(3)} · max ${h.psi_limit}</span></div>
        <div class="it"><span class="l">Data freshness</span><span class="v">${!S.meta.live ? "Fixed file" : d.breaker.on && /stale/i.test(d.breaker.reason || "") ? "Stale" : "Live · OK"}</span></div>
        <div class="it"><span class="l">Mode</span><span class="v"><span class="stamp" style="background:${state === "gap" || h.breaker ? "var(--bad)" : "var(--sky)"};color:${state === "gap" || h.breaker ? "#fff" : "#000"}">${state === "gap" || h.breaker ? "Conservative" : "Standby"}</span></span></div></div></div>`;

    const rows = L.week.map((w) => [w.day, w.dep, w.wd]).concat([["Next 24 h", M.dep.reduce((a, b) => a + b, 0), M.wd.reduce((a, b) => a + b, 0)]]);
    const val = (r) => (V.tab === "dep" ? r[1] : V.tab === "wd" ? r[2] : r[1] - r[2]);
    const mx = Math.max(...rows.map((r) => Math.abs(val(r))), 1);
    $("#ccChartSub").textContent = V.tab === "net" ? "net by day" : V.tab === "wd" ? "withdrawals by day" : "deposits by day";
    document.querySelectorAll("#ccTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === V.tab));
    $("#ccBars").innerHTML = rows.map((r, i) => {
      const v = val(r), sel = i === V.sel, live = i === rows.length - 1;
      const fill = sel ? "var(--good-fill)" : V.tab === "net" ? (v < 0 ? "var(--bad)" : "var(--data1-fill)") : "var(--data2-fill)";
      const bg = live && !sel ? `repeating-linear-gradient(135deg, ${fill} 0 6px, #fff 6px 10px)` : fill;
      return `<button type="button" data-i="${i}" aria-label="${esc(r[0])} ${fmt(v)}">${sel ? `<span class="tip">${fmt(v)}</span>` : ""}<span class="col" style="height:${Math.max(3, (Math.abs(v) / mx) * 82).toFixed(1)}%;background:${bg};box-shadow:${sel ? "4px 0 0 #000" : "none"}"></span></button>`;
    }).join("");
    $("#ccBarsX").innerHTML = rows.map((r, i) => `<span>${i === rows.length - 1 ? "Live" : esc(r[0])}</span>`).join("");

    $("#ccCash").innerHTML = `<div class="row between mono" style="font-size:11px;font-weight:800"><span>TIER 0 · CASH BUFFER</span><span class="pill ${stMap[0]}" style="background:#fff">${stMap[1]}</span></div>
      <div class="num" style="font-size:36px;margin-top:12px;letter-spacing:-0.02em">${fmt(cashNow)}</div>
      <div style="font-weight:700;font-size:13px">${surplus >= 0 ? `${fmt(surplus)} above the reserve floor` : `${fmt(-surplus)} below the reserve floor`}</div>
      <div class="duo" style="margin-top:14px"><div><div class="k">${swept ? "Swept at 17:00" : "Sweep at 17:00"}</div><div class="v" style="font-size:16px">${fmt(M.sweep)}</div></div><div><div class="k">Carry tonight</div><div class="v" style="font-size:16px">${money(M.carry)}</div></div></div>`;

    const moves = [{ ic: "R", c: "var(--accent)", t1: swept ? "Swept into repo" : "Repo sweep · planned", t2: M.k17 >= 0 ? `Today ${hh(M.k17 + 1)}` : "—", amt: `−${fmt(M.sweep)}` }];
    d.events.filter((e) => e.kind === "maturity").slice(0, 1).forEach((e) => {
      const amt = (e.title.match(/\$[\d,.]+[MB]/) || [""])[0];
      moves.push({ ic: "M", c: "var(--good-fill)", t1: "Repo matured", t2: dayLabel(e.ts, d.ts), amt: amt ? `+${amt}` : "—" });
    });
    moves.push({ ic: "D", c: "var(--data1-fill)", t1: "Customer deposits", t2: "Since 00:00", amt: `+${fmt(M.depSoFar)}` });
    moves.push({ ic: "W", c: "var(--data2-fill)", t1: "Withdrawals", t2: "Since 00:00", amt: `−${fmt(M.wdSoFar)}` });
    $("#ccMoves").innerHTML = `<div style="padding:10px 16px;background:#000;color:#fff;font:700 13px var(--display);text-transform:uppercase">Movements today</div>` +
      moves.map((mv) => `<div class="movement" style="${mv === moves[0] ? "border-top:0" : ""}"><span class="ic" style="background:${mv.c}">${mv.ic}</span><div style="flex:1;min-width:0"><div class="t1">${esc(mv.t1)}</div><div class="t2">${esc(mv.t2)}</div></div><span class="amt">${esc(mv.amt)}</span></div>`).join("");

    const hc = L.haircuts, be = hc.map((x) => x / (L.apr / 8760));
    const tiers = [["Tier −1 · Overdraft", fmt(od), `Limit ${fmt(L.od_limit)}`, `${pct(L.apr, 0)} APR · immediate`, "#94A3B8"],
      ["Tier 0 · Cash", fmt(Math.max(0, cashNow)), `Floor ${fmt(M.reserve)}`, "0% · immediate", "var(--good-fill)"],
      ["Tier 1 · Repo", fmt(M.repoNow), "Back at 08:00", `${V.r.toFixed(2)}% · ${pct(hc[0], 1)} early cut`, "var(--accent)"],
      ["Tier 2 · Bonds", fmt(L.bonds), "Duration 1.92 y", `${L.bond_yield.toFixed(2)}% yield · ${pct(hc[1], 0)} cut`, "var(--data1-fill)"],
      ["Tier 3 · Equities", fmt(L.equities), "Volatility 18%", `Same day · ${pct(hc[2], 0)} cut`, "var(--data2-fill)"]];
    $("#ccTiers").innerHTML = tiers.map(([k, v, d1, d2, c]) => `<div class="tier" style="border-top:8px solid ${c}"><div class="k">${k}</div><div class="v">${v}</div><div class="d">${d1}</div><div class="d2">${d2}</div></div>`).join("");

    const parts = [[Math.max(0, cashNow), "var(--good-fill)", "Cash"], [M.repoNow, "var(--accent)", "Repo"], [L.bonds, "var(--data1-fill)", "Bonds"], [L.equities, "var(--data2-fill)", "Equities"]];
    const tot = parts.reduce((a, p) => a + p[0], 0) || 1, Cc = 2 * Math.PI * 50;
    let acc = 0;
    const arcs = parts.map(([v, c]) => { const len = (v / tot) * Cc; const s = `<circle cx="66" cy="66" r="50" fill="none" stroke="${c}" stroke-width="16" stroke-dasharray="${Math.max(0, len - 2).toFixed(1)} ${Cc.toFixed(1)}" stroke-dashoffset="${(-acc).toFixed(1)}" transform="rotate(-90 66 66)"></circle>`; acc += len; return s; }).join("");
    $("#ccDonut").innerHTML = `<h2>Available</h2><div class="row" style="gap:14px;margin-top:10px;flex-wrap:nowrap">
      <svg width="124" height="124" viewBox="0 0 132 132" role="img" aria-label="Liquid ${fmt(Math.max(0, cashNow) + M.repoNow)} of ${fmt(tot)}" style="flex:none"><circle cx="66" cy="66" r="50" fill="none" stroke="#000" stroke-width="22"></circle>${arcs}
        <text x="66" y="64" text-anchor="middle" fill="#000" font-family="JetBrains Mono, monospace" font-size="14" font-weight="800">${fmt(Math.max(0, cashNow) + M.repoNow)}</text><text x="66" y="82" text-anchor="middle" fill="#333" font-family="DM Sans, sans-serif" font-size="11">liquid now</text></svg>
      <div style="display:grid;gap:6px;font-size:12px;font-weight:700">${parts.map(([v, c, n]) => `<span class="row" style="gap:6px"><span class="sw" style="background:${c}"></span>${n} ${fmt(v)}</span>`).join("")}</div></div>`;

    const lo = Math.min(0, ...M.path), hi = Math.max(...M.path), span = hi - lo || 1;
    const X = (k) => (k / 24) * 310 + 5, Y = (v) => 112 - ((v - lo) / span) * 104;
    $("#ccWave").innerHTML = `<div class="chart-head"><span>Next 24 hours · projected cash</span><span class="mono">${hh(st)}</span></div>
      <div style="padding:12px 16px 14px"><svg viewBox="0 0 320 120" width="100%" height="140" role="img" aria-label="Projected cash over the next 24 hours, lowest ${fmt(Math.min(...M.path))}">
        <rect x="0" y="${Y(0).toFixed(1)}" width="320" height="${Math.max(0, 120 - Y(0)).toFixed(1)}" fill="rgba(255,59,48,0.14)"></rect>
        <line x1="0" y1="${Y(0).toFixed(1)}" x2="320" y2="${Y(0).toFixed(1)}" stroke="#000" stroke-width="1.5" stroke-dasharray="4 3"></line>
        <polyline points="${M.path.map((v, k) => `${X(k).toFixed(1)},${Y(v).toFixed(1)}`).join(" ")}" fill="none" stroke="#000" stroke-width="3" stroke-linejoin="miter"></polyline>
        <rect x="${(X(st) - 5).toFixed(1)}" y="${(Y(cashNow) - 5).toFixed(1)}" width="10" height="10" fill="var(--accent)" stroke="#000" stroke-width="2.5"></rect></svg>
        <div class="row between mono" style="font-size:11px;font-weight:800"><span>NOW</span><span>${M.k17 >= 0 ? `${hh(M.k17 + 1)} SWEEP` : ""}</span><span>+24 H</span></div></div>`;

    $("#ccBreak").innerHTML = `<h2>Borrow or sell?</h2><div class="formula">h* = haircut ÷ (APR ÷ 8,760)</div><div class="list mono" style="font-size:12px">
      <div class="it"><span class="l">Repo early · ${pct(hc[0], 1)}</span><span class="v">${Math.round(be[0]).toLocaleString()} h</span></div>
      <div class="it"><span class="l">Bonds · ${pct(hc[1], 0)}</span><span class="v">${Math.round(be[1]).toLocaleString()} h</span></div>
      <div class="it"><span class="l">Equities · ${pct(hc[2], 0)}</span><span class="v">${Math.round(be[2]).toLocaleString()} h</span></div></div>
      <p class="note" style="color:#000">Any gap shorter than these is cheaper on the ${pct(L.apr, 0)} overdraft than selling. <a href="#/ladder">Open the ladder →</a></p>`;
  }

  const bind = (id, key, parse) => { $(id).addEventListener("input", (e) => { V[key] = parse(e.target.value); V.requested = null; S.touched = true; draw(); }); };
  bind("#sM", "m", parseFloat); bind("#sD", "d", parseFloat); bind("#sR", "r", parseFloat);
  $("#sQ").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { V.q = +b.dataset.q; V.requested = null; S.touched = true; draw(); } });
  $("#ccTabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { V.tab = b.dataset.tab; draw(); } });
  $("#ccBars").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { V.sel = +b.dataset.i; draw(); } });
  $("#ccReset").onclick = () => { Object.assign(V, { m: 1, d: 0, r: L.repo_rate, q: q0, step: 0, requested: null }); $("#sM").value = 1; $("#sD").value = 0; $("#sR").value = L.repo_rate.toFixed(2); stopLive(); draw(); };
  const stopLive = () => { if (S.replayTimer) { clearInterval(S.replayTimer); S.replayTimer = null; } $("#ccLive").textContent = V.step > 0 && V.step < 24 ? "Resume live" : "Go live"; $("#ccLive").classList.remove("on"); };
  $("#ccLive").onclick = () => {
    if (S.replayTimer) { stopLive(); return; }
    if (V.step >= 24) V.step = 0;
    $("#ccLive").textContent = "Pause"; $("#ccLive").classList.add("on");
    S.replayTimer = setInterval(() => { if (S.page !== "overview" || V.step >= 24) { stopLive(); return; } V.step += 1; draw(); }, 900);
  };
  draw();
}
function dayLabel(eventTs, nowTs) {
  if (eventTs.slice(0, 10) === nowTs.slice(0, 10)) return eventTs.slice(11);
  const wd = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(`${eventTs.slice(0, 10)}T12:00:00`).getDay()];
  return `${wd} ${eventTs.slice(11)}`;
}

/* ---------- 2. Forecast ---------- */
async function pageForecast(el, h = 1, start = null) {
  const d = await api(`/api/forecast?h=${h}${start !== null ? `&start=${start}` : ""}`);
  const cov = d.coverage, s = d.series, cur = start ?? 0;
  const m = (t) => d.metrics.find((x) => x.target === t);
  el.innerHTML = `<div class="stack">
    ${hero(`Test window · from ${nice(S.meta.splits.test)} · never used for training`, `${pct(1 - m("net").model_wape / m("net").naive_wape, 0)} less error than the simple baseline`,
      `The 95% band covered <strong class="num" style="color:var(--ink)">${pct(cov.static_95)}</strong> of hours.`,
      `<div class="tabs" id="hz">${[1, 6, 24].map((x) => `<button class="${x === h ? "on" : ""}" data-h="${x}">${x} h ahead</button>`).join("")}</div>`)}
    <section class="card">
      <div class="head"><h2>Net flow per hour, $M</h2><div class="row">${legend([[css("--ink-2"), "Actual"], [css("--data1"), "Forecast"]])}<button class="btn sm" id="prev">←</button><button class="btn sm" id="next">→</button></div></div>
      <div class="chart tall"><canvas id="f1"></canvas></div>
    </section>
    <section class="grid g2">
      <div class="card"><div class="head"><h2>Error by target</h2><span class="meta">WAPE, lower is better</span></div>
        <table><tr><th>Target</th><th class="r">Model</th><th class="r">Same hour yesterday</th></tr>
        ${["net", "deposits", "withdrawals"].map((t) => { const x = m(t); return `<tr><td>${t[0].toUpperCase() + t.slice(1)}</td><td class="r num">${x.model_wape.toFixed(3)}</td><td class="r num dim">${x.naive_wape.toFixed(3)}</td></tr>`; }).join("")}</table></div>
      <div class="card"><div class="head"><h2>Band coverage, rolling 7 days</h2><button class="btn sm" id="drift">Run drift test</button></div>
        <div class="chart short"><canvas id="f2"></canvas></div><div id="driftOut" class="note">Alarm below 93% switches the circuit breaker on.</div></div>
    </section>
  </div>`;
  const c = C();
  chart("f1", { type: "line", data: { labels: s.ts.map((x) => nice(x)), datasets: [
    { data: s.lo95, borderColor: "transparent" },
    { data: s.hi95, borderColor: "transparent", backgroundColor: c.d1Soft, fill: "-1" },
    { label: "Forecast", data: s.p50, borderColor: c.d1 },
    { label: "Actual", data: s.actual, borderColor: c.ink2, borderWidth: 1.2 },
  ] }, options: { scales: { x: xAxis(8), y: axis() } } });
  chart("f2", { type: "line", data: { labels: cov.ts.map((x) => nice(x).split(" ·")[0]), datasets: [
    { label: "Adaptive", data: cov.rolling_aci, borderColor: c.d1 },
    { label: "Static", data: cov.rolling_static, borderColor: c.ink3, borderWidth: 1.4 },
    { label: "Alarm", data: cov.ts.map(() => 0.93), borderColor: c.bad, borderDash: [4, 3], borderWidth: 1 },
  ] }, options: { scales: { x: xAxis(6), y: axis({ min: 0.85, max: 1, ticks: { callback: (v) => pct(v, 0) } }) } } });
  el.querySelectorAll("#hz button").forEach((b) => (b.onclick = () => { killCharts(); loading(el); pageForecast(el, +b.dataset.h); }));
  $("#prev").onclick = () => { killCharts(); pageForecast(el, h, Math.max(0, cur - 336)); };
  const testHours = cov.ts.length * 6;
  $("#next").onclick = () => { killCharts(); pageForecast(el, h, Math.max(0, Math.min(testHours - 336, cur + 336))); };
  $("#drift").onclick = async () => {
    const r = await api("/api/forecast/drift-test", { method: "POST", body: JSON.stringify({ factor: 2 }) });
    $("#driftOut").innerHTML = r.hours_to_alarm === null ? `<span class="bad">No alarm fired.</span>` : `<span class="ok">Doubling the error variance tripped the alarm in ${r.hours_to_alarm} h</span> (target: within 48 h).`;
  };
}

/* ---------- 3. Funding Ladder ---------- */
async function pageLadder(el, gap = 10, dur = 10) {
  const d = await api(`/api/ladder?t=${S.t}&gap=${gap * 1e6}&duration=${dur}`);
  const best = d.recommended, next = d.paths.find((p, i) => i > 0 && p.feasible);
  const maxCost = Math.max(...d.paths.filter((p) => p.feasible).map((p) => p.cost), 1);
  const color = (p) => (p.path.startsWith("Borrow") || p.path.startsWith("Overdraft") ? "var(--accent)" : p.path.includes("equities") ? "var(--bad)" : p.path.includes("bonds") ? "var(--data1)" : "var(--data3)");
  const be = d.breakeven_hours;
  el.innerHTML = `<div class="stack">
    ${hero(`Cover ${money(gap * 1e6, 0)} for ${dur} hours`, esc(best.path), `Costs <strong class="num" style="color:var(--ink)">${money(best.cost)}</strong>${next ? `, against ${money(next.cost)} to ${esc(next.path.toLowerCase())}` : ""}.`,
      `<button class="btn primary" id="req">Request approval</button>`)}
    <section class="grid g2">
      <div class="field"><span>Gap <span class="val" id="gv">$${gap}M</span></span><input type="range" id="gap" min="1" max="400" value="${gap}" aria-label="Gap in millions"></div>
      <div class="field"><span>Expected duration <span class="val" id="dv">${dur} h</span></span><input type="range" id="dur" min="1" max="720" value="${dur}" aria-label="Duration in hours"></div>
    </section>
    <section class="card flush"><table>
      ${d.paths.map((p, i) => `<tr class="${i === 0 ? "best" : p.feasible ? "" : "muted"}"><td style="padding:16px 18px"><div style="font-weight:${i === 0 ? 600 : 500}">${esc(p.path)}</div><div class="dim" style="font-size:13px">${esc(p.note)} · ${esc(p.access.toLowerCase())}${p.feasible ? "" : " · not enough balance"}</div></td>
        <td style="width:180px">${p.feasible ? `<div class="bar"><span style="width:${Math.max(2, (p.cost / maxCost) * 100)}%;background:${color(p)}"></span></div>` : '<span class="dim" style="font-size:13px">Not available</span>'}</td>
        <td class="r num" style="width:130px;font-size:16px">${money(p.cost)}</td></tr>`).join("")}
    </table></section>
    <p class="lead">Borrowing stays cheaper than selling until <strong>${Math.round(be.repo).toLocaleString()} h</strong> for repo, <strong>${Math.round(be.bonds).toLocaleString()} h</strong> for bonds and <strong>${Math.round(be.equities).toLocaleString()} h</strong> for equities. Your gap is ${dur} h. <span class="dim">Overdraft headroom ${money(d.headroom, 0)} ${badge("A")} · repo matures in ${d.hours_to_maturity} h · decision #${d.decision.id}</span></p>
  </div>`;
  const upd = () => { $("#gv").textContent = `$${$("#gap").value}M`; $("#dv").textContent = `${$("#dur").value} h`; };
  $("#gap").oninput = upd; $("#dur").oninput = upd;
  const rerun = () => pageLadder(el, +$("#gap").value, +$("#dur").value);
  $("#gap").onchange = rerun; $("#dur").onchange = rerun;
  $("#req").onclick = () => requestApproval("funding", gap * 1e6, { path: best.path, cost: best.cost, at: ts(S.t), decision: d.decision.id });
}
async function requestApproval(kind, amount, detail) {
  if (!can("operator")) { toast("Requesting approval needs the operator role"); return; }
  try {
    const r = await api("/api/approvals", { method: "POST", body: JSON.stringify({ kind, amount, detail }) });
    toast(r.status === "pending" ? `Request #${r.id} needs a second approver` : `Request #${r.id} approved automatically (under threshold)`);
  } catch (e) { toast(e.message); }
}

/* ---------- 4. Night-Lock ---------- */
async function pageNightlock(el) {
  const d = await api(`/api/nightlock?t=${S.t}`);
  const h = d.history, cv = d.curve;
  el.innerHTML = `<div class="stack">
    ${hero(`${d.projected ? "Tonight, projected" : "Sweep window"} · ${nice(d.ts)} → 08:00`, `Sweep ${money(d.sweep)} into repo`,
      `Keeps ${money(d.night_reserve)} overnight and earns ${money(d.carry)} of carry.${d.projected ? " Cash at 17:00 is projected from expected flows; the plan updates as each hour arrives." : ""}`,
      `<button class="btn primary" id="req">Request approval</button>`)}
    <section class="figs g4">
      ${fig(`Cash before sweep ${badge("M")}`, money(d.cash))}
      ${fig(`Overdraft chance ${badge("M")}`, pct(d.od_prob, 2), "any use before 08:00")}
      ${fig(`Beyond-limit chance ${badge("M")}`, pct(d.breach_prob, 3), `policy cap ${pct(d.cap, 1)}`)}
      ${fig(`Repo rate ${badge("O")}`, `${(d.rate * 100).toFixed(2)}%`, "earned for 15 hours")}
    </section>
    <section class="card">
      <div class="head"><h2>Net carry by sweep size, one night</h2>${legend([[css("--accent"), "Carry minus expected overdraft cost"], [css("--bad"), "Overdraft chance", true]])}</div>
      <div class="chart"><canvas id="n1"></canvas></div>
      <p class="note">Shaded: sweeps that would dip into the overnight reserve.</p>
    </section>
    <details class="more"><summary>Calibration over the year</summary><div>
      <p class="lead">Beyond-limit nights: predicted <strong>${h.expected_breach_nights.toFixed(1)}</strong>, observed <strong>${h.observed_breach_nights}</strong>. Nights with any overdraft use: predicted <strong>${h.expected_od_nights.toFixed(1)}</strong> (95% interval ${h.interval[0].toFixed(1)}–${h.interval[1].toFixed(1)}), observed <strong>${h.observed_od_nights}</strong>.</p>
      <p class="note">${calNote(h)}</p></div></details>
  </div>`;
  const c = C(), xi = cv.sweep.findIndex((x) => x * 1e6 >= d.sweep - 1);
  chart("n1", { type: "line", data: { labels: cv.sweep.map((x) => `$${x.toFixed(0)}M`), datasets: [
    { label: "Net carry", data: cv.net, borderColor: c.accent },
    { label: "Recommended", data: cv.net.map((v, i) => (i === xi ? v : null)), backgroundColor: c.accent, borderColor: c.accent, pointRadius: 6, showLine: false },
    { label: "Overdraft chance", data: cv.od_prob, borderColor: c.bad, yAxisID: "y2", borderDash: [4, 3], borderWidth: 1.6 },
  ] }, options: { scales: { x: xAxis(8), y: axis({ ticks: { callback: (v) => `$${v}` } }), y2: { position: "right", min: 0, max: 1, grid: { display: false }, border: { display: false }, ticks: { callback: (v) => pct(v, 0) } } } },
    plugins: [{ id: "zone", beforeDraw(ch) { const x = ch.scales.x, a = ch.chartArea; const i0 = cv.sweep.findIndex((v) => v * 1e6 > d.s_max + 1); if (i0 < 0) return; const px = x.getPixelForValue(i0); ch.ctx.save(); ch.ctx.fillStyle = c.grey; ch.ctx.fillRect(px, a.top, a.right - px, a.bottom - a.top); ch.ctx.restore(); } }] });
  $("#req").onclick = () => requestApproval("repo_sweep", d.sweep, { at: d.ts, breach_prob: d.breach_prob, decision: d.decision.id });
}
function calNote(h) {
  const inB = h.observed_breach_nights <= Math.max(h.breach_interval[1], 0.5);
  const inO = h.observed_od_nights >= h.interval[0] && h.observed_od_nights <= h.interval[1];
  return (inB ? '<span class="ok">Beyond-limit nights are inside the prediction, which is the acceptance test.</span>' : '<span class="bad">Beyond-limit nights exceed the prediction.</span>') +
    (inO ? " Overdraft-use nights match too." : ` Overdraft-use nights run ${h.observed_od_nights > h.interval[1] ? "above" : "below"} the estimate, so the overnight tail is ${h.observed_od_nights > h.interval[1] ? "somewhat underestimated" : "overestimated"}.`);
}

/* ---------- 5. Allocation ---------- */
async function pageAllocation(el) {
  const d = await api(`/api/allocation?t=${S.t}`);
  if (!d.feasible) { el.innerHTML = hero("Optimizer", "No feasible allocation", `The circuit breaker falls back to the conservative policy. Reserve ${money(d.reserve)}.`); return; }
  const tiers = ["cash", "repo", "bonds", "equities"], names = ["Cash", "Repo", "Bonds", "Equities"];
  const att = d.attribution;
  const moves = [["Buy bonds", d.buy_bonds], ["Sell bonds", d.sell_bonds], ["Buy equities", d.buy_equities], ["Sell equities", d.sell_equities]].filter((x) => x[1] > 1e5);
  el.innerHTML = `<div class="stack">
    ${hero(`Optimizer · ${d.horizon_days}-day horizon`, moves.length ? moves.map((b) => `${b[0]} ${money(b[1])}`).join(" · ") : "Hold the current allocation",
      `Expected return ${money(d.expected_return_horizon)} over ${d.horizon_days} days. Decided by: ${d.binding.map((b) => esc(b)).join(", ") || "no binding limits"}.`,
      `<button class="btn" id="repro">Reproduce #${d.decision.id}</button>`)}
    <section class="grid g2">
      <div class="card"><div class="head"><h2>Current vs target</h2>${legend([[css("--ink-3"), "Current"], [css("--accent"), "Target"]])}</div><div class="chart"><canvas id="a1"></canvas></div></div>
      <div class="card"><div class="head"><h2>Why the reserve moved</h2><span class="meta num">${money(att.from)} → ${money(att.to)}</span></div><div class="chart"><canvas id="a2"></canvas></div><p class="note">Since ${nice(d.attribution_from)}. Each step is one component; they add up exactly.</p></div>
    </section>
    <details class="more"><summary>Access times, coverage and return assumptions</summary><div class="grid g2">
      <table><tr><th>Tier</th><th>Available</th><th class="r">Haircut</th></tr>${d.access.map((a) => `<tr><td>${a.tier}</td><td class="dim">${a.when}</td><td class="r num">${pct(a.haircut, 1)}</td></tr>`).join("")}</table>
      <div class="list">
        <div class="it"><span class="l">Reserve</span><span class="v">${money(d.reserve)}</span></div>
        <div class="it"><span class="l">24 h stressed outflow</span><span class="v">${money(d.coverage_need["24h"])}</span></div>
        <div class="it"><span class="l">72 h stressed outflow</span><span class="v">${money(d.coverage_need["72h"])}</span></div>
        <div class="it"><span class="l">Overdraft headroom counted</span><span class="v">${money(d.headroom)}</span></div>
        <div class="it"><span class="l">Returns over ${d.horizon_days} d (repo · bonds · equities)</span><span class="v">${pct(d.returns.repo, 2)} · ${pct(d.returns.bonds, 2)} · ${pct(d.returns.equities, 2)}</span></div>
      </div></div></details>
  </div>`;
  const c = C();
  chart("a1", { type: "bar", data: { labels: names, datasets: [
    { label: "Current", data: tiers.map((k) => d.current[k] / 1e6), backgroundColor: "#fff" },
    { label: "Target", data: tiers.map((k) => d.target[k] / 1e6), backgroundColor: c.accentFill },
  ] }, options: { scales: { x: xAxis(), y: axis({ ticks: { callback: (v) => `$${v}M` } }) } } });
  let run = att.from / 1e6;
  const steps = att.steps.filter((s) => Math.abs(s.delta) > 1);
  const bars = [[0, att.from / 1e6]], labels = ["Before"], colors = ["#fff"];
  const twoLines = (t) => { const w = t.split(" "); const h = Math.ceil(w.length / 2); return w.length > 1 ? [w.slice(0, h).join(" "), w.slice(h).join(" ")] : t; };
  steps.forEach((s) => { const a = run, b = run + s.delta / 1e6; bars.push([a, b]); labels.push(twoLines(label(s.component))); colors.push(s.delta >= 0 ? c.bad : c.goodFill); run = b; });
  bars.push([0, att.to / 1e6]); labels.push("After"); colors.push(c.accentFill);
  chart("a2", { type: "bar", data: { labels, datasets: [{ data: bars, backgroundColor: colors }] },
    options: { plugins: { tooltip: { callbacks: { label: (x) => `${(x.raw[1] - x.raw[0]).toFixed(1)} $M` } } }, scales: { y: axis({ ticks: { callback: (v) => `$${v}M` } }), x: { ...xAxis(), ticks: { autoSkip: false, maxRotation: 0, font: { size: 11 } } } } } });
  $("#repro").onclick = async () => {
    const r = await api(`/api/decisions/${d.decision.id}/reproduce`);
    toast(r.match === true ? `Decision #${r.id} reproduced exactly (${r.got})` : r.match === false ? `Mismatch: ${r.expected} vs ${r.got}` : r.note);
  };
}

/* ---------- 6. Stress Lab ---------- */
async function pageStress(el, tab = "twin") {
  const tabs = [["twin", "What-if shocks"], ["reverse", "Breaking point"], ["wrong", "Wrong-way risk"]];
  el.innerHTML = `<div class="stack">
    <div class="tabs" id="st">${tabs.map(([k, n]) => `<button class="${k === tab ? "on" : ""}" data-k="${k}">${n}</button>`).join("")}</div>
    <div id="sb"></div></div>`;
  el.querySelectorAll("#st button").forEach((b) => (b.onclick = () => { killCharts(); pageStress(el, b.dataset.k); }));
  const sb = $("#sb");
  if (tab === "twin") {
    sb.innerHTML = `<div class="stack">
      ${hero(`Digital Twin · from ${nice(ts(S.t))}`, "Inject a shock and watch the plan respond", "The baseline dataset is never changed.", `<button class="btn primary" id="runTwin">Run scenario</button>`)}
      <section class="controls">
        ${slider("withdrawal_mult", "Withdrawals", 1, 5, 0.1, 3, "×")}${slider("deposit_delay_h", "Deposit delay", 0, 48, 1, 0, " h")}
        ${slider("equity_drop", "Equity drop", 0, 50, 1, 0, "%")}${slider("hours", "Shock length", 6, 168, 6, 48, " h")}
      </section>
      <details class="more"><summary>More shocks</summary><div class="controls">
        ${slider("yield_shock_bp", "Bond yield shock", 0, 300, 10, 0, " bp")}${slider("haircut_add_pct", "Extra haircut", 0, 10, 0.5, 0, " pts")}
        <label class="check"><input type="checkbox" id="missed_maturity"> Missed repo maturity</label>
        <label class="check"><input type="checkbox" id="stale_feed"> Stale data feed</label></div></details>
      <div id="twinOut"></div></div>`;
    sb.querySelectorAll("input[type=range]").forEach((r) => (r.oninput = () => { $(`#v_${r.id}`).textContent = `${r.value}${r.dataset.u}`; }));
    $("#runTwin").onclick = runTwin;
    if (!can("analyst", "operator")) { $("#runTwin").disabled = true; $("#runTwin").title = "Needs the analyst or operator role"; }
  } else if (tab === "reverse") {
    sb.innerHTML = `<div class="stack">
      ${hero("Reverse stress test · 72 h window", "Find the smallest shock that breaks the plan", "Starts from the most vulnerable hour of the replay. Breaking means a fire sale or an unfunded payout.",
        `<select id="rx" class="btn">${axisOpts("withdrawal_mult")}</select><span class="dim">vs</span><select id="ry" class="btn">${axisOpts("deposit_delay_h")}</select><button class="btn primary" id="runRev">Find it</button>`)}
      <div id="revOut"></div></div>`;
    $("#runRev").onclick = runReverse;
    if (!can("analyst", "operator")) { $("#runRev").disabled = true; $("#runRev").title = "Needs the analyst or operator role"; }
  } else {
    const w = (await api("/api/data")).wrongway;
    sb.innerHTML = `<div class="stack">
      ${hero("Wrong-way risk", w.significant ? "Outflows move with markets" : "Outflows don't move with markets",
        `Equity return during the top 5% outflow hours differs by <strong class="num" style="color:var(--ink)">${w.top5_eq_ret_diff_bp.toFixed(2)} bp</strong> (95% interval ${w.ci_bp[0].toFixed(2)} to ${w.ci_bp[1].toFixed(2)}). ${w.significant ? "Scenarios sample flows and markets together." : "Scenarios can sample them independently."}`,
        `<span class="pill ${w.significant ? "p-watch" : "p-safe"}">${w.significant ? "Link found" : "No link"}</span>`)}
      <section class="card"><div class="head"><h2>Correlation of net flow with market moves, by lag</h2>${legend([[css("--data1"), "Equity return"], [css("--data2"), "Yield change"]])}</div><div class="chart short"><canvas id="w1"></canvas></div></section></div>`;
    const c = C();
    chart("w1", { type: "bar", data: { labels: w.lags.map((l) => `${l} h`), datasets: [
      { data: w.corr_equity, backgroundColor: c.d1Fill }, { data: w.corr_yield, backgroundColor: c.d2Fill },
    ] }, options: { scales: { x: xAxis(), y: axis({ suggestedMin: -0.1, suggestedMax: 0.1 }) } } });
  }
}
function slider(id, name, min, max, step, val, u) {
  return `<div class="field"><span>${name} <span class="val" id="v_${id}">${val}${u}</span></span><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${val}" data-u="${u}" aria-label="${name}"></div>`;
}
function axisOpts(sel) {
  return [["withdrawal_mult", "Withdrawals"], ["deposit_delay_h", "Deposit delay"], ["equity_drop", "Equity drop"], ["yield_shock_bp", "Yield shock"]]
    .map(([v, n]) => `<option value="${v}" ${v === sel ? "selected" : ""}>${n}</option>`).join("");
}
async function runTwin() {
  const out = $("#twinOut"); loading(out, "Running the Digital Twin…");
  const v = (id) => ($(`#${id}`) ? +$(`#${id}`).value : 0);
  const body = { start: S.t, hours: v("hours"), withdrawal_mult: v("withdrawal_mult"), deposit_delay_h: v("deposit_delay_h"), equity_drop: v("equity_drop") / 100,
    yield_shock_bp: v("yield_shock_bp"), haircut_add_pct: v("haircut_add_pct"), missed_maturity: $("#missed_maturity").checked, stale_feed: $("#stale_feed").checked };
  const d = await api("/api/scenario", { method: "POST", body: JSON.stringify(body) });
  const s = d.metrics.shocked, b = d.metrics.baseline;
  const verdict = s.shortfall_hours > 0 ? ["p-breach", `Payout gap for ${s.shortfall_hours} h`] : s.forced_sales > 1 ? ["p-watch", `Survives with ${money(s.forced_sales)} of fire sales`] : s.peak_od > 0 ? ["p-watch", `Survives on the overdraft, peak ${money(s.peak_od)}`] : ["p-safe", "Survives from cash and repo"];
  out.innerHTML = `<div class="stack">
    <div class="row"><span class="pill ${verdict[0]}" style="font-size:14px;padding:6px 14px">${verdict[1]}</span><span class="dim">Decision #${d.decision.id}${d.breaker_hours ? ` · circuit breaker on for ${d.breaker_hours} h` : ""}</span></div>
    ${d.projected_from !== null && d.projected_from !== undefined ? `<p class="note">Hours from ${esc(d.ts[d.projected_from] || "")} on are projected with each hour-of-week's average flows from the last eight weeks ${badge("M")}: the data doesn't reach that far yet.</p>` : ""}
    <section class="card"><div class="head"><h2>Cash, shocked vs baseline</h2>${legend([[css("--bad"), "Shocked"], [css("--ink-3"), "Baseline", true], [css("--warn"), "Overdraft drawn"]])}</div><div class="chart"><canvas id="t1"></canvas></div></section>
    <section class="figs g4">
      ${fig("Peak overdraft", money(s.peak_od), `baseline ${money(b.peak_od)}`)}
      ${fig("Fire sales", money(s.forced_sales), `baseline ${money(b.forced_sales)}`)}
      ${fig("Haircut cost", money(s.haircut_cost), `baseline ${money(b.haircut_cost)}`)}
      ${fig("Lowest cash + repo", money(s.min_liquid), `baseline ${money(b.min_liquid)}`)}
    </section></div>`;
  const c = C();
  chart("t1", { type: "line", data: { labels: d.ts, datasets: [
    { data: d.shocked.cash, borderColor: c.bad },
    { data: d.baseline.cash, borderColor: c.ink3, borderDash: [5, 4], borderWidth: 1.6 },
    { data: d.shocked.od.map((x) => -x), borderColor: c.warn, backgroundColor: c.warnSoft, fill: "origin", borderWidth: 1.6 },
  ] }, options: { scales: { x: xAxis(8), y: axis({ ticks: { callback: (v) => `$${v}M` } }) } } });
}
async function runReverse() {
  const out = $("#revOut"); loading(out, "Searching the shock grid (about 10 s)…");
  const d = await api(`/api/reverse?x=${$("#rx").value}&y=${$("#ry").value}`);
  const fmt = (v, u) => (u === "%" ? `${(v * 100).toFixed(0)}%` : `${+v.toFixed(2)}${u === "×" ? "×" : ` ${u}`}`);
  const n = d.xs.length, cells = [];
  for (let j = n - 1; j >= 0; j--) for (let i = 0; i < n; i++) cells.push(`<div title="${d.x_label} ${fmt(d.xs[i], d.x_unit)}, ${d.y_label} ${fmt(d.ys[j], d.y_unit)}" style="background:${d.grid[j][i] ? "var(--bad)" : "var(--good-soft)"};opacity:${d.grid[j][i] ? 0.75 : 1}"></div>`);
  out.innerHTML = `<section class="grid g2" style="align-items:start">
    <div><div class="rgrid" style="grid-template-columns:repeat(${n},1fr)">${cells.join("")}</div>
      <div class="row between dim" style="font-size:12px;margin-top:8px;max-width:560px"><span>${fmt(d.xs[0], d.x_unit)}</span><span>${esc(d.x_label)} →</span><span>${fmt(d.xs[n - 1], d.x_unit)}</span></div>
      <p class="note">Up: ${esc(d.y_label.toLowerCase())} from ${fmt(d.ys[0], d.y_unit)} to ${fmt(d.ys[n - 1], d.y_unit)}. Magenta breaks the plan.</p></div>
    <div class="stack" style="gap:20px">
      ${fig(`Breaks at · from ${nice(d.start_ts)}`, d.margin === null ? "Never in range" : `${fmt(d.margin, d.x_unit)} ${esc(d.x_label.toLowerCase())}`)}
      <div class="list">
        <div class="it"><span class="l">Starting cash + repo</span><span class="v">${money(d.start_state.cash + d.start_state.repo)}</span></div>
        <div class="it"><span class="l">At the largest ${esc(d.y_label.toLowerCase())}</span><span class="v">${d.boundary[n - 1] === null ? "none" : fmt(d.boundary[n - 1], d.x_unit)}</span></div>
        <div class="it"><span class="l">Boundary re-checked in the Digital Twin</span><span class="v">${d.confirmed.filter((c) => c.fails).length} of ${d.confirmed.length}</span></div>
      </div></div></section>`;
}

/* ---------- 7. Backtest ---------- */
async function pageBacktest(el) {
  const d = await api("/api/backtest");
  const ms = d.metrics, opt = ms.find((m) => m.policy === "optimized"), fix = ms.find((m) => m.policy === "fixed"), base = ms.find((m) => m.policy === "conservative").net_return;
  const signed = (x) => `${x >= 0 ? "+" : ""}${money(x, 2)}`;
  const gainFix = opt.net_return - fix.net_return, gaps = ms.filter((m) => m.policy !== "oracle").reduce((a, m) => a + m.shortfall_hours, 0);
  el.innerHTML = `<div class="stack">
    ${hero(`${nice(d.window[0])} → ${nice(d.window[1])} · same start for every policy`, `${signed(gainFix)} vs the fixed reserve · ${opt.shortfall_hours} gap hours`,
      `Optimized ${gainFix >= 0 ? "earned" : "lost"} ${money(Math.abs(gainFix), 2)} more than the fixed reserve and ${signed(opt.net_return - base)} vs all cash, with ${opt.shortfall_hours === 0 ? "no hour of unfunded payouts" : `${opt.shortfall_hours} hours of unfunded payouts`}. It captures ${opt.capture === null ? "—" : pct(opt.capture, 0)} of the gain perfect hindsight finds.`, `<a class="btn" href="/api/report">Download report</a>`)}
    <section class="figs g4">
      ${fig("Gain vs fixed reserve", signed(gainFix), "two months", gainFix >= 0 ? "ok" : "bad")}
      ${fig("Gain vs all cash", signed(opt.net_return - base), "two months", opt.net_return >= base ? "ok" : "bad")}
      ${fig("Gap hours", String(opt.shortfall_hours), gaps === 0 ? "every policy paid every payout" : "hours with unfunded payouts", opt.shortfall_hours === 0 ? "ok" : "bad")}
      ${fig("Idle cash", money(opt.idle_cash, 0), `vs ${money(fix.idle_cash, 0)} for fixed`)}
    </section>
    <section class="card flush"><table>
      <tr><th style="padding-left:18px">Policy</th><th class="r">Gain vs fixed</th><th class="r">Gain vs all cash</th><th class="r">Gap hours</th><th class="r">Overdraft cost</th><th class="r">Haircuts</th><th class="r">Capture</th><th class="r" style="padding-right:18px">Total incl. market ${badge("M")}</th></tr>
      ${ms.map((m) => `<tr class="${m.policy === "optimized" ? "best" : ""}"><td style="padding-left:18px">${esc(m.label)}</td><td class="r num">${m.policy === "fixed" ? "—" : signed(m.net_return - fix.net_return)}</td><td class="r num">${m.policy === "conservative" ? "—" : signed(m.net_return - base)}</td><td class="r num">${m.shortfall_hours}</td><td class="r num">${money(m.overdraft_cost, 2)}</td><td class="r num">${money(m.haircut_cost, 2)}</td><td class="r num">${m.capture === null ? "—" : pct(m.capture, 0)}</td><td class="r num dim" style="padding-right:18px">${money(m.net_return, 2)}</td></tr>`).join("")}
    </table></section>
    <p class="lead">The gains compare liquidity decisions on identical flows. Every policy holds the same bonds and equities, which fell with the market, so the last column (total including market moves) is negative for all of them. Perfect hindsight knows the future and is a ceiling, not a strategy.</p>
    <details class="more"><summary>Price of safety: the optimized policy at six reserve levels</summary><div>
      <table><tr><th>Reserve quantile</th><th class="r">Net return</th><th class="r">Gap hours</th><th class="r">Overdraft cost</th><th class="r">Cost of this step</th></tr>
      ${d.price_of_safety.map((x) => `<tr><td class="num">${pct(x.q, 1)}</td><td class="r num">${money(x.net_return, 3)}</td><td class="r num">${x.shortfall_hours}</td><td class="r num">${money(x.overdraft_cost)}</td><td class="r num dim">${x.cost_vs_prev === undefined ? "—" : money(x.cost_vs_prev)}</td></tr>`).join("")}</table>
      <p class="note">${posNote(d.price_of_safety)}</p></div></details>
  </div>`;
}
function posNote(p) {
  const a = p[0], b = p[p.length - 1];
  return `Going from ${pct(a.q, 0)} to ${pct(b.q, 1)} costs ${money(a.net_return - b.net_return)} over two months, with gap hours ${a.shortfall_hours} → ${b.shortfall_hours}. Extra safety is cheap here because overnight risk is small next to the overdraft line.`;
}

/* ---------- 8. Replay ---------- */
async function pageReplay(el) {
  const d = await api("/api/replay");
  const N = d.ts.length, W = 24 * 14, o = d.policies.optimized, f = d.policies.fixed;
  let cur = W, speed = 100, playing = false, last = 0;
  const om = d.metrics.optimized, fm = d.metrics.fixed;
  el.innerHTML = `<div class="stack">
    ${hero("Full year · Jan 1 → Dec 31", "Watch the year play out", `Optimized finishes ${money(Math.abs(om.net_return - fm.net_return), 1)} ${om.net_return >= fm.net_return ? "ahead of" : "behind"} the fixed reserve, with ${om.shortfall_hours} gap hours.`,
      `<button class="btn primary" id="play">Play</button><div class="tabs" id="spd">${[10, 100, 500].map((x) => `<button class="${x === speed ? "on" : ""}" data-s="${x}">${x}×</button>`).join("")}</div>`)}
    <div><div class="row between"><span class="num" id="clock" style="font-size:18px"></span><span class="dim" style="font-size:13px">hours per second</span></div>
      <input type="range" id="scrub" min="${W}" max="${N}" value="${cur}" style="width:100%;margin-top:10px;accent-color:var(--accent)" aria-label="Replay position"></div>
    <section class="figs g4" id="tiles"></section>
    <section class="card"><div class="head"><h2>Cash + repo, trailing 14 days</h2>${legend([[css("--accent"), "Optimized"], [css("--ink-3"), "Fixed reserve"], [css("--bad"), "Reserve", true]])}</div><div class="chart tall"><canvas id="r1"></canvas></div><p class="note">Shaded hours: circuit breaker on.</p></section>
    <details class="more"><summary>Alerts (${d.alerts.length}) · click one to jump</summary><div class="alerts">${d.alerts.map((a) => `<div class="alert ${a.level}" data-t="${a.t}"><span class="t">${nice(d.ts[a.t])}</span><span><strong>${esc(a.title)}</strong> · ${esc(a.detail)}</span></div>`).join("")}</div></details>
  </div>`;
  const c = C();
  const ch = chart("r1", { type: "line", data: { labels: [], datasets: [
    { data: [], borderColor: c.accent }, { data: [], borderColor: c.ink3, borderWidth: 1.6 }, { data: [], borderColor: c.bad, borderDash: [5, 4], borderWidth: 1.4 },
  ] }, options: { scales: { x: xAxis(7), y: axis({ ticks: { callback: (v) => `$${v}M` } }) } },
    plugins: [{ id: "brk", beforeDraw(chart) { const a = chart.chartArea, x = chart.scales.x, lo = cur - W; chart.ctx.save(); chart.ctx.fillStyle = c.warnSoft;
      for (let i = 0; i < W; i++) if (d.breaker[lo + i]) chart.ctx.fillRect(x.getPixelForValue(i), a.top, Math.max(1, (a.right - a.left) / W), a.bottom - a.top); chart.ctx.restore(); } }] });
  const liquid = (p, i) => +(p.cash[i] + p.repo[i]).toFixed(1);
  function draw() {
    const lo = cur - W, i = cur - 1;
    ch.data.labels = d.ts.slice(lo, cur).map((x) => nice(x));
    ch.data.datasets[0].data = Array.from({ length: W }, (_, k) => liquid(o, lo + k));
    ch.data.datasets[1].data = Array.from({ length: W }, (_, k) => liquid(f, lo + k));
    ch.data.datasets[2].data = d.reserve.slice(lo, cur);
    ch.update("none");
    $("#clock").textContent = nice(d.ts[i]);
    $("#scrub").value = cur;
    $("#tiles").innerHTML = fig("Cash", money(o.cash[i] * 1e6)) + fig("Repo tonight", money(o.repo[i] * 1e6)) + fig("Overdraft", money(o.od[i] * 1e6), "", o.od[i] > 0 ? "warn" : "") +
      fig("Circuit breaker", d.breaker[i] ? "On" : "Off", "", d.breaker[i] ? "warn" : "ok");
  }
  function loop(t) {
    if (!playing) return;
    if (last) cur = Math.min(N, cur + Math.max(1, Math.round((speed * (t - last)) / 1000)));
    last = t; draw();
    if (cur >= N) { playing = false; $("#play").textContent = "Play"; return; }
    S.replayTimer = requestAnimationFrame(loop);
  }
  $("#play").onclick = () => { playing = !playing; $("#play").textContent = playing ? "Pause" : "Play"; last = 0; if (playing) { if (cur >= N) cur = W; S.replayTimer = requestAnimationFrame(loop); } };
  el.querySelectorAll("#spd button").forEach((b) => (b.onclick = () => { speed = +b.dataset.s; el.querySelectorAll("#spd button").forEach((x) => x.classList.toggle("on", x === b)); }));
  $("#scrub").oninput = (e) => { cur = +e.target.value; draw(); };
  el.querySelectorAll(".alert").forEach((a) => (a.onclick = () => { cur = Math.max(W, Math.min(N, +a.dataset.t + 12)); draw(); window.scrollTo({ top: 0, behavior: "smooth" }); }));
  draw();
}

/* ---------- 9. Trust ---------- */
async function pageTrust(el, tab = "data") {
  const [d, dv] = await Promise.all([api("/api/trust"), api("/api/data")]);
  const tabs = [["data", "Data"], ["honesty", "Honesty"], ["policy", "Policy"], ["approvals", "Approvals"], ["audit", "Audit"], ...(can("admin") ? [["users", "Users"]] : [])];
  el.innerHTML = `<div class="stack"><div class="tabs" id="tt">${tabs.map(([k, n]) => `<button class="${k === tab ? "on" : ""}" data-k="${k}">${n}</button>`).join("")}</div><div id="tb"></div></div>`;
  el.querySelectorAll("#tt button").forEach((b) => (b.onclick = () => { killCharts(); pageTrust(el, b.dataset.k); }));
  const tb = $("#tb");
  if (tab === "data") return trustData(tb, d, dv);
  if (tab === "honesty") return trustHonesty(tb, d);
  if (tab === "policy") return trustPolicy(tb, d, el);
  if (tab === "approvals") return trustApprovals(tb, d, el);
  if (tab === "users") return trustUsers(tb, el);
  return trustAudit(tb, d, el);
}
function trustData(tb, d, dv) {
  const s = dv.summary, m = d.meta, allOk = m.checks.every((c) => c.ok);
  const mx = Math.max(...s.heatmap.flat().map(Math.abs));
  const cell = (v) => { const c = v < 0 ? `rgba(255,59,48,${((Math.abs(v) / mx) * 0.9).toFixed(3)})` : `rgba(56,219,255,${((v / mx) * 0.95).toFixed(3)})`;
    return `<div title="${v.toFixed(2)} $M" style="background:linear-gradient(${c}, ${c}), #fff"></div>`; };
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  tb.innerHTML = `<div class="stack">
    ${hero(`Dataset · sha256 ${m.sha256.slice(0, 12)}`, allOk ? `All ${m.checks.length} data checks pass` : "Some data checks failed", `${m.rows.toLocaleString()} hourly rows from ${m.start.slice(0, 10)} to ${m.end.slice(0, 10)}.`)}
    <section class="figs g4">
      ${fig(`Avg hourly deposits ${badge("O")}`, money(s.avg_deposits, 2))}
      ${fig(`Avg hourly withdrawals ${badge("O")}`, money(s.avg_withdrawals, 2))}
      ${fig(`Net-outflow hours ${badge("O")}`, pct(s.neg_share))}
      ${fig(`Annual net flow ${badge("O")}`, money(s.cum_net))}
    </section>
    <section class="card"><div class="head"><h2>Mean net flow by hour and weekday</h2>${legend([[css("--data1"), "Inflow"], [css("--bad"), "Outflow"]])}</div>
      <div class="heat"><div></div>${Array.from({ length: 24 }, (_, h) => `<div>${h % 3 === 0 ? h : ""}</div>`).join("")}${s.heatmap.map((row, i) => `<div>${days[i]}</div>${row.map(cell).join("")}`).join("")}</div></section>
    <details class="more"><summary>Checks, distribution, autocorrelation and drift</summary><div class="stack" style="gap:24px">
      <div class="list">${m.checks.map((c) => `<div class="it"><span class="l">${c.ok ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>'} ${esc(c.name)}</span><span class="v dim" style="font-family:inherit">${esc(c.detail)}</span></div>`).join("")}</div>
      <div class="grid g3">
        <div class="card"><h2 style="font-size:15px;margin-bottom:12px">Net flow per hour, $M</h2><div class="chart short"><canvas id="d1"></canvas></div></div>
        <div class="card"><h2 style="font-size:15px;margin-bottom:12px">Autocorrelation · lag 1 = ${s.acf["1"].toFixed(2)}</h2><div class="chart short"><canvas id="d2"></canvas></div></div>
        <div class="card"><h2 style="font-size:15px;margin-bottom:12px">Monthly drift (PSI, alarm 0.25)</h2><div class="chart short"><canvas id="d3"></canvas></div></div>
      </div></div></details>
  </div>`;
  const c = C(), h = s.hist, lags = Object.keys(s.acf), months = Object.keys(dv.psi);
  chart("d1", { type: "bar", data: { labels: h.counts.map((_, i) => h.edges[i].toFixed(0)), datasets: [{ data: h.counts, backgroundColor: h.edges.slice(0, -1).map((e) => (e < 0 ? c.bad : c.d1Fill)) }] }, options: { scales: { x: xAxis(6), y: axis() } } });
  chart("d2", { type: "bar", data: { labels: lags.map((l) => `${l}h`), datasets: [{ data: lags.map((l) => s.acf[l]), backgroundColor: c.d1Fill }] }, options: { scales: { x: xAxis(), y: axis() } } });
  chart("d3", { type: "bar", data: { labels: months.map((x) => x.slice(5)), datasets: [{ data: months.map((k) => dv.psi[k]), backgroundColor: months.map((k) => (dv.psi[k] > 0.25 ? c.bad : c.d2Fill)) }] }, options: { scales: { x: xAxis(), y: axis({ suggestedMax: 0.3 }) } } });
}
function trustHonesty(tb, d) {
  const h = d.honesty, flagged = h.findings.filter((f) => f.flag).length;
  tb.innerHTML = `<div class="stack">
    ${hero("Dataset honesty report", `${flagged} of ${h.findings.length} checks suggest generated data`, "So results hold for this file, not for real treasuries.")}
    <section class="card flush"><table>${h.findings.map((f) => `<tr><td style="padding-left:18px">${f.flag ? '<span class="warn">●</span>' : '<span class="ok">●</span>'} ${esc(f.check)}</td><td>${esc(f.result)}</td><td class="dim">${esc(f.detail)}</td></tr>`).join("")}</table></section>
    <section class="grid g2">
      <div><h2 class="ok" style="margin-bottom:8px">Safe to claim</h2><div class="list">${h.supports.map((x) => `<div class="it"><span class="l">${esc(x)}</span></div>`).join("")}</div></div>
      <div><h2 class="bad" style="margin-bottom:8px">Not safe to claim</h2><div class="list">${h.does_not_support.map((x) => `<div class="it"><span class="l">${esc(x)}</span></div>`).join("")}</div></div>
    </section></div>`;
}
function trustPolicy(tb, d, el) {
  const p = d.policy, b = d.balances;
  tb.innerHTML = `<div class="stack">
    ${hero(`Policy v${p.version} · sha256 ${p.sha256.slice(0, 12)}`, "Rules every recommendation is checked against", `Saved by ${esc(p.created_by)} at ${esc(p.created_at)}. Saving re-runs every replay (a few seconds).${can("admin") ? "" : " Only admins can change the policy and balances."}`, can("admin") ? `<button class="btn primary" id="savePol">Save new version</button>` : "")}
    <section class="grid g2" style="align-items:start">
      <textarea class="yaml" id="yaml" spellcheck="false" aria-label="Policy YAML">${esc(p.yaml)}</textarea>
      <div class="stack" style="gap:24px">
        <div><h2 style="margin-bottom:4px">Starting balances ${badge("A")}</h2><p class="note" style="margin:0 0 14px">Not in the dataset: simulation inputs, $M on Jan 1.</p>
          <div class="grid g3">${["cash", "bonds", "equities"].map((k) => `<div class="field"><span>${k[0].toUpperCase() + k.slice(1)}</span><input type="number" id="bal_${k}" value="${(b[k] / 1e6).toFixed(0)}" min="0" step="50"></div>`).join("")}</div>
          <button class="btn" id="saveBal" style="margin-top:14px">Apply and re-run</button></div>
        <div><h2 style="margin-bottom:8px">Versions</h2><div class="list">${d.versions.map((v) => `<div class="it"><span class="l">v${v.version} · ${esc(v.created_by)}</span><span class="v dim">${v.sha256}</span></div>`).join("")}</div></div>
      </div></section></div>`;
  if (!can("admin")) { $("#yaml").readOnly = true; $("#saveBal").disabled = true; tb.querySelectorAll("input[id^=bal_]").forEach((i) => (i.disabled = true)); return; }
  $("#savePol").onclick = async () => {
    $("#savePol").disabled = true; toast("Saving and re-running replays…");
    try { const r = await api("/api/policy", { method: "PUT", body: JSON.stringify({ yaml: $("#yaml").value }) }); toast(`Saved policy v${r.version}`); pageTrust(el, "policy"); }
    catch (e) { toast(e.message); $("#savePol").disabled = false; }
  };
  $("#saveBal").onclick = async () => {
    $("#saveBal").disabled = true; toast("Re-running replays…");
    const body = Object.fromEntries(["cash", "bonds", "equities"].map((k) => [k, +$(`#bal_${k}`).value * 1e6]));
    try { await api("/api/balances", { method: "PUT", body: JSON.stringify(body) }); toast("Balances applied"); pageTrust(el, "policy"); }
    catch (e) { toast(e.message); $("#saveBal").disabled = false; }
  };
}
function trustApprovals(tb, d, el) {
  const thr = d.policy.policy.approvals.maker_checker_above_usd, pending = d.approvals.filter((a) => a.status === "pending").length;
  tb.innerHTML = `<div class="stack">
    ${hero(`Maker-checker · above ${money(thr, 0)}`, pending ? `${pending} request${pending > 1 ? "s" : ""} waiting for a second approver` : "Nothing waiting for approval",
      `Signed in as <strong style="color:var(--ink)">${esc(S.user.username)}</strong> (${esc(S.user.roles.join(", "))}). Nobody can approve their own request; a second person with the approver role must decide.`,
      can("operator") ? `<button class="btn" id="demoReq">Create a $40M test request</button>` : "")}
    <section class="card flush"><table>
      <tr><th style="padding-left:18px">#</th><th>Kind</th><th class="r">Amount</th><th>Requested by</th><th>Status</th><th></th></tr>
      ${d.approvals.map((a) => `<tr><td style="padding-left:18px" class="num">${a.id}</td><td>${esc(a.kind.replace("_", " "))}</td><td class="r num">${money(a.amount)}</td><td>${esc(a.requested_by)}</td>
        <td><span class="pill ${a.status === "approved" || a.status === "auto_approved" ? "p-safe" : a.status === "rejected" ? "p-breach" : "p-watch"}">${a.status.replace("_", " ")}</span>${a.decided_by ? ` <span class="dim" style="font-size:13px">by ${esc(a.decided_by)}</span>` : ""}</td>
        <td class="r" style="padding-right:18px">${a.status !== "pending" ? "" : a.requested_by === S.user.username ? '<span class="dim" style="font-size:13px">Waiting for someone else</span>' : can("approver") ? `<button class="btn sm" data-a="${a.id}" data-v="1">Approve</button> <button class="btn sm" data-a="${a.id}" data-v="0">Reject</button>` : '<span class="dim" style="font-size:13px">Needs an approver</span>'}</td></tr>`).join("") || '<tr><td colspan="6" class="dim" style="padding:18px">No requests yet. Request approval from Funding Ladder or Night-Lock.</td></tr>'}
    </table></section></div>`;
  tb.querySelectorAll("button[data-a]").forEach((b) => (b.onclick = async () => {
    try { await api(`/api/approvals/${b.dataset.a}/decide`, { method: "POST", body: JSON.stringify({ approve: b.dataset.v === "1" }) }); toast("Decision recorded"); pageTrust(el, "approvals"); }
    catch (e) { toast(e.message); }
  }));
  if ($("#demoReq")) $("#demoReq").onclick = async () => { await requestApproval("funding", 40e6, { note: "test request" }); pageTrust(el, "approvals"); };
}
function trustAudit(tb, d, el) {
  const v = d.verify;
  tb.innerHTML = `<div class="stack">
    ${hero(`Hash-chained audit log · head ${v.head || ""}`, v.ok ? `Chain verified · ${v.records} records` : `<span class="bad">Chain broken at record #${v.broken_at}</span>`,
      "Each record stores SHA-256 of the previous hash plus itself, so any edit shows.", `<button class="btn" id="verify">Verify again</button>${d.demo_tools && can("admin") ? '<button class="btn primary" id="tamper">Run tamper test</button>' : ""}`)}
    <div id="tamperOut"></div>
    <section class="card flush"><table>
      <tr><th style="padding-left:18px">#</th><th>Event</th><th>Details</th><th>Hash</th><th></th></tr>
      ${d.audit.map((e) => `<tr><td style="padding-left:18px" class="num">${e.seq}</td><td>${esc((e.payload.kind || e.kind).replace("_", " "))}<div class="dim" style="font-size:12px">${esc(e.at)}</div></td><td class="dim" style="font-size:13px;max-width:420px;overflow-wrap:anywhere">${esc(summ(e))}</td><td class="num dim">${e.hash}</td>
        <td class="r" style="padding-right:18px">${e.kind === "decision" && ["allocation", "ladder"].includes(e.payload.kind) ? `<button class="btn sm" data-r="${e.seq}">Reproduce</button>` : ""}</td></tr>`).join("")}
    </table></section></div>`;
  $("#verify").onclick = () => pageTrust(el, "audit");
  if ($("#tamper")) $("#tamper").onclick = async () => {
    const r = await api("/api/audit/tamper-test", { method: "POST" });
    $("#tamperOut").innerHTML = `<p class="lead">Edited record #${r.tampered_seq}: verification <span class="bad">failed at #${r.after_tamper.broken_at}</span>. Restored it: <span class="ok">${r.after_restore.ok ? "verified again" : "still broken"}</span>.</p>`;
  };
  tb.querySelectorAll("button[data-r]").forEach((b) => (b.onclick = async () => {
    const r = await api(`/api/decisions/${b.dataset.r}/reproduce`);
    toast(r.match === true ? `#${r.id} reproduced exactly` : r.match === false ? `#${r.id} mismatch` : r.note);
  }));
}
function summ(e) {
  const p = e.payload;
  if (e.kind === "decision") return `policy v${p.policy_version} · ${p.model_version} · seed ${p.seed} · by ${p.by}`;
  return Object.entries(p).map(([k, v]) => `${k}: ${k === "sha256" ? String(v).slice(0, 12) : typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ").slice(0, 140);
}

/* ---------- Trust · Users (admin) ---------- */
async function trustUsers(tb, el) {
  const d = await api("/api/users");
  const roles = Object.keys(d.roles);
  const roleBoxes = (name, have = []) => roles.map((r) => `<label class="check" style="font-size:13px" title="${esc(d.roles[r])}"><input type="checkbox" name="${name}" value="${r}" ${have.includes(r) ? "checked" : ""}> ${r}</label>`).join("");
  tb.innerHTML = `<div class="stack">
    ${hero(`${d.users.length} account${d.users.length === 1 ? "" : "s"} · ${d.tokens.length} API token${d.tokens.length === 1 ? "" : "s"}`, "Who can do what",
      "Passwords are stored as salted PBKDF2 hashes. Five wrong passwords lock an account for five minutes. Every change lands in the audit log.")}
    <section class="card flush"><table>
      <tr><th style="padding-left:18px">User</th><th>Roles</th><th>Last sign-in</th><th>Status</th><th></th></tr>
      ${d.users.map((u) => `<tr data-u="${esc(u.username)}"><td style="padding-left:18px"><strong>${esc(u.username)}</strong><div class="dim" style="font-size:12px">added by ${esc(u.created_by)}</div></td>
        <td><div class="row" style="gap:10px;flex-wrap:wrap">${roleBoxes(`r_${u.username}`, u.roles)}</div></td>
        <td class="dim num" style="font-size:13px">${esc(u.last_login || "never")}</td>
        <td><span class="pill ${u.active ? "p-safe" : "p-breach"}">${u.active ? "active" : "disabled"}</span></td>
        <td class="r" style="padding-right:18px;white-space:nowrap"><button class="btn sm" data-act="roles">Save roles</button> <button class="btn sm" data-act="pw">Reset password</button> <button class="btn sm" data-act="toggle">${u.active ? "Disable" : "Enable"}</button></td></tr>`).join("")}
    </table></section>
    <section class="grid g2" style="align-items:start">
      <form class="card" id="newUser"><h2 style="margin-bottom:12px">Add a person</h2>
        <div class="grid g2"><label class="field"><span>Username</span><input class="inp" name="username" required minlength="2" maxlength="40" autocomplete="off"></label>
          <label class="field"><span>Temporary password</span><input class="inp" name="password" type="password" required minlength="10" autocomplete="new-password"></label></div>
        <div class="row" style="gap:12px;flex-wrap:wrap;margin:14px 0">${roleBoxes("nr", ["viewer"])}</div>
        <button class="btn primary" type="submit">Create account</button><p class="note">Share the password with them privately; they can change it after signing in.</p></form>
      <div class="card"><h2 style="margin-bottom:6px">API tokens</h2><p class="note" style="margin-top:0">For scripts and live feeds. A token acts as its user, with that user's roles; give feeds a user with only the <b>ingest</b> role.</p>
        <div class="list">${d.tokens.map((t) => `<div class="it"><span class="l">${esc(t.label)} · ${esc(t.username)}<div class="dim" style="font-size:12px">created ${esc(t.created_at)} · id ${t.id}</div></span><span class="v"><button class="btn sm" data-revoke="${t.id}">Revoke</button></span></div>`).join("") || '<div class="it"><span class="l dim">No tokens yet</span></div>'}</div>
        <form id="newTok" class="grid g2" style="margin-top:14px;align-items:end">
          <label class="field"><span>For user</span><select name="username">${d.users.filter((u) => u.active).map((u) => `<option>${esc(u.username)}</option>`).join("")}</select></label>
          <label class="field"><span>Label</span><input class="inp" name="label" value="live feed" maxlength="60"></label>
          <button class="btn" type="submit">Create token</button></form>
        <div id="tokOut"></div></div>
    </section></div>`;
  const reload = () => pageTrust(el, "users");
  tb.querySelectorAll("tr[data-u] button").forEach((b) => (b.onclick = async () => {
    const row = b.closest("tr"), u = row.dataset.u;
    try {
      if (b.dataset.act === "roles") {
        const rs = [...row.querySelectorAll("input[type=checkbox]:checked")].map((x) => x.value);
        await api(`/api/users/${encodeURIComponent(u)}`, { method: "PATCH", body: JSON.stringify({ roles: rs }) }); toast(`Roles saved for ${u}`);
      } else if (b.dataset.act === "pw") {
        const pw = prompt(`New temporary password for ${u} (at least 10 characters)`); if (!pw) return;
        await api(`/api/users/${encodeURIComponent(u)}`, { method: "PATCH", body: JSON.stringify({ password: pw }) }); toast(`Password reset for ${u}; their sessions were signed out`);
      } else {
        const active = b.textContent === "Enable";
        await api(`/api/users/${encodeURIComponent(u)}`, { method: "PATCH", body: JSON.stringify({ active }) }); toast(`${u} ${active ? "enabled" : "disabled"}`);
      }
      reload();
    } catch (e) { toast(e.message); }
  }));
  $("#newUser").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { await api("/api/users", { method: "POST", body: JSON.stringify({ username: f.get("username"), password: f.get("password"), roles: f.getAll("nr") }) }); toast("Account created"); reload(); }
    catch (err) { toast(err.message); }
  };
  $("#newTok").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api("/api/tokens", { method: "POST", body: JSON.stringify({ username: f.get("username"), label: f.get("label") }) });
      $("#tokOut").innerHTML = `<p class="note"><b>Copy this token now.</b> It is shown once and stored only as a hash.</p><div class="token-once">${esc(r.token)}</div>`;
    } catch (err) { toast(err.message); }
  };
  tb.querySelectorAll("[data-revoke]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Revoke this token? Anything using it stops working.")) return;
    try { await api(`/api/tokens/${b.dataset.revoke}`, { method: "DELETE" }); toast("Token revoked"); reload(); } catch (e) { toast(e.message); }
  }));
}

/* ---------- 10. Data sources: bring your own data, live feed ---------- */
async function pageData(el) {
  const d = await api("/api/datasets"), eng = d.engine, admin = can("admin");
  const act = d.datasets.find((x) => x.id === d.active) || {};
  const host = location.origin;
  el.innerHTML = `<div class="stack">
    ${hero(`Active dataset${act.live ? ' · <span class="live-pill">LIVE</span>' : ""}`, esc(act.name || "—"),
      `${(eng.rows || 0).toLocaleString()} hourly rows · ${esc(S.meta.ts[0])} → ${esc(eng.last_ts || "")}. ${esc(eng.building ? eng.message : eng.error ? eng.message : "Engine ready.")}${act.assumed && act.assumed.length ? ` Assumed columns: ${esc(act.assumed.join(", "))}.` : ""}`,
      admin ? '<a class="btn primary" href="#upload" id="goUp">Upload your data</a>' : "")}
    <section class="card flush"><table>
      <tr><th style="padding-left:18px">Dataset</th><th class="r">Rows</th><th>Range</th><th>Added</th><th></th></tr>
      ${d.datasets.map((x) => `<tr class="${x.id === d.active ? "best" : ""}"><td style="padding-left:18px"><strong>${esc(x.name)}</strong>${x.live ? ' <span class="live-pill">LIVE</span>' : ""}<div class="dim" style="font-size:12px">${esc(x.id)}${x.readonly ? " · read-only demo" : ""}${x.notes && x.notes.length ? ` · ${esc(x.notes.join("; "))}` : ""}</div></td>
        <td class="r num">${x.rows ? x.rows.toLocaleString() : "—"}</td><td class="dim num" style="font-size:13px">${esc((x.start || "").slice(0, 16))}${x.end ? ` → ${esc(x.end.slice(0, 16))}` : ""}</td>
        <td class="dim" style="font-size:13px">${esc(x.created_by)} · ${esc((x.created_at || "").slice(0, 10))}</td>
        <td class="r" style="padding-right:18px;white-space:nowrap">${x.id === d.active ? '<span class="pill p-safe">active</span>' : admin ? `<button class="btn sm" data-activate="${x.id}">Activate</button>${x.readonly ? "" : ` <button class="btn sm" data-del="${x.id}">Delete</button>`}` : ""}</td></tr>`).join("")}
    </table></section>
    ${admin ? `<section class="card" id="upload"><div class="head"><h2>Upload your data</h2><span class="meta">CSV · up to ${d.max_upload_mb} MB · at least ${d.min_days} days</span></div>
      <p class="note" style="margin-top:0">Hourly totals or individual transactions both work: rows are added up into clock hours, missing hours count as zero flow, and market columns you don't have use standard values (shown as assumed). Nothing leaves this server.</p>
      <label class="drop" id="drop"><input type="file" id="file" accept=".csv,text/csv" hidden>Drop a CSV here or click to choose a file</label>
      <div id="wiz"></div></section>` : ""}
    <section class="card"><div class="head"><h2>Live feed</h2><span class="meta">${act.readonly ? "upload a dataset to stream into it" : act.live ? "receiving rows" : "ready for rows"}</span></div>
      <div class="figs g4">
        ${fig("Newest hour", esc(eng.last_ts ? nice(eng.last_ts) : "—"))}
        ${fig("Last row received", eng.last_ingest_age_s == null ? "—" : `${Math.round(eng.last_ingest_age_s)} s ago`, eng.stale ? esc(eng.stale_reason) : "")}
        ${fig("Full retrain in", eng.hours_until_retrain == null ? "—" : `${eng.hours_until_retrain} h of data`, "re-scores on every batch")}
        ${fig("Last rebuild", eng.last_build_seconds == null ? "—" : `${eng.last_build_seconds} s`)}
      </div>
      <p class="note">Push rows with an API token (create one under Trust → Users) from a bank export job, a webhook or the bundled feeder:</p>
      <pre class="code">curl -X POST ${esc(host)}/api/live/rows \\
  -H "Authorization: Bearer $RJ_TOKEN" -H "Content-Type: application/json" \\
  -d '{"rows":[{"timestamp":"${esc(eng.last_ts ? nextHour(eng.last_ts) : "2026-11-16 10:00")}","deposits_usd":1200000,"withdrawals_usd":950000}]}'</pre>
      <pre class="code" style="margin-top:10px">python tools/live_feed.py --url ${esc(host)} --token $RJ_TOKEN --csv data/treasury_4tier_dataset.csv --start "2026-11-16 10:00" --every 10</pre>
    </section>
  </div>`;
  el.querySelectorAll("[data-activate]").forEach((b) => (b.onclick = () => activateDataset(d.datasets.find((x) => x.id === b.dataset.activate))));
  el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    if (!confirm("Delete this dataset file? Its audit records stay.")) return;
    try { await api(`/api/datasets/${b.dataset.del}`, { method: "DELETE" }); toast("Dataset deleted"); route(); } catch (e) { toast(e.message); }
  }));
  if (!admin) return;
  $("#goUp").onclick = (e) => { e.preventDefault(); $("#upload").scrollIntoView({ behavior: "smooth" }); };
  const drop = $("#drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) startUpload(e.dataTransfer.files[0], d); };
  $("#file").onchange = (e) => { if (e.target.files[0]) startUpload(e.target.files[0], d); };
}
function nextHour(s) { const t = new Date(s.replace(" ", "T") + ":00Z"); t.setUTCHours(t.getUTCHours() + 1); return t.toISOString().slice(0, 16).replace("T", " "); }
async function startUpload(file, d) {
  const wiz = $("#wiz");
  if (file.size > d.max_upload_mb * 1024 * 1024) { wiz.innerHTML = `<p class="bad">That file is over ${d.max_upload_mb} MB.</p>`; return; }
  loading(wiz, `Reading ${esc(file.name)}…`);
  const raw = await file.text();
  let ins;
  try { ins = await api("/api/datasets/inspect", { method: "POST", body: raw, headers: { "Content-Type": "text/csv" } }); }
  catch (e) { wiz.innerHTML = `<p class="bad" style="margin-top:14px">${esc(e.message)}</p>`; return; }
  const opt = (field) => `<select data-f="${field}"><option value="">— not in the file —</option>${ins.columns.map((c) => `<option ${ins.suggested[field] === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  wiz.innerHTML = `<div class="stack" style="gap:18px;margin-top:18px">
    <div class="row between"><h2 style="margin:0">${esc(file.name)} · ${ins.rows.toLocaleString()} rows · ${ins.columns.length} columns</h2></div>
    <div class="sample"><table><tr>${ins.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>${ins.sample.map((r) => `<tr>${r.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</table></div>
    <div class="grid g2" style="align-items:start">
      <div><h3 style="margin:0 0 8px">Match the columns</h3><p class="note" style="margin-top:0">We guessed from the headers. Map deposits + withdrawals, or a single net-flow column.</p>
        <table class="maptbl">${Object.entries(ins.fields).map(([f, lab]) => `<tr><td>${esc(lab)}</td><td style="width:50%">${opt(f)}</td></tr>`).join("")}</table></div>
      <div class="stack" style="gap:14px">
        <label class="field"><span>Name</span><input class="inp" id="dsName" value="${esc(file.name.replace(/\.csv$/i, ""))}" maxlength="80"></label>
        <label class="check"><input type="checkbox" id="dayfirst" ${ins.dayfirst_guess ? "checked" : ""}> Dates are day-first (31/12/2026)</label>
        <button class="btn primary" id="doImport">Check and import</button>
        <div id="impOut"></div></div></div></div>`;
  $("#doImport").onclick = async () => {
    const mapping = {}; wiz.querySelectorAll("select[data-f]").forEach((s) => { if (s.value) mapping[s.dataset.f] = s.value; });
    const q = new URLSearchParams({ name: $("#dsName").value, source: file.name, dayfirst: $("#dayfirst").checked ? "true" : "false", mapping: JSON.stringify(mapping) });
    const out = $("#impOut"); loading(out, "Checking every row…"); $("#doImport").disabled = true;
    try {
      const r = await api(`/api/datasets?${q}`, { method: "POST", body: raw, headers: { "Content-Type": "text/csv" } });
      const rep = r.report;
      out.innerHTML = `<div class="card mint" style="padding:14px"><strong>Imported ${rep.rows.toLocaleString()} hours</strong> (${esc(rep.start.slice(0, 16))} → ${esc(rep.end.slice(0, 16))}).
        <ul style="margin:8px 0 0;padding-left:18px">${rep.notes.map((n) => `<li>${esc(n)}</li>`).join("")}${rep.assumed.length ? `<li>Assumed (not in file): ${esc(rep.assumed.join(", "))}</li>` : ""}</ul></div>
        <button class="btn primary" id="actNew" style="margin-top:12px">Activate this dataset…</button>`;
      $("#actNew").onclick = () => activateDataset(r.dataset);
    } catch (e) { out.innerHTML = `<p class="bad">${esc(e.message)}</p>`; $("#doImport").disabled = false; }
  };
}
function activateDataset(x) {
  const b = x.balances || S.meta.balances;
  const box = document.createElement("div");
  box.className = "card"; box.style.marginTop = "18px";
  box.innerHTML = `<h2>Activate “${esc(x.name)}”</h2>
    <p class="note">The models retrain on this data (usually under a minute); screens keep the current data until it's ready.</p>
    ${x.id === "demo" ? "" : `<div class="grid g3" style="margin:12px 0">${["cash", "bonds", "equities"].map((k) => `<label class="field"><span>Starting ${k}, $M ${badge("A")}</span><input type="number" id="ab_${k}" min="0" step="any" value="${(b[k] / 1e6).toFixed(2)}"></label>`).join("")}</div>
      <label class="check"><input type="checkbox" id="scalePol" checked> Scale policy amounts (minimum cash, buffers, approval threshold) to these flows</label>`}
    <div class="row" style="margin-top:14px"><button class="btn primary" id="actGo">Activate</button><button class="btn" id="actNo">Cancel</button></div><div id="actOut"></div>`;
  ($("#wiz") || $("#page .stack")).appendChild(box);
  box.scrollIntoView({ behavior: "smooth", block: "center" });
  $("#actNo").onclick = () => box.remove();
  $("#actGo").onclick = async () => {
    const body = x.id === "demo" ? {} : { balances: Object.fromEntries(["cash", "bonds", "equities"].map((k) => [k, +$(`#ab_${k}`).value * 1e6])), scale_policy: $("#scalePol").checked };
    $("#actGo").disabled = true;
    try {
      const v0 = (await api("/api/live/status")).version;
      await api(`/api/datasets/${x.id}/activate`, { method: "POST", body: JSON.stringify(body) });
      Boot.show("Training models on the new data…");
      for (let i = 0; i < 400; i++) {
        await sleep(1500);
        const st = await api("/api/live/status");
        Boot.status(st.message);
        if (st.version !== v0 && !st.building && st.dataset && st.dataset.id === x.id) break;
        if (!st.building && st.error && i > 2) { Boot.hide(); $("#actOut").innerHTML = `<p class="bad">${esc(st.message)}</p>`; $("#actGo").disabled = false; return; }
      }
      S.meta = await api("/api/meta"); S.t = S.meta.default_t; S.follow = S.meta.live;
      toast(`Now using ${x.name}`); Boot.hide();
      if (S.page === "overview") route(); else goto("overview");
    } catch (e) { Boot.hide(); $("#actOut").innerHTML = `<p class="bad">${esc(e.message)}</p>`; $("#actGo").disabled = false; }
  };
}

/* ---------- copilot ---------- */
function initCopilot() {
  const dr = $("#drawer"), msgs = $("#msgs");
  $("#fab").onclick = () => dr.classList.add("open");
  $("#closeDrawer").onclick = () => dr.classList.remove("open");
  const session = Math.random().toString(36).slice(2, 10);
  const starter = ["How are we doing?", "Cheapest way to cover $50M for 12 hours?", "Can we survive 3x withdrawals for 48 hours?",
    "What's the smallest shock that breaks us?", "How did the optimized policy do?", "What is night-lock?"];
  const chips = (list) => `<div class="suggest">${list.map((s) => `<button type="button">${esc(s)}</button>`).join("")}</div>`;
  msgs.innerHTML = `<div class="msg"><strong>Hi, I'm the Reserve Juggler assistant.</strong>
I run on this machine, with no API key, and answer from the project's own engines, so every number is real for the dataset. Times default to the as-of hour in the side panel.</div>
    ${S.meta.copilot ? '<label class="check" style="font-size:13px"><input type="checkbox" id="useClaude"> Use Claude instead</label>' : ""}
    ${chips(starter)}`;
  // markdown-lite: **bold**, "- " bullets and "  - " sub-bullets, everything else escaped
  const render = (text) => {
    const out = [];
    let list = null;
    for (const raw of String(text).split("\n")) {
      const sub = /^\s{2,}- /.test(raw), bullet = /^- /.test(raw) || sub;
      const body = esc(raw.replace(/^\s*- /, "")).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
      if (bullet) { if (!list) { out.push('<ul style="margin:6px 0 0;padding-left:18px">'); list = true; } out.push(`<li style="${sub ? "margin-left:16px;list-style:circle" : ""}">${body}</li>`); }
      else { if (list) { out.push("</ul>"); list = null; } if (body) out.push(`<div style="margin-top:4px">${body}</div>`); }
    }
    if (list) out.push("</ul>");
    return out.join("");
  };
  const bindChips = (root) => root.querySelectorAll(".suggest button").forEach((b) => (b.onclick = () => { $("#askInput").value = b.textContent; $("#askForm").requestSubmit(); }));
  bindChips(msgs);
  $("#askForm").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("#askInput").value.trim(); if (!q) return;
    $("#askInput").value = "";
    msgs.querySelectorAll(".suggest").forEach((s) => s.remove());
    msgs.insertAdjacentHTML("beforeend", `<div class="msg me">${esc(q)}</div>`);
    const wait = document.createElement("div"); wait.className = "msg"; wait.innerHTML = `<span class="spin"></span>Running the simulation…`; msgs.appendChild(wait);
    msgs.scrollTop = msgs.scrollHeight;
    try {
      if ($("#useClaude") && $("#useClaude").checked) {
        const r = await api("/api/copilot", { method: "POST", body: JSON.stringify({ question: q }) });
        const tools = (r.tools || []).map((t) => `${t.tool}${t.run_id ? ` #${t.run_id}` : ""}`).join(" · ");
        wait.innerHTML = r.error ? `<span class="bad">${esc(r.error)}</span>` : `${render(r.answer)}${tools ? `<div class="tools">${esc(tools)}</div>` : ""}`;
        if (r.disabled) { $("#useClaude").closest("label").remove(); S.meta.copilot = false; }
      } else {
        const r = await api("/api/assistant", { method: "POST", body: JSON.stringify({ question: q, t: S.t, session }) });
        const meta = [r.as_of ? `as of ${nice(r.as_of)}` : "", ...(r.runs || []).map((id) => `decision #${id}`)].filter(Boolean).join(" · ");
        wait.innerHTML = `${render(r.answer)}${meta ? `<div class="tools">${esc(meta)}</div>` : ""}`;
        if (r.suggestions && r.suggestions.length) { msgs.insertAdjacentHTML("beforeend", chips(r.suggestions)); bindChips(msgs); }
      }
    } catch (err) { wait.innerHTML = `<span class="bad">${esc(err.message)}</span>`; }
    msgs.scrollTop = msgs.scrollHeight;
  };
}

function start() {
  boot().catch((e) => {
    Boot.fail("Can't reach the engine. Start it with python backend/app.py, then try again.", () => { Boot.status("Connecting to the engine…"); start(); });
    $("#page").innerHTML = `<div class="card bad">Could not reach the API: ${esc(e.message)}. Start it with <span class="mono">python backend/app.py</span>.</div>`;
  });
}
start();
