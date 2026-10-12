// vigil-topology.js
// Owns: the Topology page (QA-32) — one SVG map of the fleet: site boxes, optional tag boxes,
//   host cards (addresses, ports, firewall, stacks), device cards, the links between machines and
//   what they reach outside the fleet, plus a detail panel for the selected card.
// HTML: #page-topology in templates/pages/_topology.html.
// Depends on: vigil-utils.js (apiJson, escHtml, escAttr, delegateClick, timeAgo, showToast),
//   vigil-nav.js (navigateTo — wrapped below), vigil-host-cards.js (openHostDetail),
//   vigil-containers.js (selectContainersHost).
// API: GET /api/v1/hosts/topology/ (apps/hosts/topology.py) — the page's only request.
//
// layoutTopology() is the map's geometry: the response and the view's toggles
// in, plain numbers out, no DOM beyond the canvas width it will wrap into.
// Everything that draws reads its result.

const topoState = { data: null, site: '', tags: new Set(), links: true, containers: false, external: false, q: '', selected: null, view: { x: 0, y: 0, k: 1 }, layout: null, loadedOnce: false, timer: null };

const TOPO_TAG_KEY = 'vigil-topo-tags';
const TOPO_COLORS = ['lavender', 'mint', 'sky', 'peach', 'rose', 'lemon'];
const TOPO_CARD_W = 200, TOPO_CARD_H = 96, TOPO_DEV_W = 150, TOPO_DEV_H = 56;
const TOPO_STACK_ROW_H = 22, TOPO_GAP = 28, TOPO_PAD = 36, TOPO_TITLE_H = 52;
const TOPO_SITE_GAP = 48, TOPO_INTERNET_H = 58;
const TOPO_FADE_MS = 180, TOPO_PAN_MS = 320;

function _topoEl(id) { return document.getElementById(id); }

function _topoHost(id) {
  return ((topoState.data && topoState.data.hosts) || []).find(h => h.id === id) || null;
}

function _topoDevice(id) {
  return ((topoState.data && topoState.data.devices) || []).find(d => d.id === id) || null;
}

function _topoColor(i) { return `var(--${TOPO_COLORS[i % TOPO_COLORS.length]}-ink)`; }

function _topoCount(host) {
  return (host.stacks || []).reduce((n, stack) => n + (stack.containers || []).length, 0);
}

function _topoFirewallText(fw) {
  if (!fw) return 'firewall unknown';
  return `${fw.tool || 'firewall'} ${fw.enabled ? 'on' : 'off'}`;
}

/** Hosts in a site, ordered so each active tag's members stay contiguous — the
 *  only thing that keeps a tag box from swallowing the whole site. */
function _topoOrderedHosts(hosts, tags) {
  const buckets = tags.map(() => []);
  const rest = [];
  const byName = (a, b) => a.hostname.localeCompare(b.hostname, undefined, { sensitivity: 'base' });
  for (const host of hosts) {
    const at = tags.findIndex(tag => (host.tags || []).includes(tag));
    if (at >= 0) buckets[at].push(host); else rest.push(host);
  }
  return buckets.map(b => b.slice().sort(byName)).concat([rest.slice().sort(byName)]).flat();
}

/** Devices go to the site of the host they talk to most; a device that talks to
 *  nobody on the map goes to the first site, the only honest place left. */
function _topoDeviceSites(data) {
  const byId = new Map(data.hosts.map(host => [host.id, host]));
  const counts = new Map();
  for (const link of data.links) {
    for (const end of [['source', 'target'], ['target', 'source']]) {
      const kind = link[end[0]].kind;
      if (kind !== 'host') continue;         // device↔device links don't exist; host↔host votes twice
      const peer = link[end[1]];
      if (peer.kind !== 'device') continue;
      const host = byId.get(link[end[0]].id);
      if (!host) continue;
      const per = counts.get(peer.id) || new Map();
      const key = host.site_id || '';
      per.set(key, (per.get(key) || 0) + (link.count || 1));
      counts.set(peer.id, per);
    }
  }
  const chosen = new Map();
  const fallback = data.sites.length ? data.sites[0].id : '';
  for (const device of data.devices) {
    const per = counts.get(device.id);
    let best = null;
    if (per) {
      let bestCount = -1;
      for (const [siteId, count] of [...per.entries()].sort()) {
        if (count > bestCount) { best = siteId; bestCount = count; }
      }
    }
    chosen.set(device.id, best !== null ? best : fallback);
  }
  return chosen;
}

/** One Internet node per map, parked in the site with the most traffic out. */
function _topoInternetSite(hosts, sites) {
  const counts = new Map();
  for (const host of hosts) {
    if (!(host.external || []).length) continue;
    const key = host.site_id || '';
    counts.set(key, (counts.get(key) || 0) + host.external.reduce((n, e) => n + (e.count || 0), 0));
  }
  let best = null, bestCount = -1;
  for (const [siteId, count] of [...counts.entries()].sort()) {
    if (count > bestCount) { best = siteId; bestCount = count; }
  }
  return best !== null ? best : (sites.length ? sites[0].id : '');
}

/** One site's cards onto a grid, and the box that holds them.

    The width is what the row actually holds, not what the grid could hold: a
    site with four cards in a 3-column grid is three cards wide, and padding it
    out to a fifth would spend the room the next site needs and push a whole
    row of sites onto the line below. A row is as tall as its tallest card, so a
    card that grew — the Containers toggle adds a row per stack — grows the site
    with it instead of overlapping the card under it. */
