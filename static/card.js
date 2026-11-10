/* Touchline Tracker match report card: a 1080×1350 picture of one saved match, made to share with parents on WhatsApp.
   Positive only: our goals, best moments and every player's best stats. Goals against appear only in the score
   (which can be hidden). Drawn on the phone with a canvas, so it works with no signal. */
const TTCard = (() => {
  const { minute, dayKey, teamColors } = TT;
  const W = 1080, H = 1350, PAD = 56;
  const DISPLAY = '"Barlow Condensed","Arial Narrow",sans-serif', BODY = 'Figtree,system-ui,sans-serif';
  const CHALK = '#f4fbf4';
  const ICONS = {
    up: 'M3 17l6-6 4 4 8-8M15 7h6v6',
    star: 'M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z',
    shield: 'M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6zM8.5 12l2.5 2.5 4.5-5',
    clock: 'M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 7v5l3 2',
    pass: 'M5 12h14M13 6l6 6-6 6',
    team: 'M8 11a3 3 0 1 0 0-6a3 3 0 1 0 0 6zM16 11a3 3 0 1 0 0-6a3 3 0 1 0 0 6zM2 20c0-3 3-5 6-5s6 2 6 5M14 15c3 0 8 1 8 5',
  };
  const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
  const ord = n => n + (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] || 'th');
  const res = h => h.us > h.them ? 'W' : h.us === h.them ? 'D' : 'L';
  const norm = s => String(s || '').trim().toLowerCase();

  /* ---------- what goes on the card ---------- */
  // ctx: {history, squad, settings, teams}; h: one saved match
  function model(h, ctx) {
    const nameOf = id => (ctx.squad.find(p => p.id === id) || {}).name;
    const st = ctx.settings || {};
    const tl = (h.timeline || []).slice().sort((a, b) => a.t - b.t);
    const ours = tl.filter(e => e.type === 'goal');
    const len = Math.max(h.len || 0, (st.matchMin || 12) * 60000, ...tl.map(e => e.t + 30000));
    const today = ctx.history.filter(x => dayKey(x.date) === dayKey(h.date)).sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const before = ctx.history.filter(x => String(x.date) < String(h.date));
    const players = h.players || {};

    const moments = [];
    const add = (score, icon, title, text, pid, kind) => moments.push({ id: title, score, icon, title, text, pid, kind });
    // the score as the game went: how far behind we were, and when
    let u = 0, t = 0, worst = 0, worstAt = null;
    for (const e of tl) {
      if (e.type === 'goal') u++; else if (e.type === 'against') t++; else continue;
      if (t - u > worst) { worst = t - u; worstAt = `${u}–${t}`; }
    }
    const r = res(h), lateFrom = len - 2 * 60000;
    if (worst > 0 && r === 'W') add(100, 'up', 'Comeback win', `From ${worstAt} down to win ${h.us}–${h.them}`);
    else if (worst > 0 && r === 'D') add(95, 'up', 'Fought back', `From ${worstAt} down to draw ${h.us}–${h.them}`);
    const last = ours[ours.length - 1];
    if (r === 'W' && h.us - h.them === 1 && last && last.t >= lateFrom && nameOf(last.pid))
      add(92, 'clock', 'Late winner', `${nameOf(last.pid)} won it in the ${ord(minute(last.t))} minute`, last.pid);
    if (r === 'W' && !before.some(x => norm(x.opp) === norm(h.opp) && res(x) === 'W') && before.some(x => norm(x.opp) === norm(h.opp)))
      add(80, 'star', 'First win', `Our first win against ${h.opp}`);
    for (const [pid, v] of Object.entries(players)) {
      const n = nameOf(pid); if (!n) continue;
      const mine = ours.filter(e => e.pid === pid);
      if (v.g >= 3) add(90 + v.g, 'star', `${n}'s hat-trick`, v.g > 3 ? `${v.g} goals in one game` : 'Three goals in one game', pid);
      else if (v.g === 2) {
        const gap = mine.length === 2 ? Math.round((mine[1].t - mine[0].t) / 60000) : null;
        add(70, 'star', `${n}'s double`, gap !== null && gap <= 3 ? `Two goals in ${plural(Math.max(1, gap), 'minute')}` : 'Two goals in one game', pid);
      }
      if (v.g > 0 && !before.some(x => (x.players?.[pid]?.g || 0) > 0))
        add(85, 'star', `${n}'s first goal`, before.some(x => (x.players?.[pid]?.min || 0) > 0) ? 'First goal of the season' : 'Scored in their first game', pid, 'first');
      const saves = v.gs ?? (v.gkMin >= 1 && v.min - v.gkMin < 1 ? v.s || 0 : 0); // older matches didn't split saves from tackles
      if (saves >= 3) add(60 + saves, 'shield', `${n}'s big saves`, `${plural(saves, 'save')} in goal`, pid);
      if (v.a >= 2) add(55 + v.a, 'pass', `${n} the playmaker`, `Set up ${plural(v.a, 'goal')}`, pid);
    }
    if (h.them === 0) add(75, 'shield', 'Clean sheet', 'Nothing let in all game');
    if (h.us - h.them >= 3) add(65, 'up', 'Big win', `Won by ${plural(h.us - h.them, 'goal')}`);
    const scorers = new Set(ours.map(e => e.pid).filter(Boolean));
    if (scorers.size >= 3) add(58, 'team', 'Team goals', `Goals from ${scorers.size} different players`);
    if (r === 'L' && ours.some(e => e.t >= lateFrom)) add(57, 'clock', 'Never gave up', 'Scored in the last two minutes');
    const work = Object.values(players).reduce((a, v) => a + (v.s || 0), 0);
    if (work >= 4 && Object.values(players).filter(v => v.s > 0).length >= 3) add(40, 'shield', 'Hard work', `${work} saves and tackles as a team`);
    if (h.us > 0 && r !== 'W') add(35, 'star', 'On the scoresheet', `${plural(h.us, 'goal')} scored`);
    add(10, 'team', 'Team effort', 'Everyone played their part');
    // best three: one per child and one "first goal" while there are other moments to show, then the rest
    moments.sort((a, b) => b.score - a.score);
    const picked = [];
    for (const strict of [true, false]) for (const m of moments) {
      if (picked.length === 3 || picked.includes(m)) continue;
      if (m.id === 'Team effort' && picked.length > 1) continue;
      if (strict && ((m.pid && picked.some(x => x.pid === m.pid)) || (m.kind && picked.some(x => x.kind === m.kind)))) continue;
      picked.push(m);
    }

    // every child who played, in shirt order, with their two best stats (minutes fill the gap)
    const stars = ctx.squad.filter(p => (players[p.id]?.min || 0) > 0.05).sort((a, b) => (+a.num || 0) - (+b.num || 0)).map(p => {
      const v = players[p.id], s = [];
      if (v.g) s.push(plural(v.g, 'goal'));
      if (v.a) s.push(plural(v.a, 'assist'));
      if (v.gs != null) { if (v.gs) s.push(plural(v.gs, 'save')); if (v.s - v.gs > 0) s.push(plural(v.s - v.gs, 'tackle')); }
      else if (v.s) s.push(v.gkMin >= 1 && v.min - v.gkMin < 1 ? plural(v.s, 'save') : plural(v.s, 'save & tackle', 'saves & tackles'));
      if (v.gkMin >= 1) s.push(`${Math.round(v.gkMin)} min in goal`);
      s.push(`${Math.max(1, Math.round(v.min))} min`);
      return { num: p.num, name: p.name, stats: s.slice(0, 2).join(' · ') };
    });

    const upTo = today.slice(0, today.findIndex(x => x.id === h.id) + 1);
    const wins = upTo.filter(x => res(x) === 'W').length, goalsToday = upTo.reduce((a, x) => a + x.us, 0);
    const dayLine = upTo.length < 2 ? '' : wins ? `Today so far: ${plural(wins, 'win')} from ${upTo.length}` : goalsToday ? `Today so far: ${plural(goalsToday, 'goal')} scored` : '';
    const opp = (ctx.teams || []).find(x => norm(x.name) === norm(h.opp)) || { name: h.opp };
    return {
      date: new Date(h.date).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }),
      game: today.length > 1 ? `Game ${upTo.length} of ${today.length}` : '',
      us: { id: 'us', name: st.teamName || 'Us', c1: st.c1, c2: st.c2, badge: st.badge }, them: opp,
      score: [h.us, h.them], result: r, len,
      goals: ours.map(e => ({ t: e.t, label: `${minute(e.t)}' ${nameOf(e.pid) || ''}`.trim() })),
      moments: picked, stars, dayLine,
    };
  }

  /* ---------- drawing ---------- */
  function rr(c, x, y, w, h, r) { c.beginPath(); c.roundRect ? c.roundRect(x, y, w, h, r) : c.rect(x, y, w, h); }
  function stripes(c, x, y, w, h, a, b, step) {
    c.fillStyle = a; c.fillRect(x, y, w, h); c.fillStyle = b;
    for (let i = x + step; i < x + w; i += step * 2) c.fillRect(i, y, Math.min(step, x + w - i), h);
  }
  function font(c, weight, size, fam) { c.font = `${weight} ${size}px ${fam}`; }
  function fit(c, text, max, weight, size, fam, min = 14) { // shrink to fit one line, then cut with …
    let s = size; font(c, weight, s, fam);
    while (c.measureText(text).width > max && s > min) font(c, weight, --s, fam);
    if (c.measureText(text).width <= max) return text;
    while (text.length > 1 && c.measureText(text + '…').width > max) text = text.slice(0, -1);
    return text + '…';
  }
  function wrap(c, text, max, lines) {
    const words = String(text).split(' '), out = [];
    let cur = '';
    for (const w of words) {
      const next = cur ? cur + ' ' + w : w;
      if (c.measureText(next).width <= max || !cur) cur = next; else { out.push(cur); cur = w; }
    }
    if (cur) out.push(cur);
    if (out.length > lines) { out.length = lines; out[lines - 1] = out[lines - 1].replace(/\s*\S*$/, '') + '…'; }
    return out;
  }
  function spaced(c, text, x, y, em, align = 'left') { // letter-spaced capitals, for labels
    const gap = parseFloat(c.font.match(/(\d+)px/)[1]) * em;
    let w = 0; for (const ch of text) w += c.measureText(ch).width + gap; w -= gap;
    let cx = align === 'right' ? x - w : align === 'center' ? x - w / 2 : x;
    const prev = c.textAlign; c.textAlign = 'left';
    for (const ch of text) { c.fillText(ch, cx, y); cx += c.measureText(ch).width + gap; }
    c.textAlign = prev;
  }
  const light = hex => { const v = parseInt(hex.slice(1), 16); return ((v >> 16) * 299 + ((v >> 8) & 255) * 587 + (v & 255) * 114) / 1000 > 150; };
  function initials(name) {
    const w = String(name || '').trim().split(/\s+/).filter(Boolean);
    return (w.length > 1 ? w[0][0] + w[1][0] : (w[0] || '?').slice(0, 2)).toUpperCase();
  }
  function badge(c, t, cx, cy, r, img) {
    const [c1, c2] = teamColors(t);
    c.save();
    c.beginPath(); c.arc(cx, cy, r + 5, 0, Math.PI * 2); c.fillStyle = 'rgba(244,251,244,.9)'; c.fill();
    c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2); c.clip();
    if (img) {
      c.fillStyle = '#fff'; c.fillRect(cx - r, cy - r, 2 * r, 2 * r);
      const inner = r * 1.64, k = Math.min(inner / img.naturalWidth, inner / img.naturalHeight);
      c.drawImage(img, cx - img.naturalWidth * k / 2, cy - img.naturalHeight * k / 2, img.naturalWidth * k, img.naturalHeight * k);
      c.restore(); c.save();
      c.beginPath(); c.arc(cx, cy, r - r / 16, 0, Math.PI * 2); c.lineWidth = r / 8; c.strokeStyle = c1; c.stroke();
    } else {
      c.fillStyle = c1; c.fillRect(cx - r, cy - r, 2 * r, 2 * r);
      c.beginPath(); c.moveTo(cx + r, cy - r); c.lineTo(cx + r, cy + r); c.lineTo(cx - r, cy + r); c.closePath(); c.fillStyle = c2; c.fill();
      font(c, 800, Math.round(r * .82), DISPLAY); c.textAlign = 'center'; c.textBaseline = 'middle';
      const txt = initials(t.name), ty = cy + r * .04;
      if (light(c1) && light(c2)) { c.fillStyle = '#14231a'; c.fillText(txt, cx, ty); }
      else { c.lineJoin = 'round'; c.lineWidth = r / 9; c.strokeStyle = 'rgba(20,35,26,.85)'; c.strokeText(txt, cx, ty); c.fillStyle = '#fff'; c.fillText(txt, cx, ty); }
    }
    c.restore();
  }
  function icon(c, name, x, y, size, color) {
    c.save(); c.translate(x, y); c.scale(size / 24, size / 24);
    c.strokeStyle = color; c.lineWidth = 2; c.lineCap = 'round'; c.lineJoin = 'round';
    c.stroke(new Path2D(ICONS[name] || ICONS.star)); c.restore();
  }
  function loadImg(src) {
    return new Promise(ok => { if (!src) return ok(null); const im = new Image(); const t = setTimeout(() => ok(null), 4000);
      im.onload = () => { clearTimeout(t); ok(im); }; im.onerror = () => { clearTimeout(t); ok(null); }; im.src = src; });
  }

  // opts: {showScore, showGoals, showStats, hide: [moment ids], accent, src: hash -> image url}
  async function draw(canvas, m, opts = {}) {
    const accent = opts.accent || '#ffcf33', src = opts.src || (h => '/badge/' + h);
    try { await Promise.all(['800 40px "Barlow Condensed"', '700 40px "Barlow Condensed"', '400 20px Figtree', '600 20px Figtree', '700 20px Figtree'].map(f => document.fonts.load(f))); } catch (e) {}
    const [ui, ti] = await Promise.all([loadImg(m.us.badge && src(m.us.badge)), loadImg(m.them.badge && src(m.them.badge))]);
    canvas.width = W; canvas.height = H;
    const c = canvas.getContext('2d');
    c.textBaseline = 'alphabetic'; c.textAlign = 'left';
    stripes(c, 0, 0, W, H, '#10301d', '#133722', 90);

    // header: badges, names, score
    const hx = PAD, hy = 52, hw = W - 2 * PAD, hh = 386;
    c.save(); rr(c, hx, hy, hw, hh, 32); c.clip(); stripes(c, hx, hy, hw, hh, '#1f6b3a', '#1b6034', 60); c.restore();
    rr(c, hx + 1, hy + 1, hw - 2, hh - 2, 31); c.strokeStyle = 'rgba(244,251,244,.14)'; c.lineWidth = 2; c.stroke();
    c.fillStyle = accent; font(c, 800, 30, DISPLAY); spaced(c, 'MATCH REPORT', hx + 40, hy + 62, .14);
    c.fillStyle = 'rgba(244,251,244,.9)'; font(c, 600, 24, BODY); c.textAlign = 'right';
    c.fillText([m.date, m.game].filter(Boolean).join(' · '), hx + hw - 40, hy + 60); c.textAlign = 'left';
    const col = (hw - 80) / 3, cx = i => hx + 40 + col * i + col / 2, by = hy + 200;
    badge(c, m.us, cx(0), by, 75, ui); badge(c, m.them, cx(2), by, 75, ti);
    c.fillStyle = CHALK; c.textAlign = 'center';
    [m.us.name, m.them.name].forEach((n, i) => { const s = fit(c, n, col - 16, 700, 28, BODY, 18); c.fillText(s, cx(i * 2), by + 75 + 50); });
    const chip = opts.showScore === false ? (m.result === 'L' ? '' : m.result === 'W' ? 'WIN' : 'DRAW') : m.result === 'W' ? 'WIN' : m.result === 'D' ? 'DRAW' : '';
    if (opts.showScore !== false) { font(c, 800, 150, DISPLAY); c.fillText(`${m.score[0]}–${m.score[1]}`, cx(1), by + (chip ? 40 : 60)); }
    else { font(c, 800, 90, DISPLAY); c.fillText('vs', cx(1), by + (chip ? 20 : 34)); }
    if (chip) {
      font(c, 800, 28, DISPLAY); const w = c.measureText(chip).width + 28 * .16 * (chip.length - 1) + 44, y0 = by + (opts.showScore !== false ? 66 : 50);
      rr(c, cx(1) - w / 2, y0, w, 44, 22); c.fillStyle = accent; c.fill();
      c.fillStyle = '#1a1a00'; spaced(c, chip, cx(1), y0 + 32, .16, 'center');
    }
    c.textAlign = 'left';

    let y = hy + hh + 30;
    const heading = (text, right) => {
      c.fillStyle = CHALK; font(c, 800, 32, DISPLAY); spaced(c, text.toUpperCase(), PAD, y + 28, .06);
      if (right) { c.fillStyle = 'rgba(244,251,244,.8)'; font(c, 600, 20, BODY); c.textAlign = 'right'; c.fillText(right, W - PAD, y + 26); c.textAlign = 'left'; }
      y += 44;
    };

    // our goals on a timeline. Labels go in the first free lane (above, below, then further out);
    // with too many goals to label, it becomes a list of chips instead.
    if (m.goals.length && opts.showGoals !== false) {
      heading('Our goals');
      const lx = PAD + 34, lw = W - 2 * PAD - 80, x = t => lx + 14 + (lw - 28) * Math.min(1, t / m.len);
      font(c, 700, 22, BODY);
      const lanes = [[], [], [], []], place = [];
      for (const g of m.goals) {
        const gx = x(g.t), w = c.measureText(g.label).width, a = Math.min(Math.max(gx - w / 2, PAD), W - PAD - w);
        const k = lanes.findIndex(l => l.every(([s, e]) => a > e + 14 || a + w < s - 14));
        if (k < 0) { place.length = 0; break; }
        lanes[k].push([a, a + w]); place.push({ gx, tx: a + w / 2, k, label: g.label });
      }
      if (place.length) {
        const two = place.some(p => p.k > 1), ly = y + (two ? 92 : 66), dy = [-28, 46, -58, 76];
        c.fillStyle = 'rgba(244,251,244,.3)'; rr(c, lx, ly - 2, lw, 4, 2); c.fill();
        c.fillStyle = 'rgba(244,251,244,.7)'; font(c, 600, 18, BODY); c.textAlign = 'right';
        c.fillText("0'", lx - 10, ly + 6); c.textAlign = 'left'; c.fillText(`${Math.round(m.len / 60000)}'`, lx + lw + 10, ly + 6);
        for (const p of place) {
          c.beginPath(); c.arc(p.gx, ly, 17, 0, Math.PI * 2); c.fillStyle = '#10301d'; c.fill();
          c.beginPath(); c.arc(p.gx, ly, 13, 0, Math.PI * 2); c.fillStyle = accent; c.fill();
        }
        c.fillStyle = CHALK; c.textAlign = 'center'; font(c, 700, 22, BODY);
        for (const p of place) c.fillText(p.label, p.tx, ly + dy[p.k]);
        c.textAlign = 'left';
        y += two ? 176 : 120;
      } else {
        let cx0 = PAD, cy0 = y + 6; font(c, 700, 22, BODY);
        for (const g of m.goals) {
          const w = c.measureText(g.label).width + 54;
          if (cx0 + w > W - PAD) { cx0 = PAD; cy0 += 52; }
          rr(c, cx0, cy0, w, 42, 21); c.fillStyle = 'rgba(244,251,244,.08)'; c.fill();
          c.beginPath(); c.arc(cx0 + 22, cy0 + 21, 8, 0, Math.PI * 2); c.fillStyle = accent; c.fill();
          c.fillStyle = CHALK; c.fillText(g.label, cx0 + 38, cy0 + 29); cx0 += w + 10;
        }
        y = cy0 + 42 + 30;
      }
    }

    // best moments
    const ms = m.moments.filter(x => !(opts.hide || []).includes(x.id));
    if (ms.length) {
      heading('Best moments');
      const gap = 16, cw = (W - 2 * PAD - gap * (ms.length - 1)) / ms.length, ch = 206;
      ms.forEach((mo, i) => {
        const x0 = PAD + i * (cw + gap);
        rr(c, x0, y, cw, ch, 22); c.fillStyle = 'rgba(244,251,244,.08)'; c.fill();
        c.strokeStyle = 'rgba(244,251,244,.1)'; c.lineWidth = 2; c.stroke();
        icon(c, mo.icon, x0 + 22, y + 22, 40, accent);
        c.fillStyle = CHALK; font(c, 800, 34, DISPLAY);
        const tl = wrap(c, mo.title, cw - 44, 2); tl.forEach((ln, k) => c.fillText(ln, x0 + 22, y + 104 + k * 34));
        c.fillStyle = 'rgba(244,251,244,.92)'; font(c, 400, 21, BODY);
        wrap(c, mo.text, cw - 44, tl.length > 1 ? 2 : 3).forEach((ln, k) => c.fillText(ln, x0 + 22, y + 140 + (tl.length - 1) * 34 + k * 28));
      });
      y += ch + 30;
    }

    // every player who played: two columns, three for a big squad; a spare slot says well played
    const n = m.stars.length;
    if (n) {
      heading('Team stars', plural(n, 'player'));
      const cols = n > 8 ? 3 : 2, spare = (cols - n % cols) % cols, tiles = [...m.stars, ...Array(spare).fill(null)];
      const rows = tiles.length / cols, gap = 12, limit = H - 84;
      const th = Math.min(96, (limit - y - gap * (rows - 1)) / rows), tw = (W - 2 * PAD - gap * (cols - 1)) / cols, k = Math.min(1, th / 84);
      tiles.forEach((p, i) => {
        const x0 = PAD + (i % cols) * (tw + gap), y0 = y + Math.floor(i / cols) * (th + gap);
        if (!p) { // a spare slot
          if (spare > 1 && i > n) return;
          rr(c, x0, y0, tw, th, 18); c.strokeStyle = 'rgba(244,251,244,.14)'; c.lineWidth = 2; c.stroke();
          c.fillStyle = 'rgba(244,251,244,.85)'; c.textAlign = 'center';
          c.fillText(fit(c, `Well played, ${m.us.name}!`, tw - 32, 700, Math.round(26 * k), DISPLAY, 14), x0 + tw / 2, y0 + th / 2 + 9 * k);
          c.textAlign = 'left'; return;
        }
        rr(c, x0, y0, tw, th, 18); c.fillStyle = 'rgba(244,251,244,.08)'; c.fill();
        const nx = x0 + 18 + 52 * k;
        c.fillStyle = accent; font(c, 800, Math.round(40 * k), DISPLAY); c.textAlign = 'center';
        c.fillText(String(p.num || ''), x0 + 14 + 26 * k, y0 + th / 2 + 14 * k); c.textAlign = 'left';
        if (opts.showStats === false) { // names only, centred in the tile
          c.fillStyle = CHALK; c.fillText(fit(c, p.name, x0 + tw - 14 - nx, 700, Math.round(28 * k), BODY, 13), nx, y0 + th / 2 + 10 * k);
          return;
        }
        c.fillStyle = CHALK; c.fillText(fit(c, p.name, x0 + tw - 14 - nx, 700, Math.round(26 * k), BODY, 13), nx, y0 + th / 2 - 4 * k);
        c.fillStyle = 'rgba(244,251,244,.88)'; c.fillText(fit(c, p.stats, x0 + tw - 14 - nx, 400, Math.round(20 * k), BODY, 12), nx, y0 + th / 2 + 24 * k);
      });
    }

    c.fillStyle = 'rgba(244,251,244,.6)'; font(c, 600, 18, BODY);
    if (m.dayLine) c.fillText(m.dayLine, PAD, H - 44);
    c.textAlign = 'right'; c.fillText('Touchline Tracker', W - PAD, H - 44); c.textAlign = 'left';
    return canvas;
  }

  return { model, draw, W, H };
})();
