// bandSeries: the min-max band a bucketed metric series (phase 02a) draws
// behind its average line.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const w = load('vigil-utils.js');
const bandSeries = w.$('bandSeries');
// Objects built inside the vm context are not this realm's, so compare by value.
const plain = (v) => JSON.parse(JSON.stringify(v));

const T0 = Date.parse('2026-11-14T10:00:00Z');
const HOUR = 3600 * 1000;

test('a raw series carries no min or max, so there is no band', () => {
  assert.equal(bandSeries([{ time: '2026-11-14T10:00:00Z', value: 3 }]), null);
  assert.equal(bandSeries([]), null);
  assert.equal(bandSeries(null), null);
});

test('bucketed points come back oldest first with maxima on top', () => {
  const band = bandSeries([
    { time: new Date(T0 + HOUR).toISOString(), value: 20, min: 10, max: 90 },
    { time: new Date(T0).toISOString(), value: 30, min: 5, max: 40 },
  ]);
  assert.deepEqual(plain(band.high), [{ x: T0, y: 40 }, { x: T0 + HOUR, y: 90 }]);
  assert.deepEqual(plain(band.low), [{ x: T0, y: 5 }, { x: T0 + HOUR, y: 10 }]);
});

test('a bucket with no minimum of its own is left out', () => {
  const band = bandSeries([
    { time: new Date(T0).toISOString(), value: 20, min: 10, max: 90 },
    { time: new Date(T0 + HOUR).toISOString(), value: 25, min: null, max: 80 },
  ]);
  assert.deepEqual(plain(band.high), [{ x: T0, y: 90 }]);
  assert.deepEqual(plain(band.low), [{ x: T0, y: 10 }]);
});