function _topoPackSite(group, gap, pad, titleH, tagPad = 0) {
  // The Internet node gets a row of its own above the hosts, far enough up
  // that no tag box drawn around the hosts can take it in.
  const internet = group.cards.filter(card => card.kind === 'internet');
  const cards = group.cards.filter(card => card.kind !== 'internet');
  let y = titleH, widest = 0;
  for (const card of internet) {
    card.x = pad; card.y = y;
    widest = Math.max(widest, pad * 2 + card.w);
    y += card.h + gap + tagPad;
  }
  const cols = Math.max(1, Math.ceil(Math.sqrt(cards.length * 1.6)));
  for (let start = 0; start < cards.length; start += cols) {
    const row = cards.slice(start, start + cols);
    row.forEach((card, i) => { card.x = pad + i * (TOPO_CARD_W + gap); card.y = y; });
    widest = Math.max(widest, pad * 2 + row.length * TOPO_CARD_W + (row.length - 1) * gap);
    y += Math.max(...row.map(card => card.h)) + gap;
  }
  return {
    w: widest,
    h: Math.max(y - gap + pad, titleH + TOPO_DEV_H + pad),
  };
}

/** A row of sites, left to right, wrapping at `wrap`. Mutates x/y in place. */
function _topoPlace(groups, wrap, gap) {
  let x = 0, y = 0, rowH = 0, rowRight = 0;
  for (const group of groups) {
    if (x > 0 && x + group.w > wrap) { x = 0; y += rowH + gap; rowRight = 0; rowH = 0; }
    group.x = x; group.y = y;
    x += group.w + gap;
    rowH = Math.max(rowH, group.h);
    rowRight = Math.max(rowRight, group.x + group.w);
  }
  return { width: rowRight, height: y + rowH };
}

function _topoHostRows(host) {
  const rows = [{ cls: 'topo-card-sub', text: host.primary_ip || 'no address' }];
  const listening = (host.listening || []).slice()
    .sort((a, b) => (b.public - a.public) || a.port - b.port);
  if (listening.length) {
    rows.push({
      ports: listening.slice(0, 4).map(l => ({ port: l.port, public: !!l.public })),
      more: listening.length - Math.min(4, listening.length),
    });
  }
  rows.push({ cls: 'topo-card-sub', text: _topoFirewallText(host.firewall) });
  const containers = _topoCount(host);
  if (containers) {
    rows.push({ cls: 'topo-card-sub', text: `${(host.stacks || []).length} stacks · ${containers} containers` });
  }
  if (!host.network_at) rows.push({ cls: 'topo-card-warn', text: 'needs the 2026.14.4 agent' });
  return rows;
}

function _topoHostCard(host, containersOn, siteId) {
  const rows = _topoHostRows(host);
  // Title, then a 16 px line per row from y+44: a card is as tall as its rows.
  const baseH = Math.max(TOPO_CARD_H, 40 + rows.length * 16);
  const extra = containersOn ? (host.stacks || []).length * TOPO_STACK_ROW_H + 6 : 0;
  return {
    x: 0, y: 0, w: TOPO_CARD_W, h: baseH + extra, baseH,
    kind: 'host', refId: host.id, siteId,
    label: host.hostname, offline: host.status !== 'online',
    rows,
  };
}

function _topoStackRows(host) {
  return (host.stacks || []).map(stack => ({
    name: stack.name || 'unnamed',
    containers: (stack.containers || []).slice(0, 6).map(c => ({ name: c.name, running: c.state === 'running' })),
  }));
}

/** How far the box for the tag at `index` stands off its cards: room for its
 *  label inside the strip, and a step per tag so nested boxes stay apart. */
function _topoTagPad(index) { return 16 + 14 * index; }

function _topoTagBoxes(groups, activeTags) {
  const boxes = [];
  for (const group of groups) {
    activeTags.forEach((tag, index) => {
      const own = group.cards.filter(card => card.kind === 'host'
        && (card.tags || []).includes(tag));
      if (!own.length) return;
      const pad = _topoTagPad(index);
      const x0 = Math.min(...own.map(c => c.x));
      const y0 = Math.min(...own.map(c => c.y));
      const x1 = Math.max(...own.map(c => c.x + c.w));
      const y1 = Math.max(...own.map(c => c.y + c.h));
      boxes.push({
        tag, siteId: group.id, x: x0 - pad, y: y0 - pad,
        w: (x1 - x0) + pad * 2, h: (y1 - y0) + pad * 2,
        color: _topoColor(index), pad,
      });
    });
  }
  return boxes;
}

/** The cards one site holds, in grid order: the Internet node first if the map
 *  shows the outside world, then the site's hosts in the tag-aware order that
 *  keeps tag boxes compact, then the devices that belong here. An empty group
 *  gets one placeholder card, because an invisible box explains nothing. */
