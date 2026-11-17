// The WAI-ARIA tabs pattern vigil-nav.js applies to every .tab-bar: the roles
// and states screen readers announce, and the keys that move between tabs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { load } from './harness.mjs';

function tabBar() {
  const w = load('vigil-nav.js');
  const { StubElement, document } = w;
  const page = new StubElement('div');
  page.classList.add('page');
  const bar = new StubElement('div');
  bar.classList.add('tab-bar');
  page.appendChild(bar);
  const tabs = ['one', 'two', 'three'].map((name, i) => {
    const tab = new StubElement('div');
    tab.classList.add('tab');
    tab.dataset.tab = name;
    if (i === 0) tab.classList.add('active');
    bar.appendChild(tab);
    const panel = new StubElement('div');
    panel.id = name;
    panel.classList.add('tab-content');
    if (i === 0) panel.classList.add('active');
    page.appendChild(panel);
    return tab;
  });
  document.body.appendChild(page);
  w.initTabBar(bar);
  const panels = () => ['one', 'two', 'three'].map(n => document.getElementById(n));
  // Where the keyboard is: the tab a bar leaves focusable is where a real
  // browser sends the next key.
  const press = (k) => {
    const target = tabs.find(t => t.getAttribute('tabindex') === '0');
    const ev = new w.KeyboardEvent('keydown', { key: k });
    target.dispatchEvent(ev);
    return ev;
  };
  return { w, bar, tabs, panels, press };
}

test('a tab bar is a tablist of selectable tabs wired to its panels', () => {
  const { bar, tabs, panels } = tabBar();
  assert.equal(bar.getAttribute('role'), 'tablist');
  assert.deepEqual(tabs.map(t => t.getAttribute('role')), ['tab', 'tab', 'tab']);
  assert.deepEqual(tabs.map(t => t.getAttribute('aria-selected')), ['true', 'false', 'false']);
  assert.deepEqual(tabs.map(t => t.getAttribute('tabindex')), ['0', '-1', '-1']);
  assert.deepEqual(tabs.map(t => t.getAttribute('aria-controls')), ['one', 'two', 'three']);
  assert.deepEqual(panels().map(p => p.getAttribute('role')), ['tabpanel', 'tabpanel', 'tabpanel']);
  assert.deepEqual(panels().map(p => p.getAttribute('aria-labelledby')), ['tab-one', 'tab-two', 'tab-three']);
});

test('an arrow key activates the next tab, moving its states with it', () => {
  const { tabs, panels, press } = tabBar();
  press('ArrowRight');
  assert.ok(tabs[1].classList.contains('active'));
  assert.equal(tabs[1].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[1].getAttribute('tabindex'), '0');
  assert.equal(tabs[0].getAttribute('aria-selected'), 'false');
  assert.equal(tabs[0].getAttribute('tabindex'), '-1');
  assert.ok(!panels()[0].classList.contains('active'));
  assert.ok(panels()[1].classList.contains('active'));
});

test('ArrowRight from the last tab wraps to the first, and ArrowLeft wraps back', () => {
  const { tabs, panels, press } = tabBar();
  press('End');
  press('ArrowRight');
  assert.ok(tabs[0].classList.contains('active'));
  assert.equal(tabs[0].getAttribute('aria-selected'), 'true');
  assert.deepEqual(panels().map(p => p.classList.contains('active')), [true, false, false]);

  press('ArrowLeft');
  assert.ok(tabs[2].classList.contains('active'));
  assert.equal(tabs[2].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[1].getAttribute('aria-selected'), 'false');
});

test('Home and End jump to the first and last tab', () => {
  const { tabs, press } = tabBar();
  press('End');
  assert.ok(tabs[2].classList.contains('active'));
  assert.equal(tabs[2].getAttribute('aria-selected'), 'true');
  assert.equal(tabs[1].getAttribute('tabindex'), '-1');
  press('Home');
  assert.ok(tabs[0].classList.contains('active'));
  assert.equal(tabs[0].getAttribute('tabindex'), '0');
});

test('the keyboard moves the focus to the tab it activated', () => {
  const { tabs, panels, press } = tabBar();
  press('ArrowRight');
  assert.ok(panels()[1].classList.contains('active'));      // selection moved …
  assert.equal(tabs[1].getAttribute('tabindex'), '0');       // … and so did the focus
  assert.equal(tabs.filter(t => t.getAttribute('tabindex') === '0').length, 1);

  press('End');
  assert.equal(tabs[2].getAttribute('tabindex'), '0');
  assert.ok(tabs[2].classList.contains('active'));
});

test('Space activates the focused tab without scrolling the page', () => {
  const { tabs, panels, press } = tabBar();
  press('ArrowRight');
  const ev = press(' ');
  assert.equal(ev.defaultPrevented, true);
  assert.ok(tabs[1].classList.contains('active'));
  assert.ok(panels()[1].classList.contains('active'));
});

test('Enter activates the focused tab', () => {
  const { tabs, panels, press } = tabBar();
  press('ArrowRight');
  press('Enter');
  assert.ok(tabs[1].classList.contains('active'));
  assert.ok(panels()[1].classList.contains('active'));
});

test('a letter key does nothing and is not swallowed', () => {
  const { tabs, press } = tabBar();
  const ev = press('x');
  assert.equal(ev.defaultPrevented, false);
  assert.ok(!tabs[1].classList.contains('active'));
  assert.ok(tabs[0].classList.contains('active'));
});

test('moving with the keyboard clicks the tab, so other scripts still react', () => {
  const { tabs, press } = tabBar();                  // vigil-tasks.js listens for these
  let seen = 0;
  tabs[1].addEventListener('click', () => { seen += 1; });
  press('ArrowRight');
  assert.equal(seen, 1);
});

test('initTabBar twice on one bar adds a single set of listeners', () => {
  const { w, bar, tabs, press } = tabBar();
  w.initTabBar(bar);
  const ev = press('ArrowRight');
  assert.equal(ev.defaultPrevented, true);
  assert.ok(tabs[1].classList.contains('active'));
});
