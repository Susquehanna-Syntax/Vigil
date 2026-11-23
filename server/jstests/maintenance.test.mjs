// The maintenance windows pane's pure formatting: what a row says about when
// a window runs and which hosts it covers.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const w = load('vigil-utils.js', 'vigil-maintenance.js');

test('a one-off row reads as the stretch between its two ends', () => {
  const text = w._maintSchedule({
    kind: 'once',
    starts_at: '2026-11-20T02:00:00Z',
    ends_at: '2026-11-20T05:00:00Z',
  });
  const at = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  assert.equal(text, `${at('2026-11-20T02:00:00Z')} to ${at('2026-11-20T05:00:00Z')}`);
  assert.doesNotMatch(text, /:00:00/, 'no seconds');
});

test('a weekly row lists the days in Monday-first order, with the zone', () => {
  assert.equal(w._maintSchedule({
    kind: 'weekly', weekdays: [4, 0, 2], start_time: '22:00',
    duration_minutes: 180, timezone: 'America/New_York',
  }), 'Mon, Wed, Fri at 22:00 for 3 h (America/New_York)');
});

test('weekly durations under an hour, whole hours, and a part hour', () => {
  const week = (minutes) => w._maintSchedule({
    kind: 'weekly', weekdays: [0], start_time: '01:00',
    duration_minutes: minutes, timezone: 'UTC',
  });
  assert.equal(week(45), 'Mon at 01:00 for 45 min (UTC)');
  assert.equal(week(120), 'Mon at 01:00 for 2 h (UTC)');
  assert.equal(week(150), 'Mon at 01:00 for 2 h 30 min (UTC)');
});

test('a window naming nothing covers its site, or every site when global', () => {
  assert.equal(w._maintCovers({ host_ids: [], tags: [], site_id: 's1' }), 'Every host in its site');
  assert.equal(w._maintCovers({ host_ids: [], tags: [], site_id: null }), 'Every host in every site');
  assert.equal(w._maintCovers({}), 'Every host in every site');
});

test('hosts and tags each drop out when empty, and one host is singular', () => {
  assert.equal(w._maintCovers({ host_ids: ['a'], tags: [] }), '1 host');
  assert.equal(w._maintCovers({ host_ids: ['a', 'b', 'c'], tags: ['db', 'edge'] }),
    '3 hosts, tags: db, edge');
  assert.equal(w._maintCovers({ host_ids: [], tags: ['db'] }), 'tags: db');
});