function _topoSiteCards(groups, hosts, devices, activeTags, internetSite, deviceSites, slot, drawn) {
  let placed = 0;
  if (topoState.external) {
    const at = slot.get(internetSite) ?? 0;
    const reaching = hosts.filter(host => (host.external || []).length && host.site_id === internetSite);
    groups[at].cards.push({
      x: 0, y: 0, w: TOPO_CARD_W, h: TOPO_INTERNET_H, kind: 'internet', refId: 'internet',
      siteId: drawn[at].id, label: 'Internet', offline: false, tags: [],
      rows: [{ cls: 'topo-card-sub', text: `${reaching.length} hosts reach outside the fleet` }],
    });
  }
  for (const host of _topoOrderedHosts(hosts, activeTags)) {
    const at = slot.get(host.site_id);
    if (at === undefined) continue;          // not part of this view
    const card = _topoHostCard(host, topoState.containers, host.site_id);
    card.tags = (host.tags || []).filter(tag => activeTags.includes(tag));
    if (topoState.containers) card.stackRows = _topoStackRows(host);
    groups[at].cards.push(card);
    placed += 1;
  }
  for (const device of devices) {
    const wanted = deviceSites.get(device.id) || '';
    const at = slot.get(wanted) ?? 0;
    groups[at].cards.push({
      x: 0, y: 0, w: TOPO_DEV_W, h: TOPO_DEV_H, kind: 'device', refId: device.id,
      siteId: wanted, label: device.name, offline: false, tags: [],
      rows: [{ cls: 'topo-card-sub', text: `${device.type || 'device'} · ${device.ip}` }],
    });
    placed += 1;
  }
  for (const group of groups) {
    if (group.cards.length) continue;
    group.cards.push({
      x: 0, y: 0, w: TOPO_CARD_W, h: TOPO_DEV_H, kind: 'empty', refId: '', siteId: group.id,
      label: '', offline: true, tags: [],
      rows: [{ cls: 'topo-card-sub', text: placed ? 'Nothing here in this view' : 'No hosts yet' }],
    });
  }
}

/** Pure: the topology JSON in, every site box, card, tag box and link out.
    Takes the whole site list and the selected site rather than reading them
    from state, so the same response twice lays out the same map twice. */
function layoutTopology(data, sitesIn, chosenSite) {
  const hosts = (data.hosts || []).map(host => ({ ...host, site_id: host.site_id || '' }));
  const devices = (data.devices || []).slice().sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
  const links = data.links || [];
  const activeTags = (data.tags || []).map(t => t.name).filter(name => topoState.tags.has(name));

  let sites = (sitesIn || data.sites || []).map(site => ({ id: site.id, name: site.name }));
  if (!sites.length) sites = [{ id: '', name: hosts.length ? 'All hosts' : 'No hosts' }];
  const drawn = chosenSite ? sites.filter(site => site.id === chosenSite) : sites;
  const slot = new Map(drawn.map((site, i) => [site.id, i]));

  const deviceSites = _topoDeviceSites({ hosts, devices, links, sites });
  const internetSite = topoState.external ? _topoInternetSite(hosts, sites) : '';
  const groups = drawn.map(site => ({ id: site.id, name: site.name, cards: [] }));
  _topoSiteCards(groups, hosts, devices, activeTags, internetSite, deviceSites, slot, drawn);

  // The widest tag box must fit inside its site without covering the title.
  const tagPad = activeTags.length ? _topoTagPad(activeTags.length - 1) : 0;
  const sitePad = Math.max(TOPO_PAD, tagPad + 12);
  const titleH = Math.max(TOPO_TITLE_H, 40 + tagPad);
  for (const group of groups) {
    Object.assign(group, _topoPackSite(group, TOPO_GAP, sitePad, titleH, tagPad));
  }

  // The canvas's width is the one thing the layout measures: a map wrapped for
  // the box it is drawn in is a map the user does not have to scroll to read.
  const canvas = _topoEl('topo-canvas');
  const wrap = Math.max(1400, canvas ? canvas.clientWidth : 0);
  const bounds = _topoPlace(groups, wrap, TOPO_SITE_GAP);
  const siteBoxes = groups.map(group => ({
    id: group.id, name: group.name, x: group.x, y: group.y, w: group.w, h: group.h,
  }));
  for (const group of groups) {
    for (const card of group.cards) { card.x += group.x; card.y += group.y; }
  }

  const cards = {};
  for (const card of groups.flatMap(group => group.cards)) {
    // `internet` is a fixed key because the links name it; every other card is
    // looked up by the id the links carry.
    cards[card.kind === 'device' ? `device:${card.refId}` : card.refId] = card;
  }
  return {
    sites: siteBoxes, cards, tagBoxes: _topoTagBoxes(groups, activeTags),
    links: _topoLinkGeometry(data, cards),
    width: bounds.width || wrap, height: bounds.height || (TOPO_TITLE_H + TOPO_CARD_H + TOPO_PAD),
  };
}

function _topoEdge(a, b) {
  /** The two card edge midpoints a line between them crosses, and its axis. */
  const horizontal = Math.abs((a.x + a.w / 2) - (b.x + b.w / 2)) >= Math.abs((a.y + a.h / 2) - (b.y + b.h / 2));
  const x1 = horizontal ? (a.x <= b.x ? a.x + a.w : a.x) : a.x + a.w / 2;
  const y1 = horizontal ? a.y + a.h / 2 : (a.y <= b.y ? a.y + a.h : a.y);
  const x2 = horizontal ? (a.x <= b.x ? b.x : b.x + b.w) : b.x + b.w / 2;
  const y2 = horizontal ? b.y + b.h / 2 : (a.y <= b.y ? b.y : b.y + b.h);
  return { x1, y1, x2, y2, horizontal };
}

function _topoLinkGeometry(data, cards) {
  const out = [];
  for (const link of data.links || []) {
    if (link.source.kind !== 'host' || link.target.kind !== 'host') continue;
    const a = cards[link.source.id], b = cards[link.target.id];
    if (!a || !b) continue;                    // the other end is filtered out of this map
    const edge = _topoEdge(a, b);
    out.push({
      fromId: link.source.id, toId: link.target.id,
      path: _topoCurve(edge), lx: (edge.x1 + edge.x2) / 2, ly: (edge.y1 + edge.y2) / 2,
      count: link.count || 0, ports: link.ports || [],
    });
  }
  if (topoState.external && cards.internet) {
    for (const host of data.hosts || []) {
      if (!(host.external || []).length) continue;
      const card = cards[host.id];
      if (!card) continue;
      const edge = _topoEdge(card, cards.internet);
      out.push({
        fromId: host.id, toId: 'internet', external: true,
        path: _topoCurve(edge), lx: (edge.x1 + edge.x2) / 2, ly: (edge.y1 + edge.y2) / 2,
        count: host.external.reduce((n, e) => n + (e.count || 0), 0), ports: [],
      });
    }
  }
  return out;
}

