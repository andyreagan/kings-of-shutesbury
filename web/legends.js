"use strict";

// Legends board — riders ranked by how many of the tracked segments they've
// completed. Reads data-legends.json: a lightweight segment list plus, for every
// athlete (both boards), the set of segment ids they've completed. The discipline
// filter narrows both the numerator (a rider's done segments) and the denominator
// (the segments in that discipline), all computed client-side.

let DATA = null;
let disciplineFilter = "all";
let expanded = false;
const SHOW_CAP = 25;     // rows shown collapsed
const SHOW_MAX = 200;    // hard cap when expanded

// ---- filter helpers ----------------------------------------------------------
// The set of segment ids in the current discipline filter, and a quick lookup of
// segment metadata by id (for the rider popup).
let segById = new Map();

function filterSegmentIds() {
  const ids = new Set();
  for (const s of DATA.segments) {
    if (disciplineFilter === "all" || s.discipline === disciplineFilter) ids.add(s.id);
  }
  return ids;
}

// ---- standings ---------------------------------------------------------------
// Rank by completion (done count == % for a fixed denominator), ties broken only
// by name for a stable display — Strava gives no per-rider attempt count to break
// real ties on. Tied riders share a rank, competition-style (1, 1, 3).
function computeBoard() {
  const filterIds = filterSegmentIds();
  const total = filterIds.size;
  const rows = [];
  for (const lg of DATA.legends) {
    let done = 0;
    for (const sid of lg.segs) if (filterIds.has(sid)) done++;
    if (done === 0) continue;
    rows.push({
      athlete_id: lg.athlete_id, name: lg.name || `Athlete ${lg.athlete_id}`,
      avatar_url: lg.avatar_url, badge: lg.badge, gender: lg.gender,
      done, pct: total ? (done / total) * 100 : 0,
    });
  }
  rows.sort((a, b) => b.done - a.done || a.name.localeCompare(b.name));
  let lastDone = null, lastRank = 0;
  rows.forEach((r, i) => {
    if (r.done !== lastDone) { lastRank = i + 1; lastDone = r.done; }
    r.rank = lastRank;
  });
  return { rows, total };
}

// ---- render ------------------------------------------------------------------
function pctBar(pct) {
  const w = Math.max(0, Math.min(100, pct)).toFixed(1);
  return `<div class="pct-bar"><span style="width:${w}%"></span></div>`;
}

function renderBoard() {
  const { rows, total } = computeBoard();
  const shown = rows.slice(0, expanded ? SHOW_MAX : SHOW_CAP);

  const body = shown.map((r) => `
    <tr class="king-${r.rank} clickable-row" data-id="${r.athlete_id}">
      <td><span class="rankbadge">${r.rank}</span></td>
      <td><div class="name-cell">${avatar(r.avatar_url)}<a href="#athlete/${r.athlete_id}">${esc(r.name)}</a>${r.gender === "F" ? ' <span class="muted">♀</span>' : ""}</div></td>
      <td class="num pts">${r.pct.toFixed(1)}%</td>
      <td class="pct-cell">${pctBar(r.pct)}</td>
      <td class="num">${r.done} / ${total}</td>
    </tr>`).join("");

  let toggle = "";
  if (rows.length > SHOW_CAP) {
    const expandTo = Math.min(SHOW_MAX, rows.length);
    toggle = expanded
      ? `<button class="expand-btn" id="legend-toggle">Show top ${SHOW_CAP} ▲</button>`
      : `<button class="expand-btn" id="legend-toggle">Show top ${expandTo} ▼</button>`;
  }

  $("#legends").innerHTML = `
    <div class="king-card"><table>
      <thead><tr><th>#</th><th>Rider</th><th class="num">% complete</th>
        <th></th><th class="num">Segments</th></tr></thead>
      <tbody>${body || `<tr><td colspan="5" class="muted">No riders yet.</td></tr>`}</tbody>
    </table></div>${toggle}`;

  $("#legend-count").textContent = `(${rows.length} riders · ${total} segments)`;

  const btn = $("#legend-toggle");
  if (btn) btn.addEventListener("click", () => { expanded = !expanded; renderBoard(); });
  $("#legends").querySelectorAll("tbody tr[data-id]").forEach((tr) =>
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;   // let the hash link handle it
      openAthlete(+tr.dataset.id);
    }));
}

// ---- coverage ----------------------------------------------------------------
// How trustworthy a segment's completion is: the share of the field we've
// captured, and when we last swept it. Flagged "weak" when shallow (<half the
// field) or stale (swept months ago / never) — those are the segments where a
// "to do" might just be an effort we haven't pulled yet.
function coverageCell(s) {
  const tot = s.total_athletes || 0;
  const pct = tot ? Math.round((s.captured / tot) * 100) : null;
  const ago = fmtAgo(s.efforts_fetched_at);
  const weak = (pct !== null && pct < 50) || ago === "never" || /mo ago|y ago/.test(ago);
  const label = pct === null ? `${s.captured} seen` : `${pct}%`;
  const title = `${(s.captured || 0).toLocaleString()} of ${tot.toLocaleString()} riders captured · swept ${ago}`;
  return `<td class="cov${weak ? " cov-weak" : ""}" title="${esc(title)}">${label} <span class="muted">· ${ago}</span></td>`;
}

