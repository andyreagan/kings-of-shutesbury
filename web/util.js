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
// Average speed in mph from a distance (m) and an elapsed time (s).
const fmtMph = (m, sec) => (m == null || !sec) ? "—" : (m / M_PER_MI / (sec / 3600)).toFixed(1);
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

// Compact "time since" for an ISO timestamp — "today", "3d ago", "2mo ago".
// Returns "never" for null/empty (a segment we've never swept past the page seed).
function fmtAgo(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return "never";
  const days = Math.floor((Date.now() - then) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "1d ago";
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.round(days / 30)}mo ago`;
  return `${Math.round(days / 365)}y ago`;
}

async function loadData() {
  const url = (window.SITE && window.SITE.data) ? window.SITE.data : "data.json";
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}
