/* Landing page: headline numbers and the assistant box use the public endpoints (RJ_PUBLIC_DEMO).
   With public demo switched off they answer 404 and the page simply invites people to sign in. */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = (x) => { const a = Math.abs(x), s = x < 0 ? "−" : ""; return a >= 1e9 ? `${s}$${(a / 1e9).toFixed(2)}B` : a >= 1e6 ? `${s}$${(a / 1e6).toFixed(1)}M` : a >= 1e3 ? `${s}$${(a / 1e3).toFixed(1)}K` : `${s}$${a.toFixed(0)}`; };
const get = (p, o) => fetch(p, o).then((r) => { if (!r.ok) { const e = new Error(r.status); e.status = r.status; throw e; } return r.json(); });

async function loadLive() {
  for (let i = 0; i < 40; i++) {
    try {
      const d = await get("/api/public/headline");
      const st = { safe: ["p-safe", "Safe"], watch: ["p-watch", "Watch"], breach: ["p-breach", "Breach"] }[d.status];
      $("#lStatus").className = `pill ${st[0]}`; $("#lStatus").textContent = st[1];
      $("#lAction").textContent = d.next_action.title;
      $("#lDetail").textContent = d.next_action.detail;
      $("#lCash").textContent = money(d.cash); $("#lReserve").textContent = money(d.reserve);
      $("#lAsOf").textContent = `${d.live ? "LIVE" : "LEDGER"} · ${d.ts} · CIRCUIT BREAKER ${d.breaker ? "ON" : "ARMED"}`;
      $("#sErr").textContent = `${Math.round(d.error_reduction * 100)}%`;
      $("#sCov").textContent = `${(d.coverage * 100).toFixed(1)}%`;
      $("#sGain").textContent = `${d.gain_vs_fixed >= 0 ? "+" : "−"}${money(Math.abs(d.gain_vs_fixed)).replace("−", "")}`;
      $("#sGap").textContent = String(d.gap_hours);
      return;
    } catch (e) {
      if (e.status === 404) { privateMode(); return; }
      $("#lAction").textContent = e.status === 503 ? "The engine is warming up…" : "Start the server to see live numbers";
      $("#lStatus").textContent = e.status === 503 ? "Loading" : "Offline";
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}
function privateMode() {
  $("#lStatus").textContent = "Private";
  $("#lAction").textContent = "Sign in to see your treasury";
  $("#lDetail").textContent = "Live numbers are only shown to signed-in users on this server.";
  $("#tryAnswer").innerHTML = '<span class="dim">The assistant is available after you sign in.</span>';
  $("#tryForm").querySelector("button").disabled = true;
}

const session = Math.random().toString(36).slice(2, 10);
function render(text) {
  const out = []; let list = false;
  for (const raw of String(text).split("\n")) {
    const bullet = /^\s*- /.test(raw), body = esc(raw.replace(/^\s*- /, "")).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    if (bullet) { if (!list) { out.push('<ul style="margin:6px 0 0;padding-left:18px">'); list = true; } out.push(`<li>${body}</li>`); }
    else { if (list) { out.push("</ul>"); list = false; } if (body) out.push(`<div style="margin-top:4px">${body}</div>`); }
  }
  if (list) out.push("</ul>");
  return out.join("");
}
function chips(list) {
  $("#tryChips").innerHTML = list.map((s) => `<button type="button">${esc(s)}</button>`).join("");
  $("#tryChips").querySelectorAll("button").forEach((b) => (b.onclick = () => { $("#tryInput").value = b.textContent; $("#tryForm").requestSubmit(); }));
}
chips(["How are we doing?", "Cheapest way to cover $50M for 12 hours?", "What is night-lock?"]);
$("#tryForm").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#tryInput").value.trim(); if (!q) return;
  $("#tryAnswer").innerHTML = '<span class="spin"></span>Running the simulation…';
  try {
    const r = await get("/api/public/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q, session }) });
    $("#tryAnswer").innerHTML = `<div style="font-weight:700;margin-bottom:6px">“${esc(q)}”</div>${render(r.answer)}`;
    if (r.suggestions) chips(r.suggestions);
  } catch (err) {
    $("#tryAnswer").innerHTML = err.status === 429 ? '<span class="bad">That was a lot of questions. Try again in a minute.</span>'
      : err.status === 404 ? '<span class="dim">The assistant is available after you sign in.</span>'
      : `<span class="bad">Couldn't reach the assistant (${esc(err.message)}). Is the server running?</span>`;
  }
};
loadLive();
