// Unit tests of the Asetukset panel's calculations: run by tests/test_ui_js.py (`node --test`).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { axisEnd, blurLimit, changes, histogram, median, position, ticks, valueAt } from '../../karsija/static/settings.js';

const DEFAULTS = { gap: 3, hamming: 40, abs_blur: 10, rel_blur: 0.25, sim_keep: 0 };

test('the median is that of numpy', () => {
  assert.equal(median([]), null);
  assert.equal(median([5, 1, 3]), 3);
  assert.equal(median([4, 1, 3, 2]), 2.5);
});

test('the blur limit is the larger of abs_blur and rel_blur × median, as the server judges', () => {
  assert.equal(blurLimit(DEFAULTS, 100), 25);
  assert.equal(blurLimit(DEFAULTS, 20), 10);
  assert.equal(blurLimit(DEFAULTS, null), 10);
});

test('the axis is logarithmic from 0 and reaches past the sharpest shot and the thresholds', () => {
  assert.equal(position(0, 999), 0);
  assert.equal(position(999, 999), 1);
  assert.ok(Math.abs(position(31.62, 999) - 0.5) < 0.01);
  assert.equal(position(5000, 999), 1);                    // clamped
  assert.ok(Math.abs(valueAt(position(42, 999), 999) - 42) < 1e-9);
  assert.equal(axisEnd([3, 120, 840], DEFAULTS, 120), 900);
  assert.equal(axisEnd([3, 4], { ...DEFAULTS, abs_blur: 55 }, 3.5), 60);
  assert.equal(axisEnd([], DEFAULTS, null), 10);
});

test('the histogram counts every value once', () => {
  const values = [0, 1, 2, 10, 50, 100, 400, 999];
  const bins = histogram(values, 999, 6);
  assert.equal(bins.length, 6);
  assert.equal(bins.reduce((n, b) => n + b.count, 0), values.length);
  assert.equal(bins[0].lo, 0);
  assert.ok(Math.abs(bins.at(-1).hi - 999) < 1e-9);
  assert.equal(bins.at(-1).count, 2);                     // 400 and 999 (the top edge goes into the last bin)
});

test('only the changed settings are sent', () => {
  assert.deepEqual(changes({ ...DEFAULTS, gap: 1 }, DEFAULTS), { gap: 1 });
  assert.deepEqual(changes(DEFAULTS, DEFAULTS), {});
});

test('a bin that holds the limit counts the shots under it, so the red part matches the caption', () => {
  const values = [26, 28, 33, 500];
  const bins = histogram(values, 999, 3, 30);     // one bin a decade: 9–99 holds 26, 28 and 33
  const bin = bins.find((b) => b.lo <= 26 && b.hi > 33);
  assert.deepEqual([bin.count, bin.under], [3, 2]);
  assert.equal(bins.reduce((n, b) => n + b.under, 0), values.filter((v) => v < 30).length);
});

test('the ticks are the powers of ten up to the end of the axis, and end even when it does not', () => {
  assert.deepEqual(ticks(1000), [0, 1, 10, 100, 1000]);
  assert.deepEqual(ticks(5), [0, 1]);
  assert.ok(ticks(Infinity).length <= 20);
});
