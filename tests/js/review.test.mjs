// Unit tests of the UI's pure logic: run by tests/test_ui_js.py (`node --test`), no DOM or build step needed.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { keyAction } from '../../karsija/static/keys.js';
import { Order, eventGroups, filterGroups, reduce } from '../../karsija/static/review.js';

const shot = (id, group, auto, decided = null) => ({ id, group_id: group, auto_verdict: auto, user_verdict: decided });
// group 1: 1 best, 2 similar (kept), 3 blurry; 4 alone; 5 and 6 could not be analysed (no group)
const DATA = {
  groups: [
    { group_id: 1, shots: [shot(1, 1, 'best'), shot(2, 1, 'similar', 'keep'), shot(3, 1, 'blurry')] },
    { group_id: null, shots: [shot(5, null, 'error'), shot(6, null, 'error')] },
    { group_id: 2, shots: [shot(4, 2, 'keep')] },
  ],
};
const SHOTS = new Map(DATA.groups.flatMap((g) => g.shots).map((s) => [s.id, s]));
const NONE = { candidates: false, groups: false, unreviewed: false };

test('shots in no group are each a group of their own', () => {
  assert.deepEqual(eventGroups(DATA).map((g) => [g.key, g.groupId, g.ids]),
    [['g1', 1, [1, 2, 3]], ['s5', null, [5]], ['s6', null, [6]], ['g2', 2, [4]]]);
});

test('filters keep every member of a group but show only the shots that pass', () => {
  const groups = eventGroups(DATA);
  const cand = filterGroups(groups, SHOTS, { ...NONE, candidates: true });
  assert.deepEqual(cand.map((g) => [g.ids, g.members]), [[[2, 3], [1, 2, 3]]]);
  const unrev = filterGroups(groups, SHOTS, { ...NONE, unreviewed: true, groups: true });
  assert.deepEqual(unrev.map((g) => g.ids), [[1, 3]]);
  assert.equal(filterGroups(groups, SHOTS, NONE).length, 4);
});

test('Order moves by shot and by group and stops at the ends', () => {
  const groups = eventGroups(DATA);
  const order = new Order(filterGroups(groups, SHOTS, NONE));
  assert.equal(order.step(1, -1), 1);
  assert.equal(order.step(3, 1), 5);
  assert.equal(order.step(4, 1), 4);
  assert.equal(order.stepGroup(2, 1), 5);
  assert.equal(order.stepGroup(2, -1), 1);
  assert.equal(order.stepGroup(4, 1), 4);
  assert.equal(order.group(3).groupId, 1);
  // a shot not shown: stepping starts from the end it moves away from
  const cand = new Order(filterGroups(groups, SHOTS, { ...NONE, candidates: true }));
  assert.equal(cand.step(99, 1), 2);
  assert.equal(cand.step(99, -1), 3);
  assert.equal(new Order([]).step(1, 1), null);
});

test('Order.nearest: the shot itself, else the next one shown after it, else the last', () => {
  const groups = eventGroups(DATA);
  const all = groups.flatMap((g) => g.ids);
  const order = new Order(filterGroups(groups, SHOTS, { ...NONE, unreviewed: true }));    // 1, 3, 5, 6, 4
  assert.equal(order.nearest(3, all), 3);
  assert.equal(order.nearest(2, all), 3);
  const cand = new Order(filterGroups(groups, SHOTS, { ...NONE, candidates: true }));      // 2, 3
  assert.equal(cand.nearest(4, all), 3);
});

const key = (k, o = {}) => keyAction({ key: k, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false,
                                       repeat: false, isComposing: false, ...o });

test('keyAction', () => {
  assert.deepEqual(key('ArrowRight'), { type: 'move', by: 1 });
  assert.deepEqual(key('ArrowUp', { repeat: true }), { type: 'group', by: -1 });
  assert.deepEqual(key('P', { shiftKey: true }), { type: 'keep' });
  assert.deepEqual(key('3'), { type: 'rate', stars: 3 });
  assert.equal(key('6'), null);
  assert.deepEqual(key(' '), { type: 'accept' });
  assert.equal(key(' ', { repeat: true }), null);           // holding Space does not accept group after group
  assert.equal(key('x', { repeat: true }), null);
  assert.deepEqual(key('z', { ctrlKey: true }), { type: 'undo' });
  assert.deepEqual(key('Z', { metaKey: true }), { type: 'undo' });
  assert.equal(key('z', { ctrlKey: true, shiftKey: true }), null);
  assert.equal(key('z', { ctrlKey: true, repeat: true }), null);
  assert.equal(key('p', { ctrlKey: true }), null);
  assert.equal(key('p', { altKey: true }), null);
  assert.deepEqual(key('z'), { type: 'zoom' });
  assert.deepEqual(key('?', { shiftKey: true }), { type: 'help' });
});

test('a load lays over it the writes stored after it started', () => {
  const op = { changes: new Map() };
  let state = reduce(reduce({ written: [], pending: [], shots: new Map() }, { type: 'pending', op }),
    { type: 'done', op, seq: 1, shots: [], loading: true });
  state = reduce(state, { type: 'done', op, seq: 2, shots: [{ ...shot(3, 1, 'blurry'), user_verdict: 'reject' }],
                          loading: true });
  state = reduce(state, { type: 'load', eventId: 7, data: DATA, since: 1 });
  assert.equal(state.shots.get(3).user_verdict, 'reject');      // stored after the load started
  assert.equal(state.shots.get(1).user_verdict, null);
  assert.deepEqual(state.written.map((w) => w.seq), [2]);
  // with no load under way, nothing needs remembering
  state = reduce(state, { type: 'done', op, seq: 3, shots: [], loading: false });
  assert.deepEqual(state.written, []);
});

test('refused writes are counted until dismissed', () => {
  const op = { changes: new Map() };
  let state = { pending: [op], failure: null };
  state = reduce(state, { type: 'failed', op, message: 'a' });
  state = reduce(state, { type: 'failed', op, message: 'b' });
  assert.deepEqual(state.failure, { text: 'b', count: 2 });
  assert.equal(reduce(state, { type: 'dismiss' }).failure, null);
});