function _topoCurve(edge) {
  const { x1, y1, x2, y2, horizontal } = edge;
  const dist = Math.max(40, Math.hypot(x2 - x1, y2 - y1) * 0.45);
  const sx = x2 >= x1 ? 1 : -1, sy = y2 >= y1 ? 1 : -1;
  const cx1 = horizontal ? x1 + dist * sx : x1;
  const cx2 = horizontal ? x2 - dist * sx : x2;
  const cy1 = horizontal ? y1 : y1 + dist * sy;
  const cy2 = horizontal ? y2 : y2 - dist * sy;
  return `M${x1.toFixed(1)},${y1.toFixed(1)} C${cx1.toFixed(1)},${cy1.toFixed(1)} `
    + `${cx2.toFixed(1)},${cy2.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`;
}

/* ── Drawing ─────────────────────────────────────────────────────────────── */


/** The live SVG: the canvas's only child, once renderTopology has run.

    The id is an attribute, not a property, so an element this page made is
    recognised by `attrs.id` (a real DOM node answers `undefined` there, which
    is as honest as it needs to be). Looking the canvas up by id rather than
    finding the child some other way is what makes this the *current* map. */
function _topoSvg() {
  const canvas = _topoEl('topo-canvas');
  const svg = canvas && canvas.children && canvas.children.length === 1 ? canvas.children[0] : null;
  return svg && svg.attrs && svg.attrs.id === 'topo-svg' ? svg : null;
}

function _topoSvgEl(name, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const key in attrs) el.setAttribute(key, attrs[key]);
  return el;
}

/** Cards are SVG groups, so they cannot take a CSS transition; this fades the
 *  link highlight, which would otherwise snap and read as a rendering glitch. */
function _topoAnimate(el, property, from, to, ms) {
  if (typeof el.animate !== 'function') return;
  const frames = [{ [property]: from }, { [property]: to }];
  try {
    el.animate(frames, { duration: ms, easing: 'ease-out', fill: 'both' });
  } catch {
    /* A browser without element.animate keeps the CSS value it already has. */
  }
}

function _topoText(parent, attrs, text) {
  const el = _topoSvgEl('text', attrs);
  el.textContent = text;
  parent.appendChild(el);
  return el;
}

