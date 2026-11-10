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

  /* Team badges. A team is {id, name, c1, c2, badge}: two kit colours and an optional picture (a hash served at /badge/<hash>).
     With no picture the badge is a circle split into the two colours with the initials on top; with one, the main colour rings it.
     Teams with no colours yet get a pair picked from their id or name, so every team looks different from day one. */
  const PALETTE = [['#ffffff', 'White'], ['#212121', 'Black'], ['#d32f2f', 'Red'], ['#7b1f2b', 'Maroon'], ['#f57c00', 'Orange'],
    ['#fbc02d', 'Yellow'], ['#2e7d32', 'Green'], ['#4fc3f7', 'Sky blue'], ['#1565c0', 'Blue'], ['#1a237e', 'Navy'],
    ['#6a1b9a', 'Purple'], ['#ec407a', 'Pink']];
  function hashOf(s) { let h = 7; for (const ch of String(s || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h; }
  function teamColors(t) {
    const h = hashOf(t.id || String(t.name || '').trim().toLowerCase()), n = PALETTE.length;
    const a = PALETTE[h % n][0], b = PALETTE[(h % n + 1 + (h >>> 8) % (n - 1)) % n][0];
    return t.c1 ? [t.c1, t.c2 || t.c1] : [a, b];
  }
  function initials(name) {
    const w = String(name || '').trim().split(/\s+/).filter(Boolean);
    return (w.length > 1 ? w[0][0] + w[1][0] : (w[0] || '?').slice(0, 2)).toUpperCase();
  }
  const light = hex => { const v = parseInt(hex.slice(1), 16); return ((v >> 16) * 299 + ((v >> 8) & 255) * 587 + (v & 255) * 114) / 1000 > 150; };
  function badge(t, size = 32, src = h => '/badge/' + h) {
    t = t || {}; const [c1, c2] = teamColors(t), s = Math.round(size);
    const box = `display:inline-grid;place-items:center;flex:none;width:${s}px;height:${s}px;border-radius:50%;position:relative;vertical-align:middle;` +
      `box-shadow:0 0 0 1px rgba(0,0,0,.18)${t.badge ? `,0 0 0 ${Math.max(2, s / 16)}px ${c1}` : ''};`;
    const txt = `<span style="font:800 ${Math.round(s * .4)}px/1 var(--display,system-ui);color:${light(c1) ? '#14231a' : '#fff'};letter-spacing:.02em">${esc(initials(t.name))}</span>`;
    const img = t.badge ? `<img src="${esc(src(t.badge))}" alt="" onerror="this.remove()" style="position:absolute;inset:0;width:100%;height:100%;border-radius:50%;object-fit:contain;padding:${Math.round(s * .08)}px;background:#fff">` : '';
    return `<span role="img" aria-label="${esc(t.name || 'Team')} badge" style="${box}background:linear-gradient(135deg,${c1} 50%,${c2} 50%)">${txt}${img}</span>`;
  }

  return { fmt, esc, dayKey, minute, clockNow, activeEvents, replay, sumStints, countOf, timelineSVG, PALETTE, teamColors, badge };
})();
