/* ── Jackil widget shapes (J1 p03d) ────────────────────────────────────────
   Ported from Jackil's jackil-widgets.js, drawn in Vigil's look. Every name
   is _jk-prefixed: Vigil's dashboard code already owns global names like
   `stat` and `_legend`. */

const JK_TONE_PASTEL = { ok: 'mint', warn: 'lemon', bad: 'rose', info: 'sky', muted: 'grey' };

//: Chart colours are the design language's comparison slots, used in a fixed
//: order — pastels identify, oc-* slots compare.
const JK_SERIES_COLORS = ['var(--oc-1)', 'var(--oc-2)', 'var(--oc-3)', 'var(--oc-4)'];

function _jkPastel(tone) {
  return JK_TONE_PASTEL[tone] || 'grey';
}

//: Vigil has no tc-* colour classes; the stat value carries its colour inline.
function _jkInk(pastel) {
  return pastel === 'grey' ? 'var(--text-1)' : `var(--${pastel}-ink)`;
}

/* ── Renderers ─────────────────────────────────────────────────────────── */

function _jkStat(body, data) {
  const inner =
    `<div class="jk-stat-value" style="color: ${_jkInk(_jkPastel(data.tone))}">${escHtml(data.value)}</div>`
    + `<div class="jk-stat-label">${escHtml(data.label)}</div>`
    + (data.sub ? `<div class="jk-stat-sub">${escHtml(data.sub)}</div>` : '');
  body.innerHTML = data.href
    // The hrefs are Jackil's own pages; open them beside Vigil's tab.
    ? `<a class="jk-stat" href="${escAttr(data.href)}" target="_blank" rel="noopener">${inner}</a>`
    : `<div class="jk-stat">${inner}</div>`;
}