function _topoClip(text, max) {
  const s = String(text == null ? '' : text);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** How much of a hostname fits a card. SVG text does not wrap, and a clipped
 *  box would cut the end of the name — which is the part that tells two hosts
 *  apart — so names are shortened from the front instead. */
function _topoClipEnd(text, max) {
  const s = String(text == null ? '' : text);
  return s.length > max ? `…${s.slice(s.length - max + 1)}` : s;
}

function renderTopology() {
  const canvas = _topoEl('topo-canvas');
  const data = topoState.data;
  if (!canvas || !data) return;
  const layout = topoState.layout
    = layoutTopology(data, (data.sites || []).map(site => ({ id: site.id, name: site.name })), topoState.site);
  const view = topoState.view;

  const svg = _topoSvgEl('svg', { id: 'topo-svg', width: '100%', height: '100%' });
  const root = _topoSvgEl('g', {
    id: 'topo-view',
    transform: `translate(${view.x.toFixed(1)},${view.y.toFixed(1)}) scale(${view.k})`,
  });
  svg.appendChild(root);

  for (const site of layout.sites) {
    root.appendChild(_topoSvgEl('rect', {
      class: 'topo-site', x: site.x, y: site.y, width: site.w, height: site.h, rx: 16,
    }));
    const title = _topoSvgEl('text', { class: 'topo-site-title', x: site.x + 20, y: site.y + 30 });
    title.textContent = `${site.name} · ${_topoSiteCount(data, site.id)}`;
    root.appendChild(title);
  }

  for (const box of layout.tagBoxes) {
    root.appendChild(_topoSvgEl('rect', {
      class: 'topo-tagbox', x: box.x, y: box.y, width: box.w, height: box.h, rx: 12,
      stroke: box.color, fill: box.color,
    }));
    _topoText(root, { class: 'topo-tagbox-label', x: box.x + 12, y: box.y + 12, fill: box.color }, box.tag);
  }

  const linkLayer = _topoSvgEl('g', { id: 'topo-links' });
  root.appendChild(linkLayer);
  if (topoState.links) {
    for (const link of layout.links) {
      const cls = 'topo-link' + (link.external ? ' topo-link-ext' : '');
      linkLayer.appendChild(_topoSvgEl('path', {
        class: cls, d: link.path,
        'data-topo-link-from': link.fromId, 'data-topo-link-to': link.toId,
      }));
      if (link.count > 1) {
        _topoText(linkLayer, {
          class: `topo-link-label${link.external ? ' topo-link-ext' : ''}`,
          x: link.lx, y: link.ly - 4, 'text-anchor': 'middle',
        }, String(link.count));
      }
    }
  }

  for (const id in layout.cards) root.appendChild(_topoCard(layout.cards[id], data));

  canvas.replaceChildren(svg);
  _topoWirePanZoom(svg);
  _topoHighlightMatch();
}

function _topoSiteCount(data, siteId) {
  const hosts = data.hosts.filter(host => (host.site_id || '') === siteId);
  return `${hosts.filter(host => host.status === 'online').length}/${hosts.length} online`;
}

function _topoSelKey(card) {
  return card.kind === 'device' ? `device:${card.refId}` : card.refId;
}

function _topoCard(card, data) {
  const host = card.kind === 'host' ? _topoHost(card.refId) : null;
  const g = _topoSvgEl('g', {
    class: `topo-card topo-card-${card.kind}${card.offline ? ' topo-offline' : ''}`
      + (topoState.selected === _topoSelKey(card) ? ' topo-sel' : ''),
    'data-topo-card': _topoSelKey(card),
    'data-topo-link': card.refId,
  });
  g.appendChild(_topoSvgEl('rect', { x: card.x, y: card.y, width: card.w, height: card.h, rx: 10 }));
  if (card.kind !== 'empty') {
    g.appendChild(_topoSvgEl('circle', {
      class: 'topo-dot', cx: card.x + 18, cy: card.y + 22, r: 4.5,
      'data-dot': host ? host.status : (card.kind === 'device' ? 'device' : 'net'),
    }));
  }
  _topoText(g, { class: 'topo-card-title', x: card.x + 30, y: card.y + 26 },
    card.kind === 'host' ? _topoClipEnd(card.label, 22) : _topoClip(card.label, 18));

  let y = card.y + 44;
  for (const row of card.rows) {
    if (row.ports) {
      let x = card.x + 16;
      for (const port of row.ports) {
        const text = String(port.port);
        const w = 14 + text.length * 6.4;
        g.appendChild(_topoSvgEl('rect', {
          class: 'topo-port' + (port.public ? ' topo-port-public' : ''),
          x, y: y - 11, width: w, height: 15, rx: 7,
        }));
        _topoText(g, {
          class: 'topo-port-text' + (port.public ? ' topo-port-public' : ''),
          x: x + w / 2, y, 'text-anchor': 'middle',
        }, text);
        x += w + 5;
      }
      if (row.more > 0) _topoText(g, { class: 'topo-card-sub topo-port-more', x, y }, `+${row.more}`);
    } else {
      _topoText(g, { class: `topo-card-line ${row.cls || ''}`, x: card.x + 16, y },
        _topoClip(row.text, card.kind === 'host' ? 34 : 26));
    }
    y += 16;
  }

  if (card.stackRows) {
    y = card.y + (card.baseH || TOPO_CARD_H) + 6;
    for (const stack of card.stackRows) {
      _topoText(g, { class: 'topo-stack-name', x: card.x + 16, y }, _topoClip(stack.name, 12));
      let x = card.x + 92;
      for (const container of stack.containers) {
        const text = _topoClip(container.name, 10);
        const w = 12 + text.length * 5.6;
        if (x + w > card.x + card.w - 12) break;
        g.appendChild(_topoSvgEl('rect', {
          class: 'topo-ctr' + (container.running ? '' : ' topo-ctr-down'),
          x, y: y - 10, width: w, height: 14, rx: 5,
        }));
        _topoText(g, { class: 'topo-ctr-text', x: x + w / 2, y, 'text-anchor': 'middle' }, text);
        x += w + 4;
      }
      y += TOPO_STACK_ROW_H;
    }
  }
  return g;
}

/* ── Search, pan, zoom ───────────────────────────────────────────────────── */

function _topoMatches(host, q) {
  if (!host || !q) return false;
  return [host.hostname, host.primary_ip, ...(host.ips || []), ...(host.tags || [])]
    .filter(Boolean).join(' ').toLowerCase().includes(q);
}

function _topoHighlightMatch() {
  const svg = _topoSvg();
  const canvas = _topoEl('topo-canvas');
  if (!svg || !canvas || !topoState.q || !topoState.layout || !topoState.data) return;
  const hits = (topoState.data.hosts || [])
    .filter(host => topoState.layout.cards[host.id] && _topoMatches(host, topoState.q))
    .sort((a, b) => a.hostname.localeCompare(b.hostname, undefined, { sensitivity: 'base' }));
  if (!hits.length) return;
  const card = topoState.layout.cards[hits[0].id];
  _topoCentreOn(card);
  const g = svg.querySelector(`[data-topo-card="${CSS.escape(hits[0].id)}"]`);
  if (g) g.classList.add('topo-match');
}

/** Pan the current view so the given card sits in the middle of the canvas. */
function _topoCentreOn(card) {
  const canvas = _topoEl('topo-canvas');
  if (!canvas) return;
  const view = topoState.view;
  view.x = (canvas.clientWidth || 900) / 2 - (card.x + card.w / 2) * view.k;
  view.y = (canvas.clientHeight || 560) / 2 - (card.y + card.h / 2) * view.k;
  _topoApplyView();
}

/** The <g> the pan and zoom transform lives on. */
function _topoSvgRoot() {
  const svg = _topoSvg();
  return svg ? svg.children[0] : null;
}

function _topoApplyView() {
  const root = _topoSvgRoot();
  if (!root) return;
  const view = topoState.view;
  root.setAttribute('transform', `translate(${view.x.toFixed(1)},${view.y.toFixed(1)}) scale(${view.k})`);
}

function _topoWirePanZoom(svg) {
  let drag = null;
  svg.addEventListener('pointerdown', (ev) => {
    if (ev.target.closest('.topo-card')) return;
    drag = { x: ev.clientX, y: ev.clientY, vx: topoState.view.x, vy: topoState.view.y };
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    topoState.view.x = drag.vx + (ev.clientX - drag.x);
    topoState.view.y = drag.vy + (ev.clientY - drag.y);
    _topoApplyView();
  });
  const stop = (ev) => {
    if (!drag) return;
    drag = null;
    if (svg.hasPointerCapture && svg.hasPointerCapture(ev.pointerId)) svg.releasePointerCapture(ev.pointerId);
  };
  svg.addEventListener('pointerup', stop);
  svg.addEventListener('pointercancel', stop);
  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const box = svg.getBoundingClientRect();
    const px = ev.clientX - box.left, py = ev.clientY - box.top;
    const view = topoState.view;
    const k = Math.min(2.5, Math.max(0.25, view.k * (ev.deltaY < 0 ? 1.12 : 1 / 1.12)));
    view.x = px - (px - view.x) * (k / view.k);
    view.y = py - (py - view.y) * (k / view.k);
    view.k = k;
    _topoApplyView();
  }, { passive: false });
  svg.addEventListener('pointerover', (ev) => {
    const card = ev.target.closest('.topo-card');
    _topoDimLinks(card && card.dataset.topoLink ? card.dataset.topoLink : '');
  });
}

