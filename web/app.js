"use strict";

let DATA = null;
let highlightTerm = "";
let kingExpanded = false;
const KING_CAP = 20;

function matchesHighlight(name) {
  return highlightTerm && name &&
    name.toLowerCase().includes(highlightTerm);
}

// ---- featured athlete nav ---------------------------------------------------
function renderFeatured() {
  const list = DATA.featured_athletes || [];
  if (!list.length) { $("#featured").innerHTML = ""; return; }
  const links = list.map((a) => {
    const standing = (DATA.king || []).find((k) => k.athlete_id === a.id);
    const pts = standing ? ` · ${standing.points} pts` : "";
    const name = a.name || `Athlete ${a.id}`;
    return `<a class="athlete-chip" href="athlete.html?id=${a.id}">
      ${avatar(a.avatar_url)}<span>${esc(name)}${pts}</span></a>`;
  }).join("");
  $("#featured").innerHTML = `<h2>Athlete pages</h2><div class="chips">${links}</div>`;
}

// ---- King standings ---------------------------------------------------------
function renderKing() {
  const all = DATA.king || [];
  // Show everyone while searching (so a highlighted athlete is never hidden).
  const showAll = kingExpanded || !!highlightTerm;
  const shown = showAll ? all : all.slice(0, KING_CAP);
  const rows = shown.map((a) => `
    <tr class="king-${a.overall_rank} ${matchesHighlight(a.name) ? "hl" : ""}">
      <td><span class="rankbadge">${a.overall_rank}</span></td>
      <td>${esc(a.name)}</td>
      <td class="num pts">${a.points}</td>
      <td class="num">${a.segments_won}</td>
      <td class="num">${a.segments_scored}</td>
    </tr>`).join("");

  let toggle = "";
  if (all.length > KING_CAP && !highlightTerm) {
    toggle = kingExpanded
      ? `<button class="expand-btn" id="king-toggle">Show top ${KING_CAP} ▲</button>`
      : `<button class="expand-btn" id="king-toggle">Show all ${all.length} athletes ▼</button>`;
  }

  $("#king").innerHTML = `
    <div class="king-card"><table>
      <thead><tr><th>#</th><th>Athlete</th><th class="num">Points</th>
        <th class="num">Segments won</th><th class="num">Segments scored</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="5" class="muted">No scored athletes yet.</td></tr>`}</tbody>
    </table></div>${toggle}`;

  const btn = $("#king-toggle");
  if (btn) btn.addEventListener("click", () => { kingExpanded = !kingExpanded; renderKing(); });
}

// ---- Filtered-out segments --------------------------------------------------
function renderFiltered() {
  const list = DATA.filtered || [];
  if (!list.length) { $("#filtered-section").innerHTML = ""; return; }
  const rows = list.map((f) => `
    <tr><td><strong>${esc(f.name || "(unfetched)")}</strong></td>
      <td class="muted">${esc(f.location || "")}</td>
      <td><span class="pill out">${esc(f.reason)}</span></td>
      <td><a class="muted" href="https://www.strava.com/segments/${f.id}" target="_blank" rel="noopener">${f.id} ↗</a></td></tr>`).join("");
  $("#filtered-section").innerHTML = `
    <h2>Filtered out <span class="muted">(${list.length})</span></h2>
    <p class="hint">Tracked but excluded from the standings — not a ride, or doesn't start/finish in Shutesbury. Still kept in the database.</p>
    <div class="seg-card"><table>
      <thead><tr><th>Segment</th><th>Strava location</th><th>Reason</th><th>ID</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
}

// ---- Segments table ---------------------------------------------------------
const SEG_COLS = [
  { key: "name", label: "Segment", type: "str" },
  { key: "terrain", label: "Terrain", type: "str" },
  { key: "distance_m", label: "Dist (mi)", type: "num", fmt: fmtMiles },
  { key: "avg_grade", label: "Grade", type: "num", fmt: fmtGrade },
  { key: "gross_gain", label: "Gain (ft)", type: "num", fmt: fmtFeet },
  { key: "total_efforts", label: "Efforts", type: "num", fmt: (v) => (v ?? 0).toLocaleString() },
  { key: "difficulty", label: "Difficulty", type: "num", fmt: (v) => `<span class="diff">${v}</span>` },
  { key: "_leader", label: "Leader", type: "str" },
];
let sortKey = "difficulty", sortAsc = false;

