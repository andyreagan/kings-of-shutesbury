"use strict";

let DATA = null;
let kingExpanded = false;
const KING_CAP = 20;
const KING_MAX = 100;   // hard cap: only the top 100 are shown / get a popup
let disciplineFilter = "all";
let standings = [];

// ---- discipline filter helpers ----------------------------------------------
function segmentsForFilter() {
  return disciplineFilter === "all"
    ? DATA.segments
    : DATA.segments.filter((s) => s.discipline === disciplineFilter);
}

function computeStandings(segments) {
  const map = new Map();
  for (const seg of segments) {
    for (const e of seg.efforts || []) {
      if (!e.points || e.points <= 0) continue;
      let rec = map.get(e.athlete_id);
      if (!rec) {
        rec = { athlete_id: e.athlete_id, name: e.name, avatar_url: e.avatar_url,
                points: 0, segments_won: 0, segments_scored: 0 };
        map.set(e.athlete_id, rec);
      }
      rec.points += e.points;
      rec.segments_scored += 1;
      if (e.rank === 1) rec.segments_won += 1;
      rec.name = rec.name || e.name;
      rec.avatar_url = rec.avatar_url || e.avatar_url;
    }
  }
  const arr = Array.from(map.values());
  arr.sort((a, b) => b.points - a.points);
  arr.forEach((a, i) => {
    a.overall_rank = i + 1;
    a.points = Math.round(a.points * 10) / 10;
  });
  return arr;
}

function applyDisciplineFilter() {
  standings = computeStandings(segmentsForFilter());

  const unclassifiedCount = DATA.segments.filter((s) => !s.discipline).length;
  const noteEl = document.getElementById("unclassified-note");
  if (noteEl) {
    noteEl.textContent = (disciplineFilter !== "all" && unclassifiedCount > 0)
      ? `(${unclassifiedCount} unclassified)`
      : "";
  }

  const h2 = document.querySelector("#king-section h2");
  if (h2) {
    if (disciplineFilter === "all") {
      h2.textContent = "King of Shutesbury";
    } else {
      const cap = disciplineFilter.charAt(0).toUpperCase() + disciplineFilter.slice(1);
      h2.textContent = "King of " + cap;
    }
  }

  renderKing();
  renderSegments();

  if (window.setMapDiscipline) window.setMapDiscipline(disciplineFilter);
}

// ---- athlete helpers (ported from athlete.js) --------------------------------
function findEffort(seg, id) {
  return seg.efforts.find((e) => e.athlete_id === id);
}

function resolveAthlete(id) {
  const standing = standings.find((k) => k.athlete_id === id);
  let name = standing ? standing.name : null;
  let avatarUrl = null;
  for (const seg of DATA.segments) {
    const e = findEffort(seg, id);
    if (e) { name = name || e.name; avatarUrl = avatarUrl || e.avatar_url; }
    if (name && avatarUrl) break;
  }
  return { id, name: name || `Athlete ${id}`, avatarUrl, standing };
}

