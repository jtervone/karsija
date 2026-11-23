// Unit tests of the Toteuta confirmation's texts: run by tests/test_ui_js.py (`node --test`).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { applyText, planSummary, restoreText } from '../../karsija/static/apply.js';

const PLAN = {
  move: [{ shot_id: 1, files: ['a.jpg', 'a.xmp'] }, { shot_id: 2, files: ['b.NEF', 'b.JPG', 'b.NEF.xmp'] }],
  xmp_create: ['c.xmp'],
  xmp_update: ['a.xmp', 'b.NEF.xmp'],
  user_values: [],
  unreviewed: 4,
  conflicts: [],
};

test('the summary says how many shots move, how many sidecars are written and how many are unreviewed', () => {
  assert.deepEqual(planSummary(PLAN), {
    text: 'Siirretään 2 kuvaa (5 tiedostoa), kirjoitetaan 3 XMP:tä (1 uutta, 2 päivitettyä), 4 tarkastamatta.',
    empty: false,
    ready: true,
  });
  const one = { ...PLAN, move: [{ shot_id: 1, files: ['a.jpg'] }], xmp_create: [], xmp_update: ['a.xmp'] };
  assert.equal(planSummary(one).text,
    'Siirretään 1 kuva (1 tiedosto), kirjoitetaan 1 XMP (0 uutta, 1 päivitettyä), 4 tarkastamatta.');
});

test('a plan with a conflict or nothing to do cannot be confirmed', () => {
  assert.equal(planSummary({ ...PLAN, conflicts: [{ path: 'x', reason: 'on jo olemassa' }] }).ready, false);
  const empty = { ...PLAN, move: [], xmp_create: [], xmp_update: [] };
  assert.deepEqual(planSummary(empty), {
    text: 'Siirretään 0 kuvaa, kirjoitetaan 0 XMP:tä, 4 tarkastamatta.', empty: true, ready: false,
  });
});

test('an apply of the history', () => {
  assert.equal(applyText({ shots_moved: 3, xmp_created: 2, xmp_updated: 6 }), 'siirretty 3 kuvaa, kirjoitettu 8 XMP:tä');
  assert.equal(applyText({ shots_moved: 1, xmp_created: 0, xmp_updated: 1 }), 'siirretty 1 kuva, kirjoitettu 1 XMP');
});

test('an apply that did not finish says how far it got', () => {
  const a = { shots_moved: 3, xmp_created: 2, xmp_updated: 6, ops: 11, done: 4 };
  assert.equal(applyText({ ...a, status: 'failed' }), 'keskeytyi: tehty 4 / 11 toimenpiteestä');
  assert.equal(applyText({ ...a, status: 'done' }), 'siirretty 3 kuvaa, kirjoitettu 8 XMP:tä');
});

test('the restore message names only what restore kept, and warns of missing files', () => {
  const base = { missing: [], created: [], changed: [], log: null, undone_dir: '/c/undone/1' };
  assert.deepEqual(restoreText(base), { text: 'Palautettu.', dir: null, warn: false });
  const res = restoreText({ ...base, missing: ['e/_karsitut/a.jpg'], changed: ['/c/undone/1/e/c.xmp'], log: '/c/l.json' });
  assert.equal(res.text, 'Palautettu. 1 siirretty tiedosto puuttui: e/_karsitut/a.jpg. 1 sivutiedosto oli muokattu '
    + 'toteutuksen jälkeen; muokattu versio kopioitiin talteen ennen palautusta. Toteutuksen loki siirrettiin talteen.');
  assert.equal(res.dir, '/c/undone/1');
  assert.equal(res.warn, true);
});