function _topoDimLinks(refId) {
  const svg = _topoSvg();
  if (!svg) return;
  svg.querySelectorAll('#topo-links .topo-link').forEach(path => {
    const touch = !!refId && (path.dataset.topoLinkFrom === refId || path.dataset.topoLinkTo === refId);
    const dim = !!refId && !touch;
    path.classList.toggle('topo-link-hi', touch);
    path.classList.toggle('topo-link-dim', dim);
    if (touch) _topoAnimate(path, 'strokeWidth', '1.5px', '2.5px', TOPO_FADE_MS);
    _topoAnimate(path, 'strokeOpacity', '0.7', dim ? '0.16' : (touch ? '1' : '0.7'), TOPO_FADE_MS);
  });
}

/** Fit the whole map into the canvas, centred, within the zoom clamp. The view
 *  is eased so the map travels to its new place instead of blinking there. */
function fitTopology() {
  const canvas = _topoEl('topo-canvas');
  const layout = topoState.layout;
  if (!canvas || !layout) return;
  const w = canvas.clientWidth || 900, h = canvas.clientHeight || 560;
  const k = Math.min(2.5, Math.max(0.25, Math.min((w - 32) / layout.width, (h - 32) / layout.height)));
  const from = { ...topoState.view };
  topoState.view = { x: (w - layout.width * k) / 2, y: (h - layout.height * k) / 2, k };
  const root = _topoSvgRoot();
  if (!root || typeof root.animate !== 'function') { _topoApplyView(); return; }
  const frames = [
    { transform: `translate(${from.x.toFixed(1)}px,${from.y.toFixed(1)}px) scale(${from.k})` },
    { transform: `translate(${topoState.view.x.toFixed(1)}px,${topoState.view.y.toFixed(1)}px) scale(${topoState.view.k})` },
  ];
  try {
    // fill:none — the transform attribute set below is what holds the map when it lands.
    const run = root.animate(frames, { duration: TOPO_PAN_MS, easing: 'ease-in-out', fill: 'none' });
    if (run && run.finished) run.finished.then(_topoApplyView).catch(() => {});
  } catch {
    /* Without element.animate the map is already where Fit put it. */
  }
  _topoApplyView();
}

/* ── Detail panel ────────────────────────────────────────────────────────── */

function _topoPanelList(title, items, renderItem) {
  if (!items.length) return `<div class="topo-p-label">${escHtml(title)}</div>`
    + '<div class="topo-p-none">None reported</div>';
  return `<div class="topo-p-label">${escHtml(title)}</div>`
    + `<div class="topo-p-rows">${items.map(renderItem).join('')}</div>`;
}

function _topoLinkRow(link, outgoing, otherId, name) {
  return `<button type="button" class="topo-p-link" data-topo-goto="${escAttr(otherId)}">`
    + `<span class="topo-p-dir">${outgoing ? '→' : '←'}</span>${escHtml(name)}`
    + `<span class="topo-p-meta">${link.count} session${link.count === 1 ? '' : 's'}`
    + `${(link.ports || []).length ? ` · ${escHtml(link.ports.join(', '))}` : ''}</span></button>`;
}

function _topoPeerName(other) {
  const peer = other.kind === 'device' ? _topoDevice(other.id) : _topoHost(other.id);
  if (!peer) return other.id;
  return peer.hostname || peer.name || other.id;
}