function segValue(s, key) {
  if (key === "_leader") return s.leader ? s.leader.name : "";
  return s[key];
}
function renderSegments() {
  const segs = [...DATA.segments].sort((a, b) => {
    const col = SEG_COLS.find((c) => c.key === sortKey);
    let va = segValue(a, sortKey), vb = segValue(b, sortKey);
    if (col.type === "num") { va = va ?? -Infinity; vb = vb ?? -Infinity; return sortAsc ? va - vb : vb - va; }
    va = String(va ?? "").toLowerCase(); vb = String(vb ?? "").toLowerCase();
    return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  });

  const head = SEG_COLS.map((c) => {
    const cls = [c.type === "num" ? "num" : "", c.key === sortKey ? "sorted" + (sortAsc ? " asc" : "") : ""].join(" ").trim();
    return `<th class="${cls}" data-key="${c.key}">${c.label}</th>`;
  }).join("");

  const body = segs.map((s) => {
    const leaderHl = s.leader && matchesHighlight(s.leader.name);
    const cells = SEG_COLS.map((c) => {
      if (c.key === "name")
        return `<td><strong>${esc(s.name)}</strong><br><span class="muted">${esc(s.location || "")}</span></td>`;
      if (c.key === "terrain")
        return `<td><span class="pill ${s.terrain}">${s.terrain}</span></td>`;
      if (c.key === "_leader")
        return `<td>${s.leader ? esc(s.leader.name) + " · " + fmtTime(s.leader.elapsed_time) : "—"}</td>`;
      const raw = s[c.key];
      return `<td class="num">${c.fmt ? c.fmt(raw) : (raw ?? "—")}</td>`;
    }).join("");
    return `<tr data-id="${s.id}" class="${leaderHl ? "hl" : ""}">${cells}</tr>`;
  }).join("");

  $("#segments").innerHTML = `
    <div class="seg-card"><table class="clickable">
      <thead><tr>${head}</tr></thead><tbody>${body}</tbody>
    </table></div>`;
  $("#seg-count").textContent = `(${segs.length})`;

  $("#segments").querySelectorAll("thead th").forEach((th) =>
    th.addEventListener("click", () => {
      const k = th.dataset.key;
      if (k === sortKey) sortAsc = !sortAsc;
      else { sortKey = k; sortAsc = false; }
      renderSegments();
    }));
  $("#segments").querySelectorAll("tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => openDetail(+tr.dataset.id)));
}

// ---- Segment detail ---------------------------------------------------------
function elevationSVG(profile) {
  const ev = profile.elevation || [], di = profile.distance || [];
  if (ev.length < 2) return "";
  const W = 700, H = 130, pad = 6;
  const lo = Math.min(...ev), hi = Math.max(...ev), span = hi - lo || 1;
  // distance is cumulative and may not start at 0, so normalize to the segment start
  const d0 = di[0], dspan = (di[di.length - 1] - d0) || 1;
  const pts = ev.map((e, i) => {
    const x = pad + ((di[i] - d0) / dspan) * (W - 2 * pad);
    const y = pad + (1 - (e - lo) / span) * (H - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const area = `${pad},${H - pad} ${pts.join(" ")} ${W - pad},${H - pad}`;
  return `<svg class="profile" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polygon points="${area}" fill="rgba(252,82,0,0.18)"></polygon>
      <polyline points="${pts.join(" ")}" fill="none" stroke="#fc5200" stroke-width="2"></polyline>
    </svg>`;
}

function openDetail(id) {
  const s = DATA.segments.find((x) => x.id === id);
  if (!s) return;
  const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  const effortRows = s.efforts.map((e) => `
    <tr class="${matchesHighlight(e.name) ? "hl" : ""}">
      <td class="num">${e.rank ?? "—"}</td>
      <td><div class="name-cell">${avatar(e.avatar_url, e.name)}${esc(e.name)}</div></td>
      <td class="num">${effortLink(e, fmtTime(e.elapsed_time))}</td>
      <td class="num">${e.avg_watts ? Math.round(e.avg_watts) + " W" : "—"}</td>
      <td class="num pts">${e.points || ""}</td>
    </tr>`).join("");

  if (location.hash !== `#segment/${id}`) location.hash = `segment/${id}`;
  $("#detail-body").innerHTML = `
    <div class="detail-head">
      <h3>${esc(s.name)}</h3>
      <span class="pill ${s.terrain}">${s.terrain}</span>
      <span class="muted">${esc(s.location || "")}</span>
      <a href="https://www.strava.com/segments/${s.id}" target="_blank" rel="noopener" class="muted">view on Strava ↗</a>
    </div>
    <div class="stat-grid">
      ${stat("Distance", fmtMiles(s.distance_m) + " mi")}
      ${stat("Avg grade", fmtGrade(s.avg_grade))}
      ${stat("Climb", fmtFeet(s.gross_gain) + " ft")}
      ${stat("Descent", fmtFeet(s.gross_loss) + " ft")}
      ${stat("Efforts", (s.total_efforts ?? 0).toLocaleString())}
      ${stat("Athletes", (s.total_athletes ?? 0).toLocaleString())}
      ${stat("Difficulty", s.difficulty)}
    </div>
    ${elevationSVG(s.profile)}
    ${s.map_image_url ? `<img class="seg-map" src="${esc(s.map_image_url)}" alt="map of ${esc(s.name)}">` : ""}
    <h4>Leaderboard <span class="muted">(top ${s.efforts.length}; points by rank)</span></h4>
    <table><thead><tr><th class="num">#</th><th>Athlete</th><th class="num">Time</th>
      <th class="num">Power</th><th class="num">Points</th></tr></thead>
      <tbody>${effortRows}</tbody></table>`;
  $("#detail-overlay").classList.remove("hidden");
}
function closeDetail() {
  $("#detail-overlay").classList.add("hidden");
  if (location.hash.startsWith("#segment/"))
    history.replaceState(null, "", location.pathname + location.search);
}
function openFromHash() {
  const m = location.hash.match(/^#segment\/(\d+)$/);
  if (m) openDetail(+m[1]); else closeDetail();
}

// ---- wiring -----------------------------------------------------------------
function applyHighlight() { renderKing(); renderSegments(); }

function init() {
  $("#generated").textContent =
    "updated " + new Date(DATA.generated_at).toLocaleString();
  const preset = new URLSearchParams(location.search).get("athlete");
  if (preset) { highlightTerm = preset.trim().toLowerCase(); $("#highlight").value = preset; }
  renderFeatured();
  renderKing();
  renderSegments();
  renderFiltered();
  $("#highlight").addEventListener("input", (e) => {
    highlightTerm = e.target.value.trim().toLowerCase();
    applyHighlight();
  });
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
    $("#king").innerHTML = `<div class="err">Couldn't load <code>data.json</code> (${err}).<br>
      If you opened this file directly, serve it instead:<br>
      <code>cd web &amp;&amp; python3 -m http.server</code> then open
      <code>http://localhost:8000</code>.</div>`;
  });