function _jkList(body, data) {
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<div class="jk-empty"><span class="muted-note">`
      + `${escHtml(data.empty || 'Nothing here yet')}</span></div>`;
    return;
  }
  // Row shape follows Vigil's _dashCard: a fully tinted card — no coloured
  // side border — whose edge colour is the row's tone, title and meta on the
  // left, badge in the right column.
  const cards = items.map((item) => {
    const edge = 'edge-' + _jkPastel(item.tone);
    const main = `<div class="jk-card-main">`
      + `<div class="jk-card-title">${escHtml(item.title || '')}</div>`
      + (item.meta ? `<div class="jk-card-sub">${escHtml(item.meta)}</div>` : '')
      + `</div>`;
    const right = item.badge
      ? `<div class="jk-card-right">${escHtml(item.badge)}</div>`
      : '';
    const inner = main + right;
    const attrs = ` class="jk-card ${edge}" title="${escAttr(item.title || '')}"`;
    return item.href
      ? `<a href="${escAttr(item.href)}" target="_blank" rel="noopener"${attrs}>${inner}</a>`
      : `<div${attrs}>${inner}</div>`;
  }).join('');
  body.innerHTML = `<div class="jk-cards">${cards}</div>`;
}

function _jkTable(body, data) {
  // A column is numeric if any row puts a number in it, so one column always
  // aligns the same way whatever the rows happen to hold.
  const numeric = (data.columns || []).map(() => false);
  const rows = data.rows || [];
  rows.forEach((row) => row.forEach((cell, j) => {
    if (typeof cell === 'number') numeric[j] = true;
  }));
  const head = `<thead><tr>${(data.columns || [])
    .map((c) => `<th>${escHtml(c)}</th>`).join('')}</tr></thead>`;
  const tbody = `<tbody>${rows.map((row) => `<tr>${row
    .map((cell, j) => `<td${numeric[j] ? ' class="num"' : ''}>${escHtml(cell)}</td>`)
    .join('')}</tr>`).join('')}</tbody>`;
  body.innerHTML = `<table class="jk-table">${head}${tbody}</table>`;
}

function _jkGauge(body, data) {
  const hasValue = data.value !== null && data.value !== undefined;
  const pct = hasValue ? Math.max(0, Math.min(100, Number(data.value))) : 0;
  const r = 32;
  const circumference = 2 * Math.PI * r;
  const offset = circumference * (1 - pct / 100);
  const pastel = _jkPastel(data.tone);
  const text = hasValue ? Math.round(Number(data.value)) + '%' : '—';
  // The words below the ring carry the state on their own: colour is never
  // the only channel.
  body.innerHTML = `
    <div class="jk-gauge">
      <svg viewBox="0 0 78 78" class="jk-gauge-ring" aria-hidden="true">
        <circle cx="39" cy="39" r="${r}" class="gauge-bg"/>
        <circle cx="39" cy="39" r="${r}" stroke="var(--${pastel}-ink)"
                stroke-dasharray="${circumference.toFixed(1)}"
                stroke-dashoffset="${offset.toFixed(1)}" class="gauge-fill"/>
      </svg>
      <div class="jk-gauge-text">${escHtml(text)}</div>
      <div class="jk-stat-label">${escHtml(data.label)}</div>
      ${data.sub ? `<div class="jk-stat-sub">${escHtml(data.sub)}</div>` : ''}
    </div>`;
}

/* ── Charts (inline SVG, no chart library) ─────────────────────────────── */

//: Drawn at the widget's real pixel size rather than stretched with
//: preserveAspectRatio="none": stretching distorts the axis type and every
//: mark with it. A ResizeObserver redraws when Gridstack resizes the cell.

function _jkLegend(data) {
  if ((data.series || []).length < 2) return '';
  return `<div class="jk-legend">${(data.series || []).map((s, si) =>
    `<span><i style="background:${JK_SERIES_COLORS[si % JK_SERIES_COLORS.length]}"></i>`
    + `${escHtml(s.name)}</span>`).join('')}</div>`;
}

function _jkSeriesSvg(data, W, H) {
  const labels = data.labels || [];
  const series = data.series || [];
  const values = series.flatMap((s) => s.values || []);
  // Stacked bars reach the sum of their segments, so that sum is the maximum
  // the y scale has to fit; every other kind peaks at a single value.
  const max = data.kind === 'stacked-bar'
    ? Math.max(...labels.map((_, li) => series.reduce((n, s) => n + ((s.values || [])[li] || 0), 0)), 0) || 1
    : Math.max(...values, 0) || 1;
  const padL = 30, padR = 6, padT = 6, padB = 18;
  const plotW = Math.max(10, W - padL - padR);
  const plotH = Math.max(10, H - padT - padB);
  const y = (v) => padT + (1 - v / max) * plotH;
  const colour = (i) => JK_SERIES_COLORS[i % JK_SERIES_COLORS.length];
  const parts = [];

  // Two gridlines with their values: zero and the maximum.
  for (const v of [0, max]) {
    parts.push(`<line x1="${padL}" x2="${W - padR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="var(--grid-line)"/>`
      + `<text class="axis-text" x="${padL - 6}" y="${(y(v) + 3).toFixed(1)}" text-anchor="end">${escHtml(String(v))}</text>`);
  }

  let xOf;
  if (data.kind === 'bar' || data.kind === 'stacked-bar') {
    const stacked = data.kind === 'stacked-bar';
    const groupW = plotW / Math.max(1, labels.length);
    const barW = stacked
      ? Math.max(1, groupW * 0.72)
      : Math.max(1, (groupW * 0.72) / Math.max(1, series.length));
    xOf = (i) => padL + groupW * i + groupW / 2;
    labels.forEach((label, li) => {
      const x0 = stacked ? xOf(li) - barW / 2 : padL + groupW * li + (groupW - barW * series.length) / 2;
      let stackTop = padT + plotH;
      series.forEach((s, si) => {
        const v = (s.values || [])[li] || 0;
        if (stacked && v <= 0) return;
        // One series: each bar is its own category, so it takes its own slot.
        const fill = stacked || series.length > 1 ? colour(si) : colour(li);
        let top = y(v);
        let height = padT + plotH - top;
        if (stacked) {
          stackTop -= (v / max) * plotH;
          top = stackTop;
          height = (v / max) * plotH;
        }
        const isTop = !stacked || si === series.length - 1
          || series.slice(si + 1).every((t) => ((t.values || [])[li] || 0) <= 0);
        const title = stacked ? `${label} · ${s.name || ''}: ${v}` : `${label}: ${v}`;
        parts.push(`<rect x="${(stacked ? x0 : x0 + si * barW).toFixed(1)}" y="${top.toFixed(1)}" `
          + `width="${barW.toFixed(1)}" height="${height.toFixed(1)}"`
          + (isTop ? ' rx="3"' : '') + ` fill="${fill}">`
          + `<title>${escHtml(title)}</title></rect>`);
      });
    });
  } else {
    const step = labels.length > 1 ? plotW / (labels.length - 1) : 0;
    xOf = (i) => padL + step * i;
    series.forEach((s, si) => {
      const vs = s.values || [];
      if (vs.length > 1) {
        const points = vs.map((v, i) => `${xOf(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
        if (data.kind === 'area') {
          parts.push(`<polygon fill="${colour(si)}" fill-opacity="0.18" stroke="none" `
            + `points="${points} ${xOf(vs.length - 1).toFixed(1)},${y(0).toFixed(1)} ${xOf(0).toFixed(1)},${y(0).toFixed(1)}"/>`);
        }
        parts.push(`<polyline fill="none" stroke="${colour(si)}" stroke-width="2" stroke-linejoin="round" `
          + `points="${points}"/>`);
      }
      vs.forEach((v, i) => parts.push(`<circle cx="${xOf(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.5" fill="${colour(si)}">`
        + `<title>${escHtml(`${labels[i] ?? ''}: ${v}`)}</title></circle>`));
    });
  }

  // As many x labels as fit, evenly spaced, first and last always shown.
  // A label's width follows its length (an ISO date needs ~70px), and a label
  // too close to the always-shown last one gives way to it.
  const longest = labels.reduce((n, l) => Math.max(n, String(l).length), 0);
  const fit = Math.max(2, Math.floor(plotW / Math.max(56, longest * 6.5 + 12)) - 1);
  const every = Math.max(1, Math.ceil(labels.length / fit));
  const last = labels.length - 1;
  labels.forEach((label, i) => {
    if (i !== last && (i % every !== 0 || last - i < every)) return;
    const anchor = (data.kind === 'bar' || data.kind === 'stacked-bar') ? 'middle' : (i === 0 ? 'start' : (i === last ? 'end' : 'middle'));
    parts.push(`<text class="axis-text" x="${xOf(i).toFixed(1)}" y="${H - 4}" text-anchor="${anchor}">${escHtml(label)}</text>`);
  });

  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img">${parts.join('')}</svg>`;
}

function _jkSeries(body, data) {
  const legend = _jkLegend(data);
  body.innerHTML = `<div class="jk-series"><div class="jk-series-plot"></div>${legend}</div>`;
  const plot = body.querySelector('.jk-series-plot');
  const draw = () => {
    const W = Math.floor(plot.clientWidth), H = Math.floor(plot.clientHeight);
    if (W > 0 && H > 0) plot.innerHTML = _jkSeriesSvg(data, W, H);
  };
  if (body._seriesObserver) body._seriesObserver.disconnect();
  if (window.ResizeObserver) {
    body._seriesObserver = new ResizeObserver(draw);
    body._seriesObserver.observe(plot);
  }
  draw();
}

/* ── Paint ─────────────────────────────────────────────────────────────── */

const JACKIL_SHAPES = { stat: _jkStat, list: _jkList, table: _jkTable, gauge: _jkGauge, series: _jkSeries };

async function _renderJackilWidget(body, settings, widget) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(settings || {})) {
    if (k === 'title' || v === null || v === undefined || v === '') continue;
    params.set(k, String(v));
  }
  const kind = widget && widget.kind;
  const resp = await fetch('/api/v1/jackil/widgets/' + encodeURIComponent(kind) + '/?' + params,
                           { credentials: 'same-origin' });
  if (!resp.ok) throw new Error('Jackil widget unavailable');
  const env = await resp.json();
  const draw = JACKIL_SHAPES[env.shape];
  if (!draw) throw new Error('Jackil sent a widget Vigil could not draw.');
  draw(body, env.data || {});
}