function openTopoHost(id) {
  const panel = _topoEl('topo-panel');
  const host = _topoHost(id);
  if (!panel || !host) return;
  topoState.selected = id;
  if (topoState.layout && topoState.layout.cards[id]) _topoCentreOn(topoState.layout.cards[id]);
  const fw = host.firewall;
  const links = ((topoState.data && topoState.data.links) || []).filter(link =>
    (link.source.kind === 'host' && link.source.id === host.id)
    || (link.target.kind === 'host' && link.target.id === host.id));
  const stacks = host.stacks || [];
  const out = [
    `<div class="topo-p-head"><span class="topo-p-dot" data-dot="${escAttr(host.status)}"></span>`
    + `<span class="topo-p-title">${escHtml(host.hostname)}</span>`
    + '<button type="button" class="topo-p-close" data-topo-close aria-label="Close details">×</button></div>',
    `<div class="topo-p-sub">${escHtml(host.os || 'Unknown OS')} · ${escHtml(host.status)}`
    + `${(host.tags || []).length ? ` · ${escHtml(host.tags.join(', '))}` : ''}</div>`,
    '<div class="card-actions topo-p-actions">'
    + `<button type="button" class="btn btn-sm btn-outline" data-topo-open="${escAttr(host.id)}">Open host</button>`
    + `<button type="button" class="btn btn-sm btn-outline" data-topo-containers="${escAttr(host.id)}">Containers</button></div>`,
    _topoPanelList('Addresses', host.ips || [], ip =>
      `<span class="topo-pill${ip === host.primary_ip ? ' topo-pill-key' : ''}">${escHtml(ip)}</span>`),
    _topoPanelList('Listening ports', host.listening || [], l =>
      `<span class="topo-pill${l.public ? ' topo-pill-key' : ''}">${escHtml(l.proto || 'tcp')} ${l.port}`
      + `${l.public ? ' · public' : ''}${l.process ? ` · ${escHtml(l.process)}` : ''}</span>`),
    '<div class="topo-p-label">Firewall</div>',
  ];
  if (fw) {
    out.push('<div class="topo-p-rows"><div class="topo-p-line">'
      + `${escHtml(fw.tool || 'firewall')} is ${fw.enabled ? 'on' : 'off'}`
      + `${fw.incoming ? `, default incoming ${escHtml(fw.incoming)}` : ''}</div>`
      + ((fw.allows || []).length
        ? `<div class="topo-p-chips">${fw.allows.map(port => `<span class="topo-pill">${port}</span>`).join('')}</div>`
        : '<div class="topo-p-none">No allowed ports reported</div>') + '</div>');
  } else {
    out.push('<div class="topo-p-none">No firewall reported. The 2026.14.4 agent collects it.</div>');
  }
  out.push(`<div class="topo-p-label">Stacks${stacks.length ? ` · ${_topoCount(host)} containers` : ''}</div>`);
  out.push(stacks.length
    ? stacks.map(stack => `<div class="topo-p-stack"><div class="topo-p-stack-name">${escHtml(stack.name || 'unnamed')}</div>`
      + (stack.containers || []).map(c => `<div class="topo-p-ctr">`
        + `<span class="topo-p-ctr-state" data-state="${escAttr(c.state)}"></span>${escHtml(c.name)} · ${escHtml(c.state)}`
        + `${(c.ports || []).length ? ` · ${escHtml(c.ports.join(' '))}` : ''}`
        + `${(c.networks || []).length ? ` · ${escHtml(c.networks.join(', '))}` : ''}</div>`).join('')
      + '</div>').join('')
    : '<div class="topo-p-none">No containers on this host</div>');
  out.push(_topoPanelList('Connections', links, link => {
    const outgoing = link.source.kind === 'host' && link.source.id === host.id;
    const other = outgoing ? link.target : link.source;
    return _topoLinkRow(link, outgoing, other.kind === 'device' ? `device:${other.id}` : other.id,
      _topoPeerName(other));
  }));
  const external = host.external || [];
  out.push(_topoPanelList('External destinations', external, e =>
    `<div class="topo-p-line"><span class="mono">${escHtml(e.ip)}</span> · ${e.count}`
    + ` session${e.count === 1 ? '' : 's'}`
    + `${(e.ports || []).length ? ` · ${escHtml(e.ports.join(', '))}` : ''}</div>`));
  out.push(`<div class="topo-p-foot">${host.network_at
    ? `Network data collected ${escHtml(timeAgo(host.network_at))}`
      + `${host.partial ? ' · reported incomplete' : ''}`
    : 'No network data yet. This host needs the 2026.14.4 agent.'}</div>`);
  panel.hidden = false;
  panel.innerHTML = out.join('');
  _topoMarkSelected();
}

function openTopoDevice(id) {
  const panel = _topoEl('topo-panel');
  const device = _topoDevice(id);
  if (!panel || !device) return;
  topoState.selected = `device:${id}`;
  const links = ((topoState.data && topoState.data.links) || []).filter(link =>
    (link.source.kind === 'device' && link.source.id === device.id)
    || (link.target.kind === 'device' && link.target.id === device.id));
  panel.hidden = false;
  panel.innerHTML = [
    `<div class="topo-p-head"><span class="topo-p-dot" data-dot="device"></span>`
    + `<span class="topo-p-title">${escHtml(device.name)}</span>`
    + '<button type="button" class="topo-p-close" data-topo-close aria-label="Close details">×</button></div>',
    `<div class="topo-p-sub">${escHtml(device.type || 'device')} · ${escHtml(device.ip)} · unmanaged</div>`,
    _topoPanelList('Connections', links, link => {
      const outgoing = link.source.kind === 'device' && link.source.id === device.id;
      const other = outgoing ? link.target : link.source;
      return _topoLinkRow(link, outgoing, other.id, _topoPeerName(other));
    }),
  ].join('');
  _topoMarkSelected();
}

function _topoMarkSelected() {
  const svg = _topoSvg();
  if (!svg) return;
  svg.querySelectorAll('.topo-card').forEach(g => {
    g.classList.toggle('topo-sel', g.dataset.topoCard === topoState.selected);
  });
}

function _topoClosePanel() {
  topoState.selected = null;
  const panel = _topoEl('topo-panel');
  if (panel) { panel.hidden = true; panel.innerHTML = ''; }
  _topoMarkSelected();
}

/* ── Loading, toolbar, wiring ────────────────────────────────────────────── */

function _topoStoredTags() {
  try {
    const parsed = JSON.parse(localStorage.getItem(TOPO_TAG_KEY) || '[]');
    return Array.isArray(parsed) ? parsed.filter(t => typeof t === 'string') : [];
  } catch {
    return [];
  }
}

function _topoSaveTags() {
  try {
    localStorage.setItem(TOPO_TAG_KEY, JSON.stringify([...topoState.tags]));
  } catch {
    /* A window with storage blocked keeps the tags for this visit only. */
  }
}

