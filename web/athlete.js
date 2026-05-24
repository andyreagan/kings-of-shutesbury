"use strict";

let DATA = null;

function athleteId() {
  const id = new URLSearchParams(location.search).get("id");
  return id ? Number(id) : null;
}

function findEffort(seg, id) {
  return seg.efforts.find((e) => e.athlete_id === id);
}

function resolveAthlete(id) {
  const standing = (DATA.king || []).find((k) => k.athlete_id === id);
  let name = standing ? standing.name : null;
  let avatarUrl = null;
  for (const seg of DATA.segments) {
    const e = findEffort(seg, id);
    if (e) { name = name || e.name; avatarUrl = avatarUrl || e.avatar_url; }
    if (name && avatarUrl) break;
  }
  const featured = (DATA.featured_athletes || []).find((a) => a.id === id);
  if (featured) { name = name || featured.name; avatarUrl = avatarUrl || featured.avatar_url; }
  return { id, name: name || `Athlete ${id}`, avatarUrl, standing };
}

function render() {
  const id = athleteId();
  if (!id) {
    $("#athlete-head").innerHTML = `<div class="err">No athlete id. Use <code>athlete.html?id=&lt;athleteId&gt;</code>.</div>`;
    return;
  }
  const ath = resolveAthlete(id);

  // Per-segment standings for this athlete.
  const rows = DATA.segments.map((seg) => {
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

  // Header.
  $("#athlete-head").innerHTML = `
    <div class="athlete-head">
      ${avatar(ath.avatarUrl)}
      <div>
        <h1>${esc(ath.name)}</h1>
        <p class="sub">${ath.standing
          ? `King rank #${ath.standing.overall_rank} of ${DATA.king.length} · ${ath.standing.points} pts`
          : "Not yet on the board"}</p>
      </div>
    </div>`;

  // Summary stats.
  const stat = (k, v) => `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $("#athlete-summary").innerHTML = `
    <div class="stat-grid">
      ${stat("King rank", ath.standing ? "#" + ath.standing.overall_rank : "—")}
      ${stat("Points", Math.round(earnedPts * 10) / 10)}
      ${stat("KOMs", koms)}
      ${stat("Scoring (top 10)", scored)}
      ${stat("Attempted", `${attempted.length} / ${DATA.segments.length}`)}
      ${stat("To do", remaining.length)}
    </div>`;

  // Sort: attempted by points desc, then remaining by difficulty desc.
  attempted.sort((a, b) => (b.eff.points || 0) - (a.eff.points || 0) ||
    a.eff.rank - b.eff.rank);
  remaining.sort((a, b) => b.seg.difficulty - a.seg.difficulty);

  const rowHtml = ({ seg, eff, gap }) => `
    <tr data-id="${seg.id}" class="${eff ? "" : "todo"}">
      <td><strong>${esc(seg.name)}</strong><br><span class="muted">${esc(seg.location || "")}</span></td>
      <td><span class="pill ${seg.terrain}">${seg.terrain}</span></td>
      <td class="num diff">${seg.difficulty}</td>
      <td class="num">${eff ? (eff.rank ? "#" + eff.rank : "—") : "—"}</td>
      <td class="num">${eff ? fmtTime(eff.elapsed_time) : '<span class="muted">not attempted</span>'}</td>
      <td class="num">${eff && eff.rank !== 1 ? fmtGap(gap) : (eff ? '<span class="kom">KOM</span>' : "—")}</td>
      <td class="num pts">${eff && eff.points ? eff.points : ""}</td>
    </tr>`;

  $("#athlete-table").innerHTML = `
    <div class="seg-card"><table class="clickable">
      <thead><tr><th>Segment</th><th>Terrain</th><th class="num">Difficulty</th>
        <th class="num">Rank</th><th class="num">Time</th><th class="num">Gap to KOM</th>
        <th class="num">Points</th></tr></thead>
      <tbody>${attempted.map(rowHtml).join("")}${remaining.map(rowHtml).join("")}</tbody>
    </table></div>`;

  $("#athlete-table").querySelectorAll("tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => {
      location.href = `index.html#segment/${tr.dataset.id}`;
    }));

  document.title = `${ath.name} · Kings of Shutesbury`;
}

loadData()
  .then((d) => { DATA = d; render(); })
  .catch((err) => {
    $("#athlete-head").innerHTML = `<div class="err">Couldn't load <code>data.json</code> (${err}).<br>
      Serve the folder: <code>cd web &amp;&amp; python3 -m http.server</code>.</div>`;
  });
