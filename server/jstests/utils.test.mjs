// Pure helpers in vigil-utils.js: escaping, versions, sizes, rates, the
// terminal renderer and the declarative-handler arguments.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const w = load('vigil-utils.js');

test('escAttr escapes everything that can leave an attribute', () => {
  assert.equal(w.escAttr(`"'<>&`), '&quot;&#39;&lt;&gt;&amp;');
  assert.equal(w.escAttr(null), '');
  assert.equal(w.escAttr(5), '5');
});

test('escHtml escapes text but not quotes (use escAttr inside attributes)', () => {
  assert.equal(w.escHtml('<script>&'), '&lt;script&gt;&amp;');
  assert.equal(w.escHtml('"q"'), '"q"');
});

test('versions compare numerically and unknown never warns', () => {
  assert.deepEqual([...w.parseVersion('v2026.16.0')], [2026, 16, 0]);
  assert.equal(w.parseVersion('dev'), null);
  assert.equal(w.isOlderVersion('2026.9.1', '2026.10.0'), true);   // 9 < 10, not string order
  assert.equal(w.isOlderVersion('2026.16.1', '2026.16'), false);   // newer agent is not outdated
  assert.equal(w.isOlderVersion('2026.16', '2026.16.0'), false);   // missing parts are zero
  assert.equal(w.isOlderVersion('garbage', '2026.16.0'), false);
  assert.equal(w.isOlderVersion(null, '1'), false);
});

test('formatBytes and the per-second formatter', () => {
  assert.equal(w.formatBytes(0), '0 B');
  assert.equal(w.formatBytes(1536), '1.5 KB');
  assert.equal(w.formatBytes(1024 ** 3), '1 GB');
  assert.equal(w._formatBytesPerSec(-5), '0 B/s');
  assert.equal(w._formatBytesPerSec(Infinity), '0 B/s');
  assert.equal(w._formatBytesPerSec(2048), '2.00 KB/s');
  assert.equal(w._formatBytesPerSec(150 * 1024), '150 KB/s');
});

test('computeRates turns counters into per-second rates and never goes negative', () => {
  const pts = [
    { time: '2026-11-04T10:00:00Z', value: 100 },
    { time: '2026-11-04T10:00:10Z', value: 200 },
    { time: '2026-11-04T10:00:10Z', value: 250 },   // same instant: skipped
    { time: '2026-11-04T10:00:20Z', value: 50 },    // counter reset: 0, not negative
  ];
  const rates = w.computeRates(pts);
  assert.equal(rates.length, 2);
  assert.equal(rates[0].y, 10);
  assert.equal(rates[0].x, Date.parse('2026-11-04T10:00:10Z'));
  assert.equal(rates[1].y, 0);
});

test('groupByLabel buckets points, with a default for unlabelled ones', () => {
  const g = w.groupByLabel([{ labels: { dev: 'eth0' } }, { labels: {} }, {}], 'dev');
  assert.deepEqual(Object.keys(g).sort(), ['_default', 'eth0']);
  assert.equal(g._default.length, 2);
});

test('timeAgo reads skew as now and falls back to a date after a month', () => {
  const ago = (s) => new Date(Date.now() - s * 1000).toISOString();
  assert.equal(w.timeAgo(''), '');
  assert.equal(w.timeAgo('not a date'), '');
  assert.equal(w.timeAgo(new Date(Date.now() + 60000).toISOString()), 'just now');
  assert.equal(w.timeAgo(ago(30)), '30s ago');
  assert.equal(w.timeAgo(ago(5 * 60)), '5m ago');
  assert.equal(w.timeAgo(ago(3 * 3600)), '3h ago');
  assert.equal(w.timeAgo(ago(2 * 86400)), '2d ago');
  assert.ok(!w.timeAgo(ago(60 * 86400)).endsWith('ago'));
});

test('_actArg reads tokens and numbers and leaves the rest as strings', () => {
  const el = { checked: 1, value: 'v' };
  assert.equal(w._actArg('$checked', el), true);
  assert.equal(w._actArg('$value', el), 'v');
  assert.equal(w._actArg('$el', el), el);
  assert.equal(w._actArg('42', el), 42);
  assert.equal(w._actArg('-1.5', el), -1.5);
  assert.equal(w._actArg('1e3', el), '1e3');      // not a plain number: stays a string
  assert.equal(w._actArg('yaml', el), 'yaml');
  assert.equal(w._actArg('constructor', el), 'constructor');  // never reaches the prototype
  assert.equal(w._actArg(undefined, el), undefined);
});

test('termHtml colours ANSI, escapes markup and drops control bytes', () => {
  assert.equal(w.termHtml('\x1b[31mred\x1b[0m plain <b>'), '<span class="term-r">red</span> plain &lt;b&gt;');
  assert.equal(w.termPlain('a\x1b[32mb\x1b[0mc\x07'), 'abc');
  assert.equal(w.termHtml('50%\r100%'), '100%');                 // carriage return overwrites
  assert.ok(!w.termHtml('\x1b]0;title\x07x').includes('title'));  // OSC title sequences vanish
});

test('yamlToHtml highlights and escapes', () => {
  const html = w.yamlToHtml('name: <x>\n# c\n- a');
  assert.ok(html.includes('<span class="y-key">name</span>'));
  assert.ok(html.includes('&lt;x&gt;'));
  assert.ok(!html.includes('<x>'));
  assert.ok(html.includes('<span class="y-comment"># c</span>'));
});
