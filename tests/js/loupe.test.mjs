// Unit tests of the loupe's zoom geometry and metadata texts: run by tests/test_ui_js.py (`node --test`).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { position } from '../../karsija/static/event.js';
import { clampZoom, fmtAperture, fmtExposure, fmtFocal, fmtIso, fmtShare, layout, pan, zoomAt }
  from '../../karsija/static/loupe.js';
import { Order } from '../../karsija/static/review.js';

const STAGE = { w: 1000, h: 500 };
const IMG = { w: 2560, h: 1707 };

const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-6, `${a} != ${b}`);

test('fitted: the whole image, centred, never enlarged', () => {
  const fit = layout(STAGE, IMG, null);
  near(fit.s, 500 / 1707);
  near(fit.y, 0);
  near(fit.x, (1000 - 2560 * fit.s) / 2);
  assert.deepEqual(layout(STAGE, { w: 400, h: 200 }, null), { s: 1, x: 300, y: 150 });
  assert.equal(layout({ w: 4000, h: 3000 }, IMG, null, 2).s, 0.5);     // 100 % on a 2× screen at most
});

test('100 %: the point under the pointer stays there', () => {
  const fit = layout(STAGE, IMG, null);
  const point = { x: 600, y: 200 };
  const image = { x: (point.x - fit.x) / fit.s, y: (point.y - fit.y) / fit.s };       // image pixel under it
  const z = layout(STAGE, IMG, zoomAt(STAGE, IMG, point));
  assert.equal(z.s, 1);
  near(z.x + image.x, point.x);
  near(z.y + image.y, point.y);
});

test('100 % on a 2× screen: one image pixel per screen pixel', () => {
  const z = layout(STAGE, IMG, zoomAt(STAGE, IMG, null, 2), 2);
  assert.equal(z.s, 0.5);
  near(z.x, (1000 - 1280) / 2);         // around the centre
});

test('100 % near an edge leaves no gap; an axis that fits is centred', () => {
  const z = layout(STAGE, IMG, zoomAt(STAGE, IMG, { x: 0, y: 0 }));
  assert.deepEqual([z.x, z.y], [0, 0]);
  const tall = { w: 800, h: 3000 };
  const t = layout(STAGE, tall, zoomAt(STAGE, tall, { x: 1000, y: 500 }));
  assert.deepEqual([t.x, t.y], [100, 500 - 3000]);
  assert.deepEqual(clampZoom(STAGE, IMG, { cx: -5, cy: 9 }), { cx: 500 / 2560, cy: 1 - 250 / 1707 });
});

test('panning follows the pointer and stops at the edges', () => {
  const from = zoomAt(STAGE, IMG, null);
  const before = layout(STAGE, IMG, from);
  const after = layout(STAGE, IMG, pan(STAGE, IMG, from, 30, -20));
  near(after.x - before.x, 30);
  near(after.y - before.y, -20);
  const far = layout(STAGE, IMG, pan(STAGE, IMG, from, 1e6, 1e6));
  assert.deepEqual([far.x, far.y], [0, 0]);
  // a zoom left out of range (the window grew) starts from where the image actually is: no dead zone
  const out = layout(STAGE, IMG, pan(STAGE, IMG, { cx: -5, cy: 0.5 }, -10, 0));
  near(out.x, -10);
});

test('metadata texts', () => {
  assert.equal(fmtExposure(1 / 250), '1/250 s');
  assert.equal(fmtExposure(0.3333), '1/3 s');
  assert.equal(fmtExposure(0.4), '0,4 s');
  assert.equal(fmtExposure(2), '2 s');
  assert.equal(fmtExposure(undefined), null);
  assert.equal(fmtAperture(2.8), 'f/2,8');
  assert.equal(fmtAperture(8), 'f/8');
  assert.equal(fmtIso(6400), `ISO ${new Intl.NumberFormat('fi-FI').format(6400)}`);
  assert.equal(fmtFocal(50), '50 mm');
  assert.equal(fmtFocal(50, 75), '50 mm (kinovastaava 75 mm)');
  assert.equal(fmtFocal(50, 50), '50 mm');
  assert.equal(fmtFocal(null, 75), null);
  assert.equal(fmtShare(0), '0 %');
  assert.equal(fmtShare(0.0001), '< 0,1 %');
  assert.equal(fmtShare(0.125), '12,5 %');
});

test('position among the shown shots', () => {
  const order = new Order([
    { key: 'g1', groupId: 1, members: [1, 2, 3], ids: [1, 3] },
    { key: 's4', groupId: null, members: [4], ids: [4] },
  ]);
  assert.equal(position(order, 3), 'Kuva 2 / 3 · ryhmässä 2 / 2 (1 piilossa)');
  assert.equal(position(order, 4), 'Kuva 3 / 3');
});