async function loadTopology() {
  const canvas = _topoEl('topo-canvas');
  let data;
  try {
    data = await apiJson('/api/v1/hosts/topology/');
  } catch (e) {
    if (canvas) canvas.innerHTML = `<div class="topo-error">${escHtml(e.message)}</div>`;
    return;
  }
  topoState.data = data;
  const known = new Set((data.tags || []).map(t => t.name));
  if (topoState.loadedOnce) {
    for (const tag of [...topoState.tags]) if (!known.has(tag)) topoState.tags.delete(tag);
  } else {
    topoState.loadedOnce = true;
    for (const tag of _topoStoredTags()) if (known.has(tag)) topoState.tags.add(tag);
  }
  if (topoState.site && !(data.sites || []).some(s => s.id === topoState.site)) topoState.site = '';
  _renderTopoToolbar();
  renderTopology();
  _topoBanner();
  if (topoState.selected && !topoState.layout.cards[topoState.selected]) _topoClosePanel();
  else if (topoState.selected) _topoMarkSelected();
}

function _renderTopoToolbar() {
  const data = topoState.data;
  const site = _topoEl('topo-site');
  if (site) {
    site.innerHTML = '<option value="">All sites</option>'
      + (data.sites || []).map(s => `<option value="${escAttr(s.id)}">${escHtml(s.name)}</option>`).join('');
    site.value = topoState.site;
    site.disabled = !(data.sites || []).length;
  }
  const tags = _topoEl('topo-tags');
  if (tags) {
    tags.innerHTML = (data.tags || []).length
      ? data.tags.map((t, i) =>
        `<button type="button" class="topo-tag" data-topo-tag="${escAttr(t.name)}"`
        + ` aria-pressed="${topoState.tags.has(t.name)}" data-topo-color="${i % TOPO_COLORS.length}">`
        + `${escHtml(t.name)}<span class="topo-tag-n">${t.count}</span></button>`).join('')
      : '<span class="topo-tags-none">No tags on your hosts</span>';
  }
  const count = _topoEl('topo-count');
  if (count) {
    count.textContent = `${data.hosts.length} hosts`
      + ` · ${data.hosts.filter(h => h.status === 'online').length} online`
      + ` · ${(data.links || []).length} connections`
      + `${(data.devices || []).length ? ` · ${data.devices.length} devices` : ''}`;
  }
  document.querySelectorAll('[data-topo-toggle]').forEach(btn => {
    btn.setAttribute('aria-pressed', String(!!topoState[btn.dataset.topoToggle]));
  });
}

function _topoBanner() {
  const banner = _topoEl('topo-banner');
  const data = topoState.data;
  if (!banner || !data) return;
  const missing = data.hosts.filter(h => !h.network_at).length;
  if (!missing || !data.hosts.length) {
    banner.hidden = true;
    banner.textContent = '';
    return;
  }
  banner.hidden = false;
  banner.textContent = `${missing} of ${data.hosts.length} hosts have not sent network data yet. `
    + 'They need the 2026.14.4 agent.';
}

delegateClick('[data-topo-tag]', (el) => {
  const tag = el.dataset.topoTag;
  if (topoState.tags.has(tag)) topoState.tags.delete(tag); else topoState.tags.add(tag);
  _topoSaveTags();
  el.setAttribute('aria-pressed', String(topoState.tags.has(tag)));
  renderTopology();
});

delegateClick('[data-topo-toggle]', (el) => {
  const key = el.dataset.topoToggle;
  topoState[key] = !topoState[key];
  el.setAttribute('aria-pressed', String(topoState[key]));
  renderTopology();
});

delegateClick('[data-topo-card]', (el) => {
  const key = el.dataset.topoCard;
  if (!key) return;
  if (key.startsWith('device:')) openTopoDevice(key.slice(7));
  else openTopoHost(key);
});

delegateClick('[data-topo-close]', _topoClosePanel);

delegateClick('[data-topo-goto]', (el) => {
  const key = el.dataset.topoGoto;
  if (key.startsWith('device:')) openTopoDevice(key.slice(7));
  else openTopoHost(key);
});

delegateClick('[data-topo-open]', (el) => {
  const id = el.dataset.topoOpen;
  const real = document.querySelector(`.host-card[data-id="${CSS.escape(id)}"]`);
  if (real && typeof openHostDetail === 'function') { openHostDetail(real); return; }
  showToast('That host has no card on the dashboard right now', 'error');
});

delegateClick('[data-topo-containers]', (el) => {
  navigateTo('containers');
  if (typeof selectContainersHost === 'function') selectContainersHost(el.dataset.topoContainers);
});

delegateClick('#topo-fit', () => fitTopology());
delegateClick('#topo-refresh', () => loadTopology());

document.addEventListener('change', (ev) => {
  if (ev.target.id !== 'topo-site') return;
  topoState.site = ev.target.value;
  topoState.view = { x: 0, y: 0, k: 1 };
  renderTopology();
  fitTopology();
});

document.addEventListener('input', (ev) => {
  if (ev.target.id !== 'topo-q') return;
  clearTimeout(topoState.timer);
  const q = ev.target.value.trim().toLowerCase();
  topoState.timer = setTimeout(() => {
    topoState.q = q;
    _topoHighlightMatch();
  }, 200);
});

/* The page fetches its map the first time it is shown. */
(function () {
  const previous = window.navigateTo;
  if (typeof previous !== 'function') return;
  window.navigateTo = function (pageName) {
    previous.apply(this, arguments);
    if (pageName !== 'topology') return;
    if (!topoState.data) loadTopology();
    else renderTopology();
    setTimeout(() => fitTopology(), 0);
  };
})();
