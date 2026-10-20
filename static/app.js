const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const signedPct = (x, d = 1) => (x == null ? "–" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(d)}%`);
const odds = (o) => (o > 0 ? `+${o}` : `${o}`);
const money = (x) => `$${Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

const state = {
  meta: null,
  props: [],
  open: new Set(),
  projections: [],
  books: [],
  projSort: { k: "PTS", asc: false },
  settings: loadSettings(),
  sport: (() => { try { return localStorage.getItem("sport") === "nfl" ? "nfl" : "nba"; } catch { return "nba"; } })(),
  view: "props",
  nfl: { props: [], books: [], open: new Set(), settings: loadNflSettings(), loaded: false },
};

function loadSettings() {
  const d = { ev_threshold: 0.03, market_blend: 0.35, bankroll: 1000, kelly_mult: 0.25, books: [], bias_adjust: true };
  try { return { ...d, ...JSON.parse(localStorage.getItem("settings") || "{}") }; } catch { return d; }
}
function saveSettings() {
  try { localStorage.setItem("settings", JSON.stringify(state.settings)); } catch {}
}
function loadNflSettings() {
  const d = { ev_threshold: 0.05, market_blend: 0.6, bankroll: 1000, kelly_mult: 0.25, books: [] };
  try { return { ...d, ...JSON.parse(localStorage.getItem("nfl_settings") || "{}") }; } catch { return d; }
}
function saveNflSettings() {
  try { localStorage.setItem("nfl_settings", JSON.stringify(state.nfl.settings)); } catch {}
}

async function api(path, opts = {}) {
  const res = await fetch(`/api/${path}`, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  for (;;) {
    let s;
    try { s = await api("status"); } catch { s = { status: "loading", log: ["Waiting for server…"] }; }
    if (s.status === "ready") break;
    $("#loading-msg").textContent = s.status === "error" ? `Failed to load data: ${s.error}` : (s.log?.slice(-1)[0] || "Loading…");
    await new Promise((r) => setTimeout(r, 800));
  }
  $("#loading").hidden = true;
  state.meta = await api("meta");
  fillSelects();
  bindUI();
  nflBindUI();
  setSport(state.sport, location.hash.slice(1) || "props");
  await loadProps();
  refreshStatus();
}

function fillSelects() {
  const statOpts = state.meta.stats.map((s) => `<option value="${s.key}">${esc(s.label)}</option>`).join("");
  $("#prop-stat").insertAdjacentHTML("beforeend", statOpts);
  $("#add-stat").innerHTML = statOpts;
  const teamOpts = state.meta.teams.map((t) => `<option value="${t.key}">${t.key}: ${esc(t.name)}</option>`).join("");
  $("#add-opp").insertAdjacentHTML("beforeend", teamOpts);
  $("#proj-team").insertAdjacentHTML("beforeend", state.meta.teams.map((t) => `<option value="${t.key}">${t.key}</option>`).join(""));
  $("#player-list").innerHTML = state.meta.players.map((n) => `<option value="${esc(n)}">`).join("");
  $("#set-ev").value = +(state.settings.ev_threshold * 100).toFixed(2);
  $("#set-blend").value = Math.round(state.settings.market_blend * 100);
  $("#set-bankroll").value = state.settings.bankroll;
  $("#set-kelly").value = state.settings.kelly_mult;
  $("#set-bias").checked = state.settings.bias_adjust !== false;
}

function showView(v) {
  if (!["props", "projections", "model"].includes(v)) v = "props";
  state.view = v;
  const id = state.sport === "nfl" ? `view-nfl-${v}` : `view-${v}`;
  $$(".view").forEach((el) => (el.hidden = el.id !== id));
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  history.replaceState(null, "", `#${v}`);
  if (state.sport === "nfl") {
    if (v === "props" || (v === "projections" && !state.nfl.loaded)) loadNfl();
    if (v === "projections") renderNflProjections();
    if (v === "model") loadNflModel();
    return;
  }
  if (v === "projections" && !state.projections.length) loadProjections();
  if (v === "model") { refreshStatus(); loadModelReport(); loadYearly(); loadLines(); loadBacktest(); }
}

function setSport(sport, view) {
  state.sport = sport === "nfl" ? "nfl" : "nba";
  try { localStorage.setItem("sport", state.sport); } catch {}
  $$("#sport-switch button").forEach((b) => b.classList.toggle("active", b.dataset.sport === state.sport));
  const name = state.sport === "nfl" ? "NFL Prop Model" : "NBA Prop Model";
  $("#brand-name").textContent = name;
  document.title = name;
  $("#season-tag").hidden = state.sport === "nfl";
  showView(view || state.view);
}

/* ------------------------------------------------------------------ props */

function settingsQuery() {
  return new URLSearchParams(Object.entries(state.settings).map(([k, v]) => [k, Array.isArray(v) ? v.join(",") : String(v)])).toString();
}

async function loadProps() {
  const { props, books } = await api(`props?${settingsQuery()}`);
  state.props = props;
  state.books = books;
  renderBookFilter();
  renderProps();
}

function renderBookFilter() {
  const box = $("#book-filter");
  if (!state.books.length) { box.innerHTML = `<span class="muted small">Books appear here once lines from more than one book are loaded.</span>`; return; }
  const mine = new Set(state.settings.books.map((b) => b.toLowerCase()));
  box.innerHTML = state.books.map((b) => `<label class="check"><input type="checkbox" value="${esc(b)}" ${!mine.size || mine.has(b.toLowerCase()) ? "checked" : ""}> ${esc(b)}</label>`).join("");
}

function filteredProps() {
  const q = $("#prop-search").value.trim().toLowerCase();
  const stat = $("#prop-stat").value;
  const dec = $("#prop-decision").value;
  return state.props.filter((r) => {
    if (q && !r.prop.player.toLowerCase().includes(q)) return false;
    if (stat && r.prop.stat !== stat) return false;
    const d = r.ok ? r.ev.decision : "PASS";
    if (dec === "bets" && d === "PASS") return false;
    if (dec && dec !== "bets" && d !== dec) return false;
    return true;
  }).sort((a, b) => {
    const rank = (r) => (!r.ok ? -9 : r.ev.decision === "PASS" ? r.ev.best_ev - 1 : r.ev.best_ev);
    return rank(b) - rank(a);
  });
}

function renderTiles() {
  const ok = state.props.filter((r) => r.ok);
  const bets = ok.filter((r) => r.ev.decision !== "PASS");
  const avgEv = bets.length ? bets.reduce((s, r) => s + r.ev.best_ev, 0) / bets.length : null;
  const stake = bets.reduce((s, r) => s + r.ev.sides[r.ev.decision].stake, 0);
  const tiles = [
    ["Props analyzed", ok.length],
    ["Model picks (paper)", bets.length],
    ["Avg model EV of picks", avgEv == null ? "–" : signedPct(avgEv)],
    ["Paper stake if all were bet", money(stake)],
  ];
  $("#tiles").innerHTML = tiles.map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("#example-notice").hidden = !state.props.some((r) => r.prop.source === "example");
}

function renderProps() {
  renderTiles();
  const rows = filteredProps();
  const statLabel = Object.fromEntries(state.meta.stats.map((s) => [s.key, s.label]));
  $("#props-empty").hidden = state.props.length > 0;
  $("#props-table tbody").innerHTML = rows.map((r) => {
    const p = r.prop;
    const open = state.open.has(r.id);
    const venue = p.home == null ? "" : p.home ? "vs" : "@";
    const matchup = p.opponent ? `${venue || "vs"} ${p.opponent}` : "Opponent n/a";
    const team = r.player?.team ? `${r.player.team} · ` : "";
    const date = p.game_date ? ` · ${p.game_date}` : "";
    const nBooks = r.offers ? r.offers.length : 1;
    const src = p.source === "example" ? `<span class="tag">example</span>` : nBooks > 1 ? `<span class="tag">${nBooks} books</span>` : "";
    const head = `<td class="player-cell"><b>${esc(r.player?.name || p.player)}</b>${src}<div class="sub">${team}${matchup}${date}${p.b2b ? " · B2B" : ""}</div></td>
      <td>${esc(p.stat)} <span class="muted">${p.line}</span><div class="sub muted small">${esc(statLabel[p.stat] || "")}</div></td>
      <td class="num">${odds(p.over_odds)} / ${odds(p.under_odds)}${p.book && p.source !== "example" ? `<div class="sub muted small">${esc(p.book)}</div>` : ""}</td>`;
    const ids = (r.offers || [{ prop: p }]).map((o) => o.prop.id).join(",");
    const del = `<td class="num"><button class="icon-btn" data-del="${ids}" title="Remove">×</button> <span class="chev">›</span></td>`;
    if (!r.ok) {
      return `<tr class="row" data-id="${r.id}">${head}<td colspan="4" class="muted">${esc(r.error)}</td>${del}</tr>`;
    }
    const pr = r.projection, e = r.ev;
    const diff = pr.mean - p.line;
    const best = e.sides[e.best_side];
    const decision = e.decision === "PASS"
      ? `<span class="badge PASS">PASS</span>`
      : `<span class="badge ${e.decision}">${e.decision} <span class="tier">· ${e.tier}</span></span>`;
    let html = `<tr class="row${open ? " open" : ""}" data-id="${r.id}">${head}
      <td class="num" title="${pr.bias_shift ? `Raw model ${pr.raw_mean.toFixed(1)}, adjusted ${pr.bias_shift < 0 ? "+" : "-"}${Math.abs(pr.bias_shift).toFixed(1)} for tonight's model-vs-market gap` : "Model projection"}"><b>${pr.mean.toFixed(1)}</b> <span class="${diff >= 0 ? "pos" : "neg"} small">${diff >= 0 ? "+" : ""}${diff.toFixed(1)}</span></td>
      <td class="num">${pct(pr.p_over)}</td>
      <td class="num ${best.ev >= 0 ? "pos" : "neg"}">${signedPct(best.ev)} <span class="muted small">${e.best_side[0]}</span></td>
      <td>${decision}</td>${del}</tr>`;
    if (open) html += `<tr class="detail"><td colspan="8">${detailHTML(r)}</td></tr>`;
    return html;
  }).join("");
}

function detailHTML(r) {
  const e = r.ev, p = r.prop;
  const reasons = r.reasoning.points.map((pt) => {
    const sym = pt.lean === "over" ? "▲" : pt.lean === "under" ? "▼" : "•";
    return `<li><span class="lean ${pt.lean}">${sym}</span><span>${esc(pt.text)}</span></li>`;
  }).join("");
  const evRows = ["OVER", "UNDER"].map((side) => {
    const s = e.sides[side];
    return `<tr class="${side === e.decision ? "best" : ""}">
      <td>${side === "OVER" ? "Over" : "Under"} ${p.line} (${odds(s.odds)})</td>
      <td>${pct(s.model_prob)}</td><td>${pct(s.market_prob)}</td><td>${pct(s.breakeven)}</td>
      <td class="${s.ev >= 0 ? "pos" : "neg"}">${signedPct(s.ev)}</td>
      <td>${s.stake ? money(s.stake) : "–"}</td></tr>`;
  }).join("");
  const conf = e.low_confidence ? `<p class="legend-note">Low-confidence spot (small sample or new team): EV bar raised to ${pct(e.threshold)}.</p>` : "";
  const push = e.push_prob > 0.001 ? `<p class="legend-note">Push probability ${pct(e.push_prob)} (whole-number line).</p>` : "";
  return `<div class="detail-grid">
    <div>
      <p class="summary">${esc(r.reasoning.summary)}</p>
      <ul class="reasons">${reasons}</ul>
    </div>
    <div>
      <div class="panel">
        <h4>Expected value</h4>
        <table class="ev-table">
          <thead><tr><th>Side</th><th>Model</th><th>Market</th><th>Break-even</th><th>EV</th><th>Stake</th></tr></thead>
          <tbody>${evRows}</tbody>
        </table>
        ${conf}${push}
        <p class="legend-note">EV uses ${Math.round((1 - state.settings.market_blend) * 100)}% model and ${Math.round(state.settings.market_blend * 100)}% market probability. Stake is ${state.settings.kelly_mult}× Kelly on a ${money(state.settings.bankroll)} bankroll.</p>
      </div>
      ${booksPanel(r)}
      <div class="panel chart">
        <h4>Last ${r.recent.length} games vs line</h4>
        ${barChart(r.recent, p.line, r.projection.mean)}
        <p class="legend-note">Bars: actual results. Dashed: line ${p.line}. Solid: projection ${r.projection.mean.toFixed(1)}.</p>
      </div>
    </div>
  </div>`;
}

function booksPanel(r) {
  if (!r.offers || r.offers.length < 2) return "";
  const rows = [...r.offers].sort((a, b) => b.ev.best_ev - a.ev.best_ev).map((o) => {
    const p = o.prop, e = o.ev;
    const best = p.id === r.best_offer;
    const evc = (x) => `<span class="${x >= 0 ? "pos" : "neg"}">${signedPct(x)}</span>`;
    return `<tr class="${best ? "best" : ""}${o.eligible ? "" : " dim"}">
      <td>${esc(p.book || "Manual")}${best ? " ★" : ""}</td><td>${p.line}</td>
      <td>${odds(p.over_odds)}</td><td>${odds(p.under_odds)}</td>
      <td>${evc(e.sides.OVER.ev)}</td><td>${evc(e.sides.UNDER.ev)}</td></tr>`;
  }).join("");
  return `<div class="panel"><h4>Line shopping</h4>
    <table class="ev-table"><thead><tr><th>Book</th><th>Line</th><th>Over</th><th>Under</th><th>Over EV</th><th>Under EV</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="legend-note">★ best price among your books. Greyed books are excluded in Settings.</p></div>`;
}

function barChart(games, line, proj) {
  if (!games.length) return `<p class="muted small">No games yet.</p>`;
  const W = 360, H = 130, pad = { l: 22, r: 6, t: 8, b: 26 };
  const max = Math.max(line, proj, ...games.map((g) => g.value)) * 1.15 || 1;
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const bw = iw / games.length;
  const y = (v) => pad.t + ih - (v / max) * ih;
  const bars = games.map((g, i) => {
    const x = pad.l + i * bw + bw * 0.18;
    const over = g.value > line;
    const h = Math.max(1, (g.value / max) * ih);
    return `<rect x="${x}" y="${y(g.value)}" width="${bw * 0.64}" height="${h}" rx="2" fill="var(${over ? "--over" : "--bar"})"><title>${g.date} ${g.home ? "vs" : "@"} ${g.opp}: ${g.value} (${g.min} min)</title></rect>
      <text x="${x + bw * 0.32}" y="${H - 14}" text-anchor="middle">${g.opp}</text>
      <text x="${x + bw * 0.32}" y="${y(g.value) - 3}" text-anchor="middle">${g.value}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Last games versus line">
    ${bars}
    <line x1="${pad.l}" x2="${W - pad.r}" y1="${y(line)}" y2="${y(line)}" stroke="var(--under)" stroke-dasharray="4 3" stroke-width="1.2"/>
    <line x1="${pad.l}" x2="${W - pad.r}" y1="${y(proj)}" y2="${y(proj)}" stroke="var(--accent)" stroke-width="1.2"/>
    <text x="2" y="${y(line) + 3}">${line}</text>
  </svg>`;
}

/* ------------------------------------------------------------------ add prop */

function formPayload() {
  const f = $("#add-form");
  const v = Object.fromEntries(new FormData(f).entries());
  return {
    ...v,
    home: v.home === "" ? null : v.home === "home",
    b2b: f.b2b.checked,
    source: "manual",
    settings: state.settings,
  };
}

let previewTimer;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    const body = formPayload();
    if (!body.player || body.line === "") { $("#add-preview").innerHTML = ""; return; }
    try {
      const r = await api("evaluate", { method: "POST", body });
      $("#add-error").textContent = "";
      $("#add-preview").innerHTML = r.ok
        ? `Projection <b>${r.projection.mean.toFixed(1)}</b> · Over ${pct(r.projection.p_over)} · ${esc(r.reasoning.summary)}`
        : esc(r.error);
    } catch (err) {
      $("#add-preview").innerHTML = "";
      $("#add-error").textContent = err.message;
    }
  }, 350);
}

async function submitProp(ev) {
  ev.preventDefault();
  try {
    const r = await api("props", { method: "POST", body: formPayload() });
    state.open.add(r.prop.id);
    $("#add-dialog").close();
    $("#add-form").reset();
    $("#add-preview").innerHTML = "";
    await loadProps();
  } catch (err) {
    $("#add-error").textContent = err.message;
  }
}

/* ------------------------------------------------------------------ projections */

async function loadProjections() {
  $("#proj-table tbody").innerHTML = `<tr><td colspan="10" class="muted">Loading…</td></tr>`;
  const d = await api("projections");
  state.projections = d.rows;
  $("#proj-title").textContent = `${d.season} per-game projections`;
  renderProjections();
}

function renderProjections() {
  const q = $("#proj-search").value.trim().toLowerCase();
  const team = $("#proj-team").value;
  const moved = $("#proj-moved").checked;
  const { k, asc } = state.projSort;
  const rows = state.projections
    .filter((r) => (!q || r.name.toLowerCase().includes(q)) && (!team || r.team === team) && (!moved || r.changed_team))
    .sort((a, b) => {
      const av = a[k] ?? (typeof a[k] === "string" ? "" : -1), bv = b[k] ?? -1;
      const c = typeof av === "string" ? av.localeCompare(bv) : av - bv;
      return asc ? c : -c;
    });
  $$("#proj-table th[data-k]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.k === k);
    th.classList.toggle("asc", th.dataset.k === k && asc);
  });
  const f = (v) => (v == null ? "–" : v.toFixed(1));
  $("#proj-table tbody").innerHTML = rows.slice(0, 400).map((r) => r.rookie
    ? `<tr class="row" data-pid="${r.id}"><td class="player-cell"><b>${esc(r.name)}</b><span class="tag">no NBA history</span></td><td>${r.team}</td><td colspan="8" class="muted">Projection starts after ${8} games</td></tr>`
    : `<tr class="row" data-pid="${r.id}">
      <td class="player-cell"><b>${esc(r.name)}</b>${r.changed_team ? `<span class="tag">from ${r.prev_team}</span>` : ""}</td>
      <td>${r.team}</td><td class="num">${f(r.min)}</td><td class="num"><b>${f(r.PTS)}</b></td>
      <td class="num">${f(r.REB)}</td><td class="num">${f(r.AST)}</td><td class="num">${f(r["3PM"])}</td>
      <td class="num">${f(r.STL)}</td><td class="num">${f(r.BLK)}</td><td class="num">${f(r.PRA)}</td></tr>`).join("")
    || `<tr><td colspan="10" class="empty">No players match.</td></tr>`;
}

async function openPlayer(pid) {
  const dlg = $("#player-dialog");
  $("#player-body").innerHTML = `<p class="muted">Loading…</p>`;
  dlg.showModal();
  const d = await api(`player/${pid}`);
  const exp = d.experience != null ? ` · Year ${d.experience + 1}` : "";
  const moved = d.changed_team ? ` · New team (from ${d.prev_team})` : "";
  const boxes = d.stats.filter((s) => ["PTS", "REB", "AST", "3PM"].includes(s.stat)).map((s) =>
    `<div class="stat-box"><div class="k">${esc(s.label)}</div><div class="v">${s.mean.toFixed(1)}</div><div class="r">80% of games: ${s.low}–${s.high}</div></div>`).join("");
  const why = d.stats.map((s) => `<details class="stat-why"><summary>${esc(s.label)}: ${s.mean.toFixed(1)}</summary>
    <ul class="why">${s.reasoning.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></details>`).join("");
  const seasons = d.seasons.map((s) => `<tr><td>${s.season}</td><td>${s.team}</td><td class="num">${s.gp}</td><td class="num">${s.min}</td>
    <td class="num">${s.pts}</td><td class="num">${s.reb}</td><td class="num">${s.ast}</td><td class="num">${s.fg3m}</td></tr>`).join("");
  $("#player-body").innerHTML = `
    <h2>${esc(d.name)}</h2>
    <p class="muted">${esc(d.team_name || "")}${exp}${moved} · ${d.projected_minutes ?? "–"} projected mpg</p>
    ${d.stats.length ? `<div class="stat-grid">${boxes}</div>${why}` : `<p class="muted" style="margin:14px 0">Not enough NBA games to project yet.</p>`}
    ${seasons ? `<h3 style="margin-top:16px">History</h3>
    <table class="table"><thead><tr><th>Season</th><th>Team</th><th class="num">GP</th><th class="num">MIN</th><th class="num">PTS</th><th class="num">REB</th><th class="num">AST</th><th class="num">3PM</th></tr></thead>
    <tbody>${seasons}</tbody></table>` : ""}`;
}

/* ------------------------------------------------------------------ model view */

async function refreshStatus() {
  const s = await api("status");
  $("#season-tag").textContent = s.meta ? s.meta.current_season : "";
  if (!s.meta) return;
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "–");
  $("#data-status").innerHTML = `<dl class="kv">
    <dt>History</dt><dd>${s.meta.seasons[0].season} to ${s.meta.seasons[s.meta.seasons.length - 1].season}: ${s.meta.seasons.reduce((a, x) => a + x.rows, 0).toLocaleString()} player-games</dd>
    <dt>Rosters</dt><dd>${s.meta.roster_players} players (${when(s.meta.roster_fetched)})</dd>
    <dt>Built</dt><dd>${when(s.meta.built)}</dd>
    <dt>Projections</dt><dd>${s.model === "ml" ? "Machine-learning model" : "Formula model (train the ML model below)"}</dd>
    <dt>Live lines</dt><dd>Action Network (free)${s.odds_api ? " + The Odds API" : ". Add an Odds API key in config.json for a second source"}</dd>
  </dl>`;
}

async function loadBacktest() {
  const d = await api("backtest");
  const btn = $("#run-backtest");
  if (d.status === "running") {
    btn.disabled = true;
    $("#bt-body").innerHTML = `<p class="muted">Running walk-forward backtest…</p><div class="progress"><div style="width:${Math.round(d.progress * 100)}%"></div></div>`;
    setTimeout(loadBacktest, 700);
    return;
  }
  btn.disabled = false;
  if (!d.result) return;
  const r = d.result;
  $("#bt-desc").textContent = `Walk-forward test on ${r.season} games from ${r.start_date}: every prediction uses only games played before that date.`;
  const row = (k, s) => {
    const better = s.mae_model <= Math.min(s.mae_season_avg, s.mae_last10);
    return `<tr><td>${k}</td><td class="num">${s.n.toLocaleString()}</td>
      <td class="num ${better ? "pos" : ""}">${s.mae_model.toFixed(2)}</td><td class="num">${s.mae_season_avg.toFixed(2)}</td><td class="num">${s.mae_last10.toFixed(2)}</td>
      <td class="num">${s.brier.toFixed(4)}</td><td class="num muted">${s.brier_baseline.toFixed(4)}</td></tr>`;
  };
  const W = 270, H = 220, P = 30;
  const sx = (v) => P + v * (W - P - 22), sy = (v) => H - P - v * (H - P - 8);
  const dot = (c, filled) => `<circle cx="${sx(c.predicted)}" cy="${sy(c.actual)}" r="${Math.max(3, Math.min(9, Math.sqrt(c.n) / 6))}"
    fill="${filled ? "var(--accent)" : "none"}" fill-opacity=".8" stroke="${filled ? "none" : "var(--muted)"}" stroke-width="1.2">
    <title>${filled ? "Calibrated" : "Raw"} ${c.bin}: predicted ${pct(c.predicted)}, actual ${pct(c.actual)} (n=${c.n})</title></circle>`;
  const pts = r.calibration.map((c) => dot(c, false)).join("") + (r.calibration_after || []).map((c) => dot(c, true)).join("");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => `<text x="${sx(t)}" y="${H - P + 14}" text-anchor="middle">${t * 100}%</text><text x="${P - 4}" y="${sy(t) + 3}" text-anchor="end">${t * 100}%</text>`).join("");
  $("#bt-body").innerHTML = `<div class="bt-grid">
    <div>
      <table class="table">
        <thead><tr><th>Stat</th><th class="num">Games</th><th class="num">MAE model</th><th class="num">MAE season avg</th><th class="num">MAE last 10</th><th class="num">Brier</th><th class="num">Coin flip</th></tr></thead>
        <tbody>${Object.entries(r.stats).map(([k, s]) => row(k, s)).join("")}${row("All", r.overall)}</tbody>
      </table>
      <p class="legend-note" style="margin-top:10px">MAE is the average miss in stat units (lower is better). Brier scores over/under probabilities against a line set at the
        player's season-to-date average (lower is better; "coin flip" is the score from always guessing the base rate).</p>
      <p class="legend-note">Free historical sportsbook lines aren't available, so the backtest can't measure betting profit against real books.
        Against these naive average-based lines, picks hit ${pct(r.overall.bet_win_rate)}, but real lines are sharper (they're set near the median, not the mean),
        so expect far smaller edges in practice.</p>
    </div>
    <div class="chart">
      <h4 class="muted small" style="margin:0 0 6px">Calibration: predicted vs actual over rate</h4>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Calibration chart">
        <line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(1)}" y2="${sy(1)}" stroke="var(--border)" stroke-dasharray="4 3"/>
        <line x1="${P}" y1="${H - P}" x2="${W - 8}" y2="${H - P}" stroke="var(--border)"/>
        <line x1="${P}" y1="8" x2="${P}" y2="${H - P}" stroke="var(--border)"/>
        ${ticks}${pts}
      </svg>
      <p class="legend-note">Hollow: raw model. Filled: after recalibration (the probabilities used for EV). On the diagonal means the
        probabilities are honest. Dot size reflects sample size.${r.brier_after ? ` Brier ${r.overall.brier.toFixed(4)} → ${r.brier_after.toFixed(4)}.` : ""}</p>
    </div>
  </div>`;
}

async function loadModelReport() {
  const d = await api("model-report");
  const btn = $("#train-model");
  if (d.status === "running") {
    btn.disabled = true;
    $("#ml-body").innerHTML = `<p class="muted">Training… this takes several minutes. You can keep using the app.</p>
      <div class="progress"><div style="width:${Math.round(d.progress * 100)}%"></div></div>`;
    state.wasTraining = true;
    setTimeout(loadModelReport, 1500);
    return;
  }
  btn.disabled = false;
  if (state.wasTraining && d.status === "done") { state.wasTraining = false; state.projections = []; loadProps(); refreshStatus(); }
  if (d.error) { $("#ml-body").innerHTML = `<p class="error">Training failed: ${esc(d.error)}</p>`; return; }
  const r = d.report;
  if (!r) return;
  btn.textContent = "Retrain";
  $("#ml-desc").textContent = `Trained ${new Date(r.trained_at * 1000).toLocaleString()} on ${r.rows.toLocaleString()} player-games (${r.first_season} onward). ` +
    `Tuned on ${r.tune_season}, calibrated on ${r.validation_season}, scored once on held-out ${r.holdout_season}.`;
  const keys = Object.keys(r.stats);
  const f2 = (x) => x.toFixed(2), f4 = (x) => x.toFixed(4);
  const rows = keys.map((k) => {
    const h = r.stats[k].holdout;
    const win = h.mae_ml <= Math.min(h.mae_formula, h.mae_season_avg);
    const gain = 1 - h.mae_ml / h.mae_season_avg;
    return `<tr><td>${k}</td><td class="num">${h.n.toLocaleString()}</td>
      <td class="num ${win ? "pos" : ""}"><b>${f2(h.mae_ml)}</b></td><td class="num">${f2(h.mae_formula)}</td><td class="num">${f2(h.mae_season_avg)}</td>
      <td class="num ${gain > 0 ? "pos" : "neg"}">${signedPct(gain)}</td>
      <td class="num">${f4(h.brier_cal)}</td><td class="num muted">${f4(h.brier_base_rate)}</td></tr>`;
  }).join("");
  const sel = state.mlStat || "PTS";
  const top = r.stats[sel].importance;
  const imp = top.map((f) => `<div class="imp-row"><span>${esc(f.label)}</span>
    <div class="imp-bar"><div style="width:${Math.round(f.share / top[0].share * 100)}%"></div></div>
    <span class="num small muted">${(f.share * 100).toFixed(1)}%</span></div>`).join("");
  const opts = keys.map((k) => `<option ${k === sel ? "selected" : ""}>${k}</option>`).join("");
  $("#ml-body").innerHTML = `<div class="bt-grid">
    <div>
      <table class="table">
        <thead><tr><th>Stat</th><th class="num">Games</th><th class="num">MAE ML</th><th class="num">MAE formula</th><th class="num">MAE season avg</th><th class="num">vs season avg</th><th class="num">Brier</th><th class="num">Coin flip</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="legend-note" style="margin-top:10px">All numbers are on ${r.holdout_season}, a season the model never trained or tuned on, for players projected for 20+ minutes.
        MAE is the average miss in stat units. Brier scores the calibrated over/under probabilities against a line at the player's season-to-date average (lower is better).</p>
    </div>
    <div>
      <div class="imp-head"><h4 class="muted small" style="margin:0">What the model relies on</h4><select id="ml-stat">${opts}</select></div>
      <div class="imp">${imp}</div>
      <div class="chart" style="margin-top:14px">${calibrationSVG(r.calibration.raw, r.calibration.calibrated)}
        <p class="legend-note">Calibration on ${r.holdout_season}. Hollow: raw. Filled: calibrated (used for EV).</p></div>
    </div>
  </div>`;
  $("#ml-stat").addEventListener("change", (e) => { state.mlStat = e.target.value; loadModelReport(); });
}

function calibrationSVG(raw, cal) {
  const W = 270, H = 200, P = 30;
  const sx = (v) => P + v * (W - P - 22), sy = (v) => H - P - v * (H - P - 8);
  const dot = (c, filled) => `<circle cx="${sx(c.predicted)}" cy="${sy(c.actual)}" r="${Math.max(3, Math.min(9, Math.sqrt(c.n) / 8))}"
    fill="${filled ? "var(--accent)" : "none"}" fill-opacity=".8" stroke="${filled ? "none" : "var(--muted)"}" stroke-width="1.2">
    <title>${c.bin}: predicted ${pct(c.predicted)}, actual ${pct(c.actual)} (n=${c.n})</title></circle>`;
  const ticks = [0, 0.5, 1].map((t) => `<text x="${sx(t)}" y="${H - P + 14}" text-anchor="middle">${t * 100}%</text><text x="${P - 4}" y="${sy(t) + 3}" text-anchor="end">${t * 100}%</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Calibration chart">
    <line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(1)}" y2="${sy(1)}" stroke="var(--border)" stroke-dasharray="4 3"/>
    <line x1="${P}" y1="${H - P}" x2="${W - 8}" y2="${H - P}" stroke="var(--border)"/>
    <line x1="${P}" y1="8" x2="${P}" y2="${H - P}" stroke="var(--border)"/>
    ${ticks}${raw.map((c) => dot(c, false)).join("")}${cal.map((c) => dot(c, true)).join("")}</svg>`;
}

async function loadYearly() {
  const d = await api("yearly");
  const btn = $("#run-yearly");
  if (d.status === "running") {
    btn.disabled = true;
    $("#yearly-body").innerHTML = `<p class="muted">Running… retraining once per season.</p><div class="progress"><div style="width:${Math.round(d.progress * 100)}%"></div></div>`;
    setTimeout(loadYearly, 2000);
    return;
  }
  btn.disabled = false;
  if (d.error) { $("#yearly-body").innerHTML = `<p class="error">${esc(d.error)}</p>`; return; }
  const r = d.result;
  if (!r) return;
  const head = r.stats.map((k) => `<th class="num">${k}</th>`).join("");
  const rows = r.seasons.map((s) => `<tr><td>${s.season}</td>${r.stats.map((k) => {
    const v = s.stats[k];
    const gain = 1 - v.mae_ml / v.mae_season_avg;
    return `<td class="num"><span class="${gain > 0 ? "pos" : "neg"}">${signedPct(gain)}</span><div class="sub muted small">${v.mae_ml.toFixed(2)} vs ${v.mae_season_avg.toFixed(2)} · side ${pct(v.side_accuracy, 0)}</div></td>`;
  }).join("")}</tr>`).join("");
  const abl = Object.entries(r.ablation).map(([name, res]) => `<tr><td>${esc(name)}</td>${r.stats.map((k) =>
    `<td class="num ${res[k].improvement > 0.0005 ? "pos" : res[k].improvement < -0.0005 ? "neg" : ""}">${signedPct(res[k].improvement, 2)}</td>`).join("")}</tr>`).join("");
  $("#yearly-body").innerHTML = `
    <table class="table"><thead><tr><th>Season</th>${head}</tr></thead><tbody>${rows}</tbody></table>
    <p class="legend-note" style="margin-top:8px">Each cell: how much smaller the model's average miss is than simply using the season-to-date average (higher is better),
      the two average misses, and how often the model picked the right side of a line set at that average.</p>
    <h4 class="muted small" style="margin:16px 0 6px">What each data source adds (${r.ablation_season}, accuracy gain from including it)</h4>
    <table class="table"><thead><tr><th>Data</th>${head}</tr></thead><tbody>${abl}</tbody></table>`;
}

async function loadLines() {
  const d = await api("lines");
  const last = d.last ? `${new Date(d.last.snapshot).toLocaleString()}: ${d.last.notes.join("; ")}` : "none yet";
  $("#lines-body").innerHTML = `<dl class="kv">
    <dt>Lines collected</dt><dd>${(d.collected || 0).toLocaleString()} across ${d.days || 0} game days</dd>
    <dt>Closing lines</dt><dd>${(d.closing_lines || 0).toLocaleString()}</dd>
    <dt>Graded</dt><dd>${(d.graded || 0).toLocaleString()} (settled against box scores; saved to data/lines/graded.csv)</dd>
    <dt>Last snapshot</dt><dd>${esc(last)}</dd>
  </dl>`;
}

/* ------------------------------------------------------------------ events */

function bindUI() {
  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) showView(b.dataset.view); });
  ["#prop-search", "#prop-stat", "#prop-decision"].forEach((s) => $(s).addEventListener("input", renderProps));
  ["#proj-search", "#proj-team", "#proj-moved"].forEach((s) => $(s).addEventListener("input", renderProjections));

  $("#props-table tbody").addEventListener("click", async (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      for (const id of del.dataset.del.split(",")) await api(`props/${id}`, { method: "DELETE" });
      await loadProps();
      return;
    }
    const row = e.target.closest("tr.row");
    if (!row) return;
    const id = row.dataset.id;
    state.open.has(id) ? state.open.delete(id) : state.open.add(id);
    renderProps();
  });

  $("#toggle-settings").addEventListener("click", () => ($("#settings").hidden = !$("#settings").hidden));
  let settingsTimer;
  $("#settings").addEventListener("input", () => {
    const n = (s, d) => (Number.isFinite(parseFloat($(s).value)) ? parseFloat($(s).value) : d);
    state.settings = {
      ev_threshold: n("#set-ev", 3) / 100,
      market_blend: Math.min(1, Math.max(0, n("#set-blend", 35) / 100)),
      bankroll: n("#set-bankroll", 1000),
      kelly_mult: Math.min(1, Math.max(0, n("#set-kelly", 0.25))),
      bias_adjust: $("#set-bias").checked,
      books: (() => {
        const boxes = $$("#book-filter input");
        const on = boxes.filter((b) => b.checked).map((b) => b.value);
        return on.length === boxes.length ? [] : on;
      })(),
    };
    saveSettings();
    clearTimeout(settingsTimer);
    settingsTimer = setTimeout(loadProps, 300);
  });

  $("#clear-examples").addEventListener("click", async () => {
    await api("props-clear?source=example", { method: "POST" });
    loadProps();
  });

  $("#import-odds").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "Importing…";
    try {
      const r = await api("odds-import", { method: "POST" });
      await loadProps();
      alert(r.imported ? `Imported ${r.imported} lines.\n${r.notes.join("\n")}`
        : `No player-prop lines are posted yet.\n${r.notes.join("\n")}\n\nBooks usually post props a day or two before games.`);
    } catch (err) { alert(err.message); }
    btn.disabled = false; btn.textContent = "Import lines";
  });

  $("#open-add").addEventListener("click", () => { $("#add-error").textContent = ""; $("#add-dialog").showModal(); });
  $("#add-cancel").addEventListener("click", () => $("#add-dialog").close());
  $("#add-form").addEventListener("input", schedulePreview);
  $("#add-form").addEventListener("submit", submitProp);

  $("#proj-table thead").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-k]");
    if (!th) return;
    const k = th.dataset.k;
    state.projSort = { k, asc: state.projSort.k === k ? !state.projSort.asc : ["name", "team"].includes(k) };
    renderProjections();
  });
  $("#proj-table tbody").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-pid]"); if (tr) openPlayer(tr.dataset.pid); });
  $("#player-close").addEventListener("click", () => $("#player-dialog").close());

  $("#refresh-data").addEventListener("click", async () => {
    await api("refresh", { method: "POST" });
    location.reload();
  });
  $("#run-yearly").addEventListener("click", async () => { await api("yearly", { method: "POST" }); loadYearly(); });
  $("#snapshot-lines").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try { await api("odds-import", { method: "POST" }); } catch (err) { alert(err.message); }
    e.currentTarget.disabled = false;
    loadLines(); loadProps();
  });
  $("#train-model").addEventListener("click", async () => { await api("train", { method: "POST" }); loadModelReport(); });
  $("#run-backtest").addEventListener("click", async () => { await api("backtest", { method: "POST" }); loadBacktest(); });
}

/* ------------------------------------------------------------------ NFL */

const NFL_MARKETS = {
  PASS_YDS: "Pass yds", PASS_CMP: "Completions", PASS_ATT: "Pass att", PASS_TD: "Pass TD", INT: "Interceptions",
  RUSH_YDS: "Rush yds", RUSH_ATT: "Rush att", REC: "Receptions", REC_YDS: "Rec yds", RUSH_REC_YDS: "Rush + rec yds",
  ANYTIME_TD: "Anytime TD",
};
const kickoff = (s) => {
  const d = new Date(s);
  return isNaN(d) ? "" : d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
};

function nflQuery() {
  const s = state.nfl.settings;
  return new URLSearchParams(Object.entries(s).map(([k, v]) => [k, Array.isArray(v) ? v.join(",") : String(v)])).toString();
}

async function loadNfl() {
  try {
    const r = await api(`nfl-board?${nflQuery()}`);
    Object.assign(state.nfl, { props: r.props, books: r.books, origin: r.origin, scored_at: r.scored_at,
                               status: r.status, error: r.error, database: r.database, loaded: true });
  } catch (err) {
    Object.assign(state.nfl, { props: [], error: err.message, loaded: true });
  }
  renderNflBookFilter();
  renderNfl();
  if (state.view === "projections") renderNflProjections();
  if (state.nfl.status === "running") setTimeout(loadNfl, 4000);
}

function renderNflBookFilter() {
  const box = $("#nfl-book-filter");
  if (!state.nfl.books.length) { box.innerHTML = `<span class="muted small">Books appear once NFL lines are scored.</span>`; return; }
  const mine = new Set(state.nfl.settings.books.map((b) => b.toLowerCase()));
  box.innerHTML = state.nfl.books.map((b) => `<label class="check"><input type="checkbox" value="${esc(b)}" ${!mine.size || mine.has(b.toLowerCase()) ? "checked" : ""}> ${esc(b)}</label>`).join("");
}

function nflFiltered() {
  const q = $("#nfl-search").value.trim().toLowerCase();
  const m = $("#nfl-market").value, dec = $("#nfl-decision").value;
  return state.nfl.props.filter((g) => {
    const d = g.best.decision;
    if (q && !g.player.toLowerCase().includes(q)) return false;
    if (m && g.market !== m) return false;
    if (dec === "bets" && d === "PASS") return false;
    if (dec && dec !== "bets" && d !== dec) return false;
    return true;
  });
}

function renderNfl() {
  const n = state.nfl, props = n.props;
  const bets = props.filter((g) => g.best.decision !== "PASS");
  const avg = bets.length ? bets.reduce((s, g) => s + g.best.best_ev, 0) / bets.length : null;
  const when = n.scored_at ? kickoff(n.scored_at) : "–";
  $("#nfl-tiles").innerHTML = [
    ["Props analyzed", props.length], ["Model picks (paper)", bets.length],
    ["Avg model EV of picks", avg == null ? "–" : signedPct(avg)],
    [n.origin === "local" ? "Scored on this PC" : "Scored in the cloud", when],
  ].map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  const msgs = [];
  if (n.status === "running") msgs.push("Scoring the open NFL lines on this PC… (the first run downloads NFL history, about a minute)");
  if (n.error) msgs.push(`NFL scoring failed: ${esc(n.error)}`);
  if (n.database === false) msgs.push("No database configured: NFL lines are collected in Supabase, so set DATABASE_URL in .env.");
  const note = $("#nfl-source-note");
  note.innerHTML = `<span>${msgs.join("<br>")}</span>`;
  note.hidden = !msgs.length;

  const empty = $("#nfl-empty");
  empty.hidden = props.length > 0;
  empty.textContent = n.origin ? "No open NFL lines right now. Books post most props 1–5 days before kickoff."
    : "No NFL scores yet. Click Score now to score the open lines on this PC.";
  $("#nfl-table tbody").innerHTML = nflFiltered().map((g) => {
    const b = g.best, open = n.open.has(g.id);
    const flags = [g.track_only && !g.outside_training ? `<span class="flag">tracked only</span>` : "",
      g.outside_training ? `<span class="flag">outside model</span>` : "",
      g.injury_status ? `<span class="flag warn">${esc(g.injury_status)}</span>` : "",
      g.at_my_books === false ? `<span class="flag">not at your books</span>` : "",
      g.books.length > 1 ? `<span class="tag">${g.books.length} books</span>` : ""].join("");
    const price = `${odds(b.over_odds)} / ${b.under_odds == null ? "–" : odds(b.under_odds)}`;
    const proj = g.kind === "binary" ? pct(b.p_over, 0) : g.projection.toFixed(1);
    let diff = "";
    if (g.kind !== "binary") {
      const d = g.projection - b.line;
      diff = ` <span class="${d >= 0 ? "pos" : "neg"} small">${d >= 0 ? "+" : ""}${d.toFixed(1)}</span>`;
    }
    const decision = b.decision === "PASS" ? `<span class="badge PASS">PASS</span>`
      : `<span class="badge ${b.decision}">${b.decision} <span class="tier">· ${b.tier}</span></span>`;
    let html = `<tr class="row${open ? " open" : ""}" data-id="${esc(g.id)}">
      <td class="player-cell"><b>${esc(g.player)}</b>${flags}<div class="sub">${esc(g.team)} · ${g.home ? "vs" : "@"} ${esc(g.opp)} · ${kickoff(g.start_time)}</div></td>
      <td>${esc(NFL_MARKETS[g.market] || g.market)} <span class="muted">${b.line}</span><div class="sub muted small">${esc(g.position)}</div></td>
      <td class="num">${price}<div class="sub muted small">${esc(b.book)}</div></td>
      <td class="num"><b>${proj}</b>${diff}</td>
      <td class="num">${pct(b.p_over)}</td>
      <td class="num ${b.best_ev >= 0 ? "pos" : "neg"}">${signedPct(b.best_ev)} <span class="muted small">${b.best_side[0]}</span></td>
      <td>${decision}</td><td class="num"><span class="chev">›</span></td></tr>`;
    if (open) html += `<tr class="detail"><td colspan="8">${nflDetail(g)}</td></tr>`;
    return html;
  }).join("");
}

function nflDetail(g) {
  const b = g.best, s = state.nfl.settings;
  const td = g.market === "ANYTIME_TD";
  const reasons = g.reasoning.map((t) => `<li><span class="lean neutral">•</span><span>${esc(t)}</span></li>`).join("");
  const evRows = ["OVER", "UNDER"].map((side) => {
    const x = b.sides[side];
    if (!x) return `<tr class="dim"><td>${td ? "No" : "Under"}</td><td colspan="5" class="muted">No price posted</td></tr>`;
    const label = td ? (side === "OVER" ? "Yes" : "No") : `${side === "OVER" ? "Over" : "Under"} ${b.line}`;
    return `<tr class="${side === b.decision ? "best" : ""}"><td>${label} (${odds(x.odds)})</td>
      <td>${pct(x.model_prob)}</td><td>${pct(x.market_prob)}</td><td>${pct(x.breakeven)}</td>
      <td class="${x.ev >= 0 ? "pos" : "neg"}">${signedPct(x.ev)}</td><td>${x.stake ? money(x.stake) : "–"}</td></tr>`;
  }).join("");
  let decisionLine;
  if (b.decision !== "PASS") decisionLine = `${b.tier} ${b.decision}: EV ${signedPct(b.best_ev)} per $1 at ${b.book}.`;
  else if (g.at_my_books === false) decisionLine = `Not offered at your books (best elsewhere: ${b.book}). Tick more books in Settings to consider it.`;
  else if (g.outside_training) decisionLine = "Not recommended: this player's role is outside what the model was trained on.";
  else if (g.track_only) decisionLine = "Not a recommended bet: tracked for paper trading only.";
  else decisionLine = `No bet: best EV ${signedPct(b.best_ev)} is below the ${pct(b.threshold)} bar.`;
  const shop = g.books.length < 2 ? "" : `<div class="panel"><h4>Line shopping</h4>
    <table class="ev-table"><thead><tr><th>Book</th><th>Line</th><th>Over</th><th>Under</th><th>Over EV</th><th>Under EV</th></tr></thead><tbody>
    ${g.books.map((x) => `<tr class="${x.best ? "best" : ""}${x.mine ? "" : " dim"}"><td>${esc(x.book)}${x.best ? " ★" : ""}</td><td>${x.line}</td>
      <td>${odds(x.over_odds)}</td><td>${x.under_odds == null ? "–" : odds(x.under_odds)}</td>
      <td class="${x.ev_over >= 0 ? "pos" : "neg"}">${signedPct(x.ev_over)}</td>
      <td>${x.ev_under == null ? "–" : `<span class="${x.ev_under >= 0 ? "pos" : "neg"}">${signedPct(x.ev_under)}</span>`}</td></tr>`).join("")}
    </tbody></table><p class="legend-note">★ best price among your books. Greyed books are excluded in Settings.</p></div>`;
  return `<div class="detail-grid">
    <div><p class="summary">${esc(decisionLine)}</p><ul class="reasons">${reasons}</ul></div>
    <div>
      <div class="panel"><h4>Expected value</h4>
        <table class="ev-table"><thead><tr><th>Side</th><th>Model</th><th>Market</th><th>Break-even</th><th>EV</th><th>Stake</th></tr></thead>
        <tbody>${evRows}</tbody></table>
        ${g.low_confidence ? `<p class="legend-note">Low-confidence spot: EV bar raised to ${pct(b.threshold)}.</p>` : ""}
        ${b.p_push > 0.001 ? `<p class="legend-note">Push probability ${pct(b.p_push)} (whole-number line).</p>` : ""}
        <p class="legend-note">EV uses ${Math.round((1 - s.market_blend) * 100)}% model and ${Math.round(s.market_blend * 100)}% market probability. Stake is ${s.kelly_mult}× Kelly on a ${money(s.bankroll)} bankroll.</p>
      </div>
      ${shop}
    </div></div>`;
}

function renderNflProjections() {
  const by = {};
  for (const g of state.nfl.props) {
    const k = `${g.player}|${g.team}`;
    const p = (by[k] ||= { player: g.player, team: g.team, opp: g.opp, home: g.home, start: g.start_time, m: {} });
    p.m[g.market] = g.kind === "binary" ? g.best.p_over : g.projection;
  }
  const teams = [...new Set(Object.values(by).map((p) => p.team))].sort();
  const sel = $("#nfl-proj-team"), cur = sel.value;
  sel.innerHTML = `<option value="">All teams</option>` + teams.map((t) => `<option ${t === cur ? "selected" : ""}>${esc(t)}</option>`).join("");
  const q = $("#nfl-proj-search").value.trim().toLowerCase();
  const rows = Object.values(by).filter((p) => (!q || p.player.toLowerCase().includes(q)) && (!sel.value || p.team === sel.value))
    .sort((a, b) => a.start.localeCompare(b.start) || a.team.localeCompare(b.team) || a.player.localeCompare(b.player));
  const dash = `<span class="muted">–</span>`;
  const num = (v) => (v == null ? dash : v.toFixed(1));
  $("#nfl-proj-table tbody").innerHTML = rows.length ? rows.map((p) => `<tr>
    <td><b>${esc(p.player)}</b></td><td>${esc(p.team)}</td><td>${p.home ? "vs" : "@"} ${esc(p.opp)} · ${kickoff(p.start)}</td>
    <td class="num">${num(p.m.PASS_YDS)}</td><td class="num">${num(p.m.RUSH_YDS)}</td><td class="num">${num(p.m.REC)}</td>
    <td class="num">${num(p.m.REC_YDS)}</td><td class="num">${p.m.ANYTIME_TD == null ? dash : pct(p.m.ANYTIME_TD, 0)}</td></tr>`).join("")
    : `<tr><td colspan="8" class="muted">No projections yet: they appear once open NFL lines are scored.</td></tr>`;
}

async function loadNflModel() {
  let m;
  try { m = await api("nfl-model"); } catch (err) { $("#nfl-model-status").innerHTML = `<p class="error">${esc(err.message)}</p>`; return; }
  if (!m.trained) {
    $("#nfl-model-status").innerHTML = `<p class="muted">Not trained yet. Run <code>python train_nfl.py</code> (about 2 minutes).</p>`;
    $("#nfl-scorecard").innerHTML = "";
  } else {
    $("#nfl-model-status").innerHTML = `<dl class="kv">
      <dt>Trained</dt><dd>${esc(new Date(m.trained_at).toLocaleString())}</dd>
      <dt>Data through</dt><dd>${esc(m.data_through)}</dd>
      <dt>Protocol</dt><dd>${esc(m.protocol)}</dd>
      <dt>Data sources</dt><dd>${Object.entries(m.provenance).map(([k, v]) => `<b>${esc(k)}</b>: ${esc(v)}`).join("<br>")}</dd></dl>
      <p class="muted small">Retrain with <code>python train_nfl.py</code> (about 2 minutes), then commit models/nfl/ so the cloud uses it.</p>`;
    const rows = Object.entries(m.markets).map(([k, v]) => {
      const h = v.holdout, bin = v.kind === "binary";
      const [model, base] = bin ? [h.logloss, h.logloss_baseline] : [h.mae, h.mae_baseline];
      const gain = base ? (base - model) / base : null;
      return `<tr><td>${esc(NFL_MARKETS[k] || k)}</td><td class="num">${h.n.toLocaleString()}</td>
        <td class="num">${model.toFixed(bin ? 3 : 2)}</td><td class="num">${base.toFixed(bin ? 3 : 2)}</td>
        <td class="num ${gain >= 0 ? "pos" : "neg"}">${signedPct(gain, 0)}</td>
        <td class="small muted">${v.top_features.slice(0, 4).map(([f]) => esc(f)).join(", ")}</td></tr>`;
    }).join("");
    $("#nfl-scorecard").innerHTML = `<table class="table"><thead><tr><th>Market</th><th class="num">2025 games</th><th class="num">Model</th>
      <th class="num">Baseline</th><th class="num">Better by</th><th>Leans most on</th></tr></thead><tbody>${rows}</tbody></table>
      <p class="legend-note">Yards and counts: average miss. Anytime TD: log loss (lower is better).</p>`;
  }
  const p = m.paper;
  if (!p) { $("#nfl-paper").innerHTML = `<p class="muted">${esc(m.paper_error || "Needs DATABASE_URL to read the paper-trading log.")}</p>`; return; }
  const cpName = { open: "First seen", t24: "24h before", t2: "2h before", close: "Closing (last before kickoff)" };
  const recRow = (label, r) => `<tr><td>${label}</td><td class="num">${r.picks}</td><td class="num">${r.wins}-${r.losses}-${r.pushes}</td>
    <td class="num ${r.units >= 0 ? "pos" : "neg"}">${r.units >= 0 ? "+" : ""}${r.units.toFixed(2)}u</td>
    <td class="num">${r.roi == null ? "–" : signedPct(r.roi)}</td>
    <td class="num">${r.avg_clv == null ? "–" : `${signedPct(r.avg_clv)} <span class="muted small">(${r.clv_n})</span>`}</td></tr>`;
  const graded = Object.values(p.checkpoints).some((r) => r.settled) || (p.track_only && p.track_only.settled);
  const cal = p.calibration.length ? `<h4>Calibration (closing checkpoint, every scored line)</h4>
    <table class="ev-table"><thead><tr><th>Model P(over)</th><th>Actual</th><th>Market</th><th>Lines</th></tr></thead><tbody>
    ${p.calibration.map((c) => `<tr><td>${pct(c.predicted, 0)}</td><td>${pct(c.actual, 0)}</td><td>${pct(c.market, 0)}</td><td>${c.n}</td></tr>`).join("")}
    </tbody></table>` : "";
  $("#nfl-paper").innerHTML = (graded ? "" : `<p class="muted">No graded picks yet: picks are settled the morning after each game.</p>`) +
    `<table class="table"><thead><tr><th>Checkpoint</th><th class="num">Picks</th><th class="num">W-L-P</th><th class="num">Units (1u flat)</th>
     <th class="num">ROI</th><th class="num">Avg CLV</th></tr></thead><tbody>
     ${Object.entries(p.checkpoints).map(([k, r]) => recRow(cpName[k], r)).join("")}
     ${p.track_only ? recRow("Tracked only (Anytime TD, outside model): would-be picks", p.track_only) : ""}
     </tbody></table>${cal}`;
}

function nflBindUI() {
  $("#sport-switch").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setSport(b.dataset.sport); });
  $("#nfl-market").insertAdjacentHTML("beforeend", Object.entries(NFL_MARKETS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join(""));
  ["#nfl-search", "#nfl-market", "#nfl-decision"].forEach((sel) => $(sel).addEventListener("input", renderNfl));
  ["#nfl-proj-search", "#nfl-proj-team"].forEach((sel) => $(sel).addEventListener("input", renderNflProjections));
  $("#nfl-table tbody").addEventListener("click", (e) => {
    const row = e.target.closest("tr.row");
    if (!row) return;
    const id = row.dataset.id, o = state.nfl.open;
    o.has(id) ? o.delete(id) : o.add(id);
    renderNfl();
  });
  $("#nfl-toggle-settings").addEventListener("click", () => ($("#nfl-settings").hidden = !$("#nfl-settings").hidden));
  const s = state.nfl.settings;
  $("#nfl-set-ev").value = +(s.ev_threshold * 100).toFixed(2);
  $("#nfl-set-blend").value = Math.round(s.market_blend * 100);
  $("#nfl-set-bankroll").value = s.bankroll;
  $("#nfl-set-kelly").value = s.kelly_mult;
  let timer;
  $("#nfl-settings").addEventListener("input", () => {
    const n = (sel, d) => (Number.isFinite(parseFloat($(sel).value)) ? parseFloat($(sel).value) : d);
    const boxes = $$("#nfl-book-filter input"), on = boxes.filter((b) => b.checked).map((b) => b.value);
    state.nfl.settings = {
      ev_threshold: n("#nfl-set-ev", 5) / 100,
      market_blend: Math.min(1, Math.max(0, n("#nfl-set-blend", 60) / 100)),
      bankroll: n("#nfl-set-bankroll", 1000),
      kelly_mult: Math.min(1, Math.max(0, n("#nfl-set-kelly", 0.25))),
      books: on.length === boxes.length ? [] : on,
    };
    saveNflSettings();
    clearTimeout(timer);
    timer = setTimeout(loadNfl, 300);
  });
  $("#nfl-score").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      await api("nfl-score", { method: "POST" });
      state.nfl.status = "running";
      renderNfl();
      setTimeout(loadNfl, 3000);
    } catch (err) { alert(err.message); }
    btn.disabled = false;
  });
}

boot();