// ---- rider popup -------------------------------------------------------------
function openAthlete(id) {
  const lg = DATA.legends.find((l) => l.athlete_id === id);
  if (!lg) { closeDetail(); return; }
  const filterIds = filterSegmentIds();
  const doneIds = new Set(lg.segs.filter((sid) => filterIds.has(sid)));

  const inFilter = DATA.segments.filter((s) => filterIds.has(s.id));
  const done = inFilter.filter((s) => doneIds.has(s.id));
  const todo = inFilter.filter((s) => !doneIds.has(s.id));
  done.sort((a, b) => b.difficulty - a.difficulty);
  todo.sort((a, b) => b.difficulty - a.difficulty);
  const total = inFilter.length;
  const pct = total ? (done.length / total) * 100 : 0;

  const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  const rowHtml = (s, did) => `
    <tr class="${did ? "" : "todo"}">
      <td><a href="index.html#segment/${s.id}"><strong>${esc(s.name)}</strong></a><br><span class="muted">${esc(s.location || "")}</span></td>
      <td><span class="pill ${s.terrain}">${s.terrain}</span></td>
      <td class="num diff">${s.difficulty}</td>
      <td class="num">${did ? "✓" : '<span class="muted">to do</span>'}</td>
      ${coverageCell(s)}
    </tr>`;

  if (location.hash !== `#athlete/${id}`) location.hash = `athlete/${id}`;
  $("#detail-body").innerHTML = `
    <div class="athlete-head">
      ${avatar(lg.avatar_url)}
      <div>
        <h3>${esc(lg.name || `Athlete ${id}`)}${lg.gender === "F" ? ' <span class="muted">♀</span>' : ""}</h3>
        <p class="sub">${pct.toFixed(1)}% of ${disciplineFilter === "all" ? "all" : disciplineFilter} segments completed</p>
      </div>
    </div>
    <div class="stat-grid">
      ${stat("Completed", `${done.length} / ${total}`)}
      ${stat("% complete", pct.toFixed(1) + "%")}
      ${stat("To do", todo.length)}
    </div>
    <h4>Segment-by-segment</h4>
    <p class="hint">Every tracked segment in the current filter. ✓ = completed; the rest are
      the to-do list, hardest first. Click a segment to see it on the map and leaderboard.
      <strong>Coverage</strong> is how much of a segment's field we've captured and when we last
      swept it — a shallow or stale segment may show a rider as "to do" only because their effort
      sits below the depth we've pulled.</p>
    <div class="seg-card"><table>
      <thead><tr><th>Segment</th><th>Terrain</th><th class="num">Difficulty</th>
        <th class="num">Done</th><th>Coverage</th></tr></thead>
      <tbody>${done.map((s) => rowHtml(s, true)).join("")}${todo.map((s) => rowHtml(s, false)).join("")}</tbody>
    </table></div>`;
  $("#detail-overlay").classList.remove("hidden");
}

function closeDetail() {
  $("#detail-overlay").classList.add("hidden");
  if (/^#athlete\//.test(location.hash))
    history.replaceState(null, "", location.pathname + location.search);
}
function openFromHash() {
  const m = location.hash.match(/^#athlete\/(\d+)$/);
  if (!m) { closeDetail(); return; }
  openAthlete(+m[1]);
}

// ---- wiring ------------------------------------------------------------------
function init() {
  segById = new Map(DATA.segments.map((s) => [s.id, s]));
  $("#generated").textContent =
    "updated " + new Date(DATA.generated_at).toLocaleString();

  const dsw = document.getElementById("discipline-switch");
  dsw.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-discipline]");
    if (!b) return;
    disciplineFilter = b.dataset.discipline;
    dsw.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    expanded = false;
    renderBoard();
    // A popup open from a different filter would show stale done/to-do — refresh it.
    if (!$("#detail-overlay").classList.contains("hidden")) openFromHash();
  });

  renderBoard();
  $("#detail-close").addEventListener("click", closeDetail);
  $("#detail-overlay").addEventListener("click", (e) => {
    if (e.target.id === "detail-overlay") closeDetail();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });
  window.addEventListener("hashchange", openFromHash);
  openFromHash();
}

loadData()
  .then((d) => { DATA = d; init(); })
  .catch((err) => {
    $("#legends").innerHTML = `<div class="err">Couldn't load <code>data-legends.json</code> (${err}).<br>
      If you opened this file directly, serve it instead:<br>
      <code>cd web &amp;&amp; python3 -m http.server</code> then open
      <code>http://localhost:8000/legends.html</code>.</div>`;
  });
