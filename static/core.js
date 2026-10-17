/* Touchline Tracker shared logic: used by the tracker (index.html) and the parent page (share.html).
   A match is a list of events; everything else (who is on, minutes, score, keeper) is replayed from them.
   That lets two phones merge their events safely and lets undo simply hide events. */
const TT = (() => {
  const fmt = ms => { ms = Math.max(0, ms || 0); const s = Math.floor(ms / 1000);
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); };
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const dayKey = d => new Date(d).toLocaleDateString('en-GB');
  const minute = ms => Math.floor((ms || 0) / 60000) + 1; // football style: 0:30 is the 1st minute

  function clockNow(m, wall = Date.now()) {
    const c = (m && m.clock) || {};
    return (c.elapsed || 0) + (c.running ? wall - (c.startedAt || wall) : 0);
  }

  function activeEvents(m) {
    const rm = new Set(m.removed || []);
    return (m.events || []).filter(e => !rm.has(e.id)).sort((a, b) => a.t - b.t || a.n - b.n);
  }

  function replay(m) {
    const st = { onPitch: [], stints: {}, gkStints: {}, gk: null, us: 0, them: 0, conc: {}, againstGk: {}, evs: activeEvents(m) };
    const close = (arr, t) => { const s = arr && arr.find(x => x[1] === null); if (s) s[1] = t; };
    for (const e of st.evs) {
      const p = e.pid;
      if (e.type === 'on' && p && !st.onPitch.includes(p)) {
        st.onPitch.push(p); (st.stints[p] = st.stints[p] || []).push([e.t, null]);
      } else if (e.type === 'off' && st.onPitch.includes(p)) {
        st.onPitch = st.onPitch.filter(x => x !== p); close(st.stints[p], e.t);
        if (st.gk === p) { close(st.gkStints[p], e.t); st.gk = null; }
      } else if (e.type === 'gk') {
        if (st.gk) close(st.gkStints[st.gk], e.t);
        st.gk = null;
        if (p && st.onPitch.includes(p)) { st.gk = p; (st.gkStints[p] = st.gkStints[p] || []).push([e.t, null]); }
      } else if (e.type === 'goal') {
        st.us++;
      } else if (e.type === 'against') {
        st.them++; st.againstGk[e.id] = st.gk;
        if (st.gk) st.conc[st.gk] = (st.conc[st.gk] || 0) + 1;
      }
    }
    return st;
  }

  const sumStints = (arr, t) => (arr || []).reduce((a, [s, e]) => a + ((e ?? t) - s), 0);
  const countOf = (st, pid, type) => st.evs.filter(e => e.pid === pid && e.type === type).length;

  /* Horizontal timeline: our goals above the line, goals against below. items: [{t, type:'goal'|'against', label}] */
  function timelineSVG(items, len) {
    const L = Math.max(len || 0, 60000, ...items.map(i => i.t + 30000));
    const W = 600, H = 100, x = t => 20 + (W - 40) * t / L;
    const mins = Math.ceil(L / 60000), step = mins > 24 ? 5 : mins > 12 ? 2 : 1;
    let g = `<line x1="20" x2="${W - 20}" y1="50" y2="50" stroke="var(--line)" stroke-width="2"/>`;
    for (let m = 0; m <= mins; m += step) {
      const X = x(m * 60000); if (X > W - 12) break;
      g += `<line x1="${X}" x2="${X}" y1="45" y2="55" stroke="var(--muted)" stroke-width="1"/>` +
           `<text x="${X}" y="68" text-anchor="middle" font-size="11" fill="var(--muted)">${m}'</text>`;
    }
    for (const i of items) {
      const X = x(i.t), us = i.type === 'goal', cy = us ? 24 : 86;
      g += `<line x1="${X}" x2="${X}" y1="${us ? 31 : 55}" y2="${us ? 45 : 79}" stroke="${us ? 'var(--good)' : 'var(--bad)'}" stroke-width="2"/>` +
           `<circle cx="${X}" cy="${cy}" r="8" fill="${us ? 'var(--good)' : 'var(--bad)'}"><title>${esc(i.label)}</title></circle>`;
    }
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Goal timeline" style="display:block;max-width:100%">${g}</svg>`;
  }

  return { fmt, esc, dayKey, minute, clockNow, activeEvents, replay, sumStints, countOf, timelineSVG };
})();
