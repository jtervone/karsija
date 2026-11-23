// Unit tests of the compare view's shared viewport and pane selection: run by tests/test_ui_js.py (`node --test`).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { bestFirst, comparableGroup, panView, paneLayout, viewAt, windowStart } from '../../karsija/static/compare.js';
import { comparePosition } from '../../karsija/static/event.js';
import { layout } from '../../karsija/static/loupe.js';
import { Order } from '../../karsija/static/review.js';

const PANE = { w: 800, h: 400 };
const IMG = { w: 2560, h: 1707 };

const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

/** The image point (as fractions of the image) at a stage point. */
function pointAt(stage, img, view, p, dpr = 1) {
  const { s, x, y } = paneLayout(stage, img, view, dpr);
  return { fx: (p.x - x) / (img.w * s), fy: (p.y - y) / (img.h * s) };
}

test('fitted until zoomed, as in the loupe', () => {
  assert.deepEqual(paneLayout(PANE, IMG, null), layout(PANE, IMG, null));
});

test('100 % on one pane: every pane of a burst shows the same region', () => {
  const point = { x: 600, y: 120 };
  const fit = layout(PANE, IMG, null);
  const under = { fx: (point.x - fit.x) / (IMG.w * fit.s), fy: (point.y - fit.y) / (IMG.h * fit.s) };
  const view = viewAt(PANE, IMG, point);
  const z = paneLayout(PANE, IMG, view);
  assert.equal(z.s, 1);
  assert.ok(z.zoomed);
  const at = pointAt(PANE, IMG, view, point);        // the spot clicked stays under the pointer
  near(at.fx, under.fx);
  near(at.fy, under.fy);
  // another shot of the burst, in another pane of the same size: the same layout, so the same region
  assert.deepEqual(paneLayout(PANE, { ...IMG }, view), z);
});

test('the same spot at the same size where the previews differ in size, shape or orientation', () => {
  const view = viewAt(PANE, IMG, { x: 300, y: 250 });
  const centre = { x: PANE.w / 2, y: PANE.h / 2 };
  const a = pointAt(PANE, IMG, view, centre);
  const small = { w: 1920, h: 1280 };                  // a RAW's embedded preview of the same frame
  const b = pointAt(PANE, small, view, centre);
  near(a.fx, b.fx);
  near(a.fy, b.fy);
  // the long edge is as long on screen in both: the scene is enlarged as much
  near(paneLayout(PANE, small, view).s * 1920, paneLayout(PANE, IMG, view).s * 2560);
  const portrait = { w: 1707, h: 2560 };
  const p = pointAt(PANE, portrait, view, centre);
  near(p.fx, a.fx);
  near(p.fy, a.fy);
  assert.equal(paneLayout(PANE, portrait, view).s, 1);
});

test('a view that would not enlarge a pane leaves it fitted', () => {
  const view = { cx: 0.5, cy: 0.5, edge: 100 };
  assert.deepEqual(paneLayout(PANE, IMG, view), layout(PANE, IMG, null));
  assert.equal(viewAt({ w: 3000, h: 2000 }, IMG, null), null);       // already whole at full size
});

test('panning one pane pans the view, as far as the image goes', () => {
  const view = viewAt(PANE, IMG, null);
  near(view.cx, 0.5);
  const moved = panView(PANE, IMG, view, -100, 50);
  near(moved.cx, 0.5 + 100 / 2560);
  near(moved.cy, 0.5 - 50 / 1707);
  assert.equal(moved.edge, view.edge);
  const edge = panView(PANE, IMG, view, 1e5, 1e5);    // dragged far: the corner, no gap
  const z = paneLayout(PANE, IMG, edge);
  near(z.x, 0);
  near(z.y, 0);
  // from a clamped view, dragging back moves at once
  near(panView(PANE, IMG, edge, -10, 0).cx, (400 + 10) / 2560);
});

test('best first: the best, then the sharpest, unanalysed last', () => {
  const shots = new Map([
    [1, { sharp: 50, auto_verdict: 'similar' }],
    [2, { sharp: 0, error: 'rikki', auto_verdict: 'error' }],
    [3, { sharp: 90, auto_verdict: 'best' }],
    [4, { sharp: 70, auto_verdict: 'similar' }],
    [5, { sharp: 70, auto_verdict: 'keep' }],
  ]);
  assert.deepEqual(bestFirst([1, 2, 3, 4, 5], shots), [3, 4, 5, 1, 2]);
});

test('the panes follow the selection only as far as it needs', () => {
  assert.equal(windowStart(3, 2, 0), 0);
  assert.equal(windowStart(9, 3, 0), 0);
  assert.equal(windowStart(9, 4, 0), 1);
  assert.equal(windowStart(9, 8, 1), 5);
  assert.equal(windowStart(9, 6, 5), 5);          // still in view: nothing moves
  assert.equal(windowStart(9, 4, 5), 4);
  assert.equal(windowStart(9, 0, 5), 0);
  assert.equal(windowStart(9, 8, 7), 5);          // never past the end
});

test('the next and previous group with something to compare', () => {
  const g = (key, groupId, ids) => ({ key, groupId, ids, members: ids });
  const order = new Order([g('g1', 1, [1, 2]), g('s3', null, [3]), g('g2', 2, [4]), g('g3', 3, [5, 6, 7])]);
  assert.equal(comparableGroup(order, 1, 1).groupId, 3);
  assert.equal(comparableGroup(order, 6, -1).groupId, 1);
  assert.equal(comparableGroup(order, 6, 1), null);
  assert.equal(comparableGroup(order, 1, -1), null);
});

test('where the compare view is', () => {
  const g = (key, groupId, ids, members = ids) => ({ key, groupId, ids, members });
  const order = new Order([g('g1', 1, [1, 2]), g('s3', null, [3]), g('g3', 3, [5, 6], [4, 5, 6])]);
  assert.equal(comparePosition(order, 6), 'Ryhmä 2 / 2 · 2 kuvaa, paras ensin, 1 piilossa');
  assert.equal(comparePosition(order, 1, { candidates: true }),
    'Ryhmä 1 / 2 · 2 kuvaa, paras ensin · suodatettu: vain ehdokkaat');
  assert.equal(comparePosition(new Order([g('g1', 1, [1], [1, 2])]), 1), 'Ryhmä · 1 kuva, 1 piilossa');
});