// ---- athlete popup ----------------------------------------------------------
function openAthlete(id) {
  const ath = resolveAthlete(id);
  const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  const filteredSegs = segmentsForFilter();

  const rows = filteredSegs.map((seg) => {
    const eff = findEffort(seg, id);
    const leader = seg.efforts[0];
    const gap = eff && leader ? eff.elapsed_time - leader.elapsed_time : null;
    return { seg, eff, gap };
  });
  const attempted = rows.filter((r) => r.eff);
  const remaining = rows.filter((r) => !r.eff);
  const koms = attempted.filter((r) => r.eff.rank === 1).length;
  const scored = attempted.filter((r) => (r.eff.points || 0) > 0).length;
  const earnedPts = attempted.reduce((s, r) => s + (r.eff.points || 0), 0);

  attempted.sort((a, b) => (b.eff.points || 0) - (a.eff.points || 0) ||
    a.eff.rank - b.eff.rank);
  remaining.sort((a, b) => b.seg.difficulty - a.seg.difficulty);

  const rowHtml = ({ seg, eff, gap }) => `
    <tr class="${eff ? "" : "todo"}">
      <td><a href="#segment/${seg.id}"><strong>${esc(seg.name)}</strong></a><br><span class="muted">${esc(seg.location || "")}</span></td>
      <td><span class="pill ${seg.terrain}">${seg.terrain}</span></td>
      <td class="num diff">${seg.difficulty}</td>
      <td class="num">${eff ? (eff.rank ? "#" + eff.rank : "—") : "—"}</td>
      <td class="num">${eff ? effortLink(eff, fmtTime(eff.elapsed_time)) : '<span class="muted">not attempted</span>'}</td>
      <td class="num">${eff && eff.rank !== 1 ? fmtGap(gap) : (eff ? '<span class="kom">KOM</span>' : "—")}</td>
      <td class="num pts">${eff && eff.points ? eff.points : ""}</td>
    </tr>`;

  if (location.hash !== `#athlete/${id}`) location.hash = `athlete/${id}`;
  $("#detail-body").innerHTML = `
    <div class="athlete-head">
      ${avatar(ath.avatarUrl)}
      <div>
        <h3>${esc(ath.name)}</h3>
        <p class="sub">${ath.standing
          ? `King rank #${ath.standing.overall_rank} of ${standings.length} · ${ath.standing.points} pts`
          : "Not yet on the board"}</p>
      </div>
    </div>
    <div class="stat-grid">
      ${stat("King rank", ath.standing ? "#" + ath.standing.overall_rank : "—")}
      ${stat("Points", Math.round(earnedPts * 10) / 10)}
      ${stat("KOMs", koms)}
      ${stat("Scoring (top 10)", scored)}
      ${stat("Attempted", `${attempted.length} / ${filteredSegs.length}`)}
      ${stat("To do", remaining.length)}
    </div>
    <h4>Segment-by-segment</h4>
    <p class="hint">Every tracked segment with this athlete's standing. Rows with no time are not-yet-attempted — the to-do list. Click a segment name for the full leaderboard.</p>
    <div class="seg-card"><table>
      <thead><tr><th>Segment</th><th>Terrain</th><th class="num">Difficulty</th>
        <th class="num">Rank</th><th class="num">Time</th><th class="num">Gap to KOM</th>
        <th class="num">Points</th></tr></thead>
      <tbody>${attempted.map(rowHtml).join("")}${remaining.map(rowHtml).join("")}</tbody>
    </table></div>
    <p class="hint" style="margin-top:12px">Note: times outside a segment's top 25 aren't
    always refreshed, so some efforts here may be missing or stale.</p>`;
  $("#detail-overlay").classList.remove("hidden");
}

// ---- King standings ---------------------------------------------------------
function renderKing() {
  const all = standings;
  const shown = all.slice(0, kingExpanded ? KING_MAX : KING_CAP);
  const rows = shown.map((a) => `
    <tr class="king-${a.overall_rank}">
      <td><span class="rankbadge">${a.overall_rank}</span></td>
      <td><a href="#athlete/${a.athlete_id}">${esc(a.name)}</a></td>
      <td class="num pts">${a.points}</td>
      <td class="num">${a.segments_won}</td>
      <td class="num">${a.segments_scored}</td>
    </tr>`).join("");

  let toggle = "";
  if (all.length > KING_CAP) {
    const expandTo = Math.min(KING_MAX, all.length);
    toggle = kingExpanded
      ? `<button class="expand-btn" id="king-toggle">Show top ${KING_CAP} ▲</button>`
      : `<button class="expand-btn" id="king-toggle">Show top ${expandTo} ▼</button>`;
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
  const segs = [...segmentsForFilter()].sort((a, b) => {
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
    return `<tr data-id="${s.id}">${cells}</tr>`;
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
    <tr>
      <td class="num">${e.rank ?? "—"}</td>
      <td><div class="name-cell">${avatar(e.avatar_url, e.name)}<a href="#athlete/${e.athlete_id}">${esc(e.name)}</a></div></td>
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
  if (/^#(segment|athlete)\//.test(location.hash))
    history.replaceState(null, "", location.pathname + location.search);
}
function openFromHash() {
  const m = location.hash.match(/^#(athlete|segment)\/(\d+)$/);
  if (!m) { closeDetail(); return; }
  if (m[1] === "athlete") openAthlete(+m[2]);
  else openDetail(+m[2]);
}

// ---- wiring -----------------------------------------------------------------
function init() {
  $("#generated").textContent =
    "updated " + new Date(DATA.generated_at).toLocaleString();

  // Discipline filter switch
  const dsw = document.getElementById("discipline-switch");
  if (dsw) {
    dsw.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-discipline]");
      if (!b) return;
      disciplineFilter = b.dataset.discipline;
      dsw.querySelectorAll("button").forEach((x) =>
        x.classList.toggle("active", x === b));
      kingExpanded = false;
      applyDisciplineFilter();
    });
  }

  renderFiltered();
  applyDisciplineFilter();
  if (typeof initMap === "function" && DATA.boundary !== undefined) initMap(DATA);
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
