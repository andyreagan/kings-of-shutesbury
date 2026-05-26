"use strict";

const M_PER_MI = 1609.34;
const M_PER_FT = 0.3048;

const $ = (sel) => document.querySelector(sel);

function fmtTime(s) {
  if (s == null) return "—";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtGap(s) {
  if (s == null) return "—";
  if (s === 0) return "—";
  return "+" + fmtTime(s);
}
const fmtMiles = (m) => m == null ? "—" : (m / M_PER_MI).toFixed(2);
const fmtFeet = (m) => m == null ? "—" : Math.round(m / M_PER_FT).toLocaleString();
const fmtGrade = (g) => g == null ? "—" : `${g.toFixed(1)}%`;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const avatar = (url) => url
  ? `<img class="avatar" src="${esc(url)}" alt="" loading="lazy">`
  : `<span class="avatar"></span>`;

// Direct link to a specific segment effort (the exact ride, segment highlighted).
function effortUrl(e) {
  return (e && e.activity_id && e.effort_id)
    ? `https://www.strava.com/activities/${e.activity_id}/segments/${e.effort_id}`
    : null;
}
function effortLink(e, label) {
  const u = effortUrl(e);
  return u ? `<a href="${u}" target="_blank" rel="noopener">${label}</a>` : label;
}

async function loadData() {
  const r = await fetch("data.json");
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
