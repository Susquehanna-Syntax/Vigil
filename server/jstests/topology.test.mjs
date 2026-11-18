// Polled devices on the topology map (S9, phase 07). Which site a device sits
// in, what its dot says, and when a dotted poller line is drawn — all of it
// decided by pure helpers here, so the map's drawing stays a render of data.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

const w = load('vigil-utils.js', 'vigil-topology.js');

const SITE = (id, name) => ({ id, name });
const HOST = (id, hostname, siteId = null) => ({
  id, hostname, status: 'online', site_id: siteId, tags: [], ips: [], primary_ip: '',
  listening: [], firewall: null, stacks: [], external: [], network_at: null, partial: false,
});
const DEVICE = (id, name, extra) => ({
  id, name, type: 'switch', ip: '10.0.0.50', site_id: null, check_state: null, poller_id: null,
  ...extra,
});
const LINK = (from, to) => ({ source: from, target: to, ports: [161], count: 1 });

test('a device with a site of its own sits there, wherever its traffic goes', () => {
  const data = {
    sites: [SITE('s1', 'North'), SITE('s2', 'South')],
    hosts: [HOST('h1', 'n-1', 's1')],
    devices: [DEVICE('d1', 'sw-1', { site_id: 's2' })],
    // Its only traffic is to a host in s1 — the vote would put it there.
    links: [LINK({ kind: 'host', id: 'h1' }, { kind: 'device', id: 'd1' })],
  };
  assert.equal(w._topoDeviceSites(data).get('d1'), 's2');
});

test('a device with no site follows the host it talks to', () => {
  const data = {
    sites: [SITE('s1', 'North'), SITE('s2', 'South')],
    hosts: [HOST('h1', 'n-1', 's1'), HOST('h2', 's-1', 's2')],
    devices: [DEVICE('d1', 'sw-1'), DEVICE('d2', 'sw-2')],
    links: [
      LINK({ kind: 'host', id: 'h1' }, { kind: 'device', id: 'd1' }),
      LINK({ kind: 'host', id: 'h2' }, { kind: 'device', id: 'd2' }),
    ],
  };
  const sites = w._topoDeviceSites(data);
  assert.equal(sites.get('d1'), 's1');
  assert.equal(sites.get('d2'), 's2');
});

test('the dot says what the check says, and blue when nobody checks', () => {
  assert.equal(w._topoDeviceDot(DEVICE('d', 'a', { check_state: 'up' })), 'online');
  assert.equal(w._topoDeviceDot(DEVICE('d', 'a', { check_state: 'down' })), 'offline');
  assert.equal(w._topoDeviceDot(DEVICE('d', 'a', { check_state: null, poller_id: 'h1' })), 'device');
  assert.equal(w._topoDeviceDot(DEVICE('d', 'a')), 'device');
});

test('the panel words: up, down, no results yet, not polled', () => {
  assert.equal(w._topoDeviceStateText(DEVICE('d', 'a', { check_state: 'up' })), 'up');
  assert.equal(w._topoDeviceStateText(DEVICE('d', 'a', { check_state: 'down' })), 'down');
  assert.equal(w._topoDeviceStateText(DEVICE('d', 'a', { check_state: null, poller_id: 'h1' })),
    'no results yet');
  assert.equal(w._topoDeviceStateText(DEVICE('d', 'a')), 'not polled');
});

const CARDS = { h1: { x: 0, y: 0, w: 200, h: 96 }, 'device:d1': { x: 400, y: 300, w: 150, h: 56 } };

test('a polled device gets one dotted line to its poller', () => {
  const data = {
    hosts: [HOST('h1', 'p-1')],
    devices: [DEVICE('d1', 'sw-1', { poller_id: 'h1' })],
    links: [],
  };
  const links = w._topoLinkGeometry(data, CARDS);
  assert.equal(links.length, 1);
  assert.equal(links[0].poll, true);
  assert.equal(links[0].fromId, 'h1');
  assert.equal(links[0].toId, 'device:d1');
});

test('a traffic line to the poller is enough: no dotted one on top of it', () => {
  const data = {
    hosts: [HOST('h1', 'p-1')],
    devices: [DEVICE('d1', 'sw-1', { poller_id: 'h1' })],
    links: [LINK({ kind: 'host', id: 'h1' }, { kind: 'device', id: 'd1' })],
  };
  const links = w._topoLinkGeometry(data, CARDS);
  assert.equal(links.length, 1);
  assert.notEqual(links[0].poll, true);
});

test('a device whose poller is not on the map gets no line', () => {
  const data = {
    hosts: [],
    devices: [DEVICE('d1', 'sw-1', { poller_id: 'gone' })],
    links: [],
  };
  assert.equal(w._topoLinkGeometry(data, CARDS).length, 0);
});
