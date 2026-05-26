"use strict";

let DATA = null;
let MAP = null;
let metric = "difficulty";
let tracks = [];            // {seg, included, layer}
let ranges = {};            // {difficulty:{min,max}, length:{min,max}}

// ---- color scales -----------------------------------------------------------
function sequentialColor(t) {            // 0..1  -> green .. red
  t = Math.max(0, Math.min(1, t));
  return `hsl(${Math.round(140 * (1 - t))}, 75%, 48%)`;
}
function divergingColor(grade) {         // grade% -> blue(descent)..grey..red(climb)
  const t = Math.max(-1, Math.min(1, (grade ?? 0) / 8));
  const hue = t >= 0 ? 8 : 210;
  const sat = Math.round(15 + 70 * Math.min(1, Math.abs(t)));
  return `hsl(${hue}, ${sat}%, 50%)`;
}
function colorFor(seg) {
  if (metric === "grade") return divergingColor(seg.avg_grade);
  const v = metric === "length" ? (seg.distance_m || 0) : (seg.difficulty || 0);
  const r = ranges[metric] || { min: 0, max: 1 };
  const t = r.max > r.min ? (v - r.min) / (r.max - r.min) : 0.5;
  return sequentialColor(t);
}

// ---- rendering --------------------------------------------------------------
function styleFor(t) {
  if (!t.included) return { color: "#8b93a7", weight: 2, opacity: 0.55, dashArray: "4 5" };
  return { color: colorFor(t.seg), weight: 4, opacity: 0.9 };
}

function popupHtml(t) {
  const s = t.seg;
  const mi = s.distance_m ? (s.distance_m / 1609.34).toFixed(2) + " mi" : "—";
  const gr = s.avg_grade != null ? s.avg_grade.toFixed(1) + "%" : "—";
  const head = `<strong>${esc(s.name)}</strong><br><span class="muted">${esc(s.location || "")}</span>`;
  const stats = `${esc(s.terrain || "")} · ${mi} · ${gr} · importance ${s.difficulty ?? "—"}`;
  if (t.included)
    return `${head}<br>${stats}<br><a href="index.html#segment/${s.id}">leaderboard →</a>`;
  return `${head}<br><span class="pill out">filtered: ${esc(s.reason || "")}</span><br>${stats}`;
}

function recolor() {
  tracks.forEach((t) => { if (t.layer) t.layer.setStyle(styleFor(t)); });
  renderLegend();
}

function renderLegend() {
  const el = document.getElementById("legend");
  let scale = "";
  if (metric === "grade") {
    scale = `<div class="bar" style="background:linear-gradient(90deg,
      ${divergingColor(-8)}, ${divergingColor(0)}, ${divergingColor(8)})"></div>
      <div class="ends"><span>descent</span><span>flat</span><span>climb</span></div>`;
  } else {
    const r = ranges[metric] || { min: 0, max: 1 };
    const lo = metric === "length" ? (r.min / 1609.34).toFixed(1) + " mi" : Math.round(r.min);
    const hi = metric === "length" ? (r.max / 1609.34).toFixed(1) + " mi" : Math.round(r.max);
    scale = `<div class="bar" style="background:linear-gradient(90deg,
      ${sequentialColor(0)}, ${sequentialColor(0.5)}, ${sequentialColor(1)})"></div>
      <div class="ends"><span>${lo}</span><span>${hi}</span></div>`;
  }
  const label = { difficulty: "Importance", grade: "Average grade", length: "Length" }[metric];
  el.innerHTML = `<div class="legend-title">${label}</div>${scale}
    <div class="legend-note"><span class="swatch filtered"></span> grey dashed = filtered out</div>`;
}

function computeRanges() {
  const inc = DATA.segments;
  const diffs = inc.map((s) => s.difficulty || 0);
  const dists = inc.map((s) => s.distance_m || 0);
  ranges = {
    difficulty: { min: Math.min(...diffs), max: Math.max(...diffs) },
    length: { min: Math.min(...dists), max: Math.max(...dists) },
  };
}

function drawTrack(seg, included) {
  if (!seg.track || seg.track.length < 2) return;
  const t = { seg, included, layer: null };
  t.layer = L.polyline(seg.track, styleFor(t)).addTo(MAP).bindPopup(popupHtml(t));
  tracks.push(t);
}

function init() {
  MAP = L.map("map", { scrollWheelZoom: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(MAP);

  // Town boundary.
  let fitted = false;
  if (DATA.boundary) {
    const b = L.geoJSON({ type: "Feature", geometry: DATA.boundary }, {
      style: { color: "#f4c542", weight: 2, fill: true, fillOpacity: 0.05, dashArray: "6 6" },
      interactive: false,
    }).addTo(MAP);
    MAP.fitBounds(b.getBounds().pad(0.05));
    fitted = true;
  }

  computeRanges();
  (DATA.segments || []).forEach((s) => drawTrack(s, true));
  (DATA.filtered || []).forEach((s) => drawTrack(s, false));

  if (!fitted) {
    const all = tracks.filter((t) => t.layer).map((t) => t.layer.getBounds());
    if (all.length) MAP.fitBounds(all.reduce((a, b) => a.extend(b)));
  }
  renderLegend();

  document.getElementById("metric-switch").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-metric]");
    if (!b) return;
    metric = b.dataset.metric;
    document.querySelectorAll("#metric-switch button")
      .forEach((x) => x.classList.toggle("active", x === b));
    recolor();
  });
  document.getElementById("show-filtered").addEventListener("change", (e) => {
    tracks.forEach((t) => {
      if (t.included || !t.layer) return;
      if (e.target.checked) t.layer.addTo(MAP); else t.layer.remove();
    });
  });
}

loadData()
  .then((d) => { DATA = d; init(); })
  .catch((err) => {
    document.getElementById("map").innerHTML =
      `<div class="err" style="margin:20px">Couldn't load <code>data.json</code> (${err}).</div>`;
  });
