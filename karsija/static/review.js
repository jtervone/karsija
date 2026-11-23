// Decisions of one event, shared by the review views: the shots as the server has them, decisions shown at once
// and sent in order in the background (dropped again if the server refuses them), undo, and moving through shots.
import { api } from './api.js';
import { useCallback, useEffect, useMemo, useReducer, useRef } from './ui.js';

/** Decision suggested by each automatic verdict, the same as the server's group accept. */
export const SUGGESTION = { best: 'keep', keep: 'keep', similar: 'reject', blurry: 'reject' };
/** Automatic verdicts of the cull candidates. */
export const CANDIDATE = new Set(['similar', 'blurry']);

/**
 * The groups of the event in order: `{ key, groupId, ids }`. Shots in no group (those that could not be analysed)
 * are each a group of their own, with `groupId` null.
 */
export function eventGroups(data) {
  const out = [];
  for (const g of data.groups) {
    if (g.group_id == null) for (const s of g.shots) out.push({ key: `s${s.id}`, groupId: null, ids: [s.id] });
    else out.push({ key: `g${g.group_id}`, groupId: g.group_id, ids: g.shots.map((s) => s.id) });
  }
  return out;
}

/**
 * The groups as shown: `ids` only the shots that pass the filters, `members` every shot of the group. A group
 * with no shot left is dropped. Filters: `candidates` (judged similar or blurry), `groups` (groups of more than
 * one shot), `unreviewed` (no decision yet).
 */
export function filterGroups(groups, shots, filters) {
  const out = [];
  for (const g of groups) {
    if (filters.groups && g.ids.length < 2) continue;
    const ids = g.ids.filter((id) => {
      const s = shots.get(id);
      return (!filters.candidates || CANDIDATE.has(s.auto_verdict)) && (!filters.unreviewed || s.user_verdict == null);
    });
    if (ids.length > 0) out.push({ ...g, members: g.ids, ids });
  }
  return out;
}

/** Positions of the shown shots, for moving with the arrow keys. */
export class Order {
  constructor(groups) {
    this.groups = groups;
    this.ids = groups.flatMap((g) => g.ids);
    this.index = new Map(this.ids.map((id, i) => [id, i]));
    this.groupIndex = new Map();
    groups.forEach((g, i) => g.ids.forEach((id) => this.groupIndex.set(id, i)));
  }

  group(id) {
    return this.groups[this.groupIndex.get(id)] ?? null;
  }

  /** The shot `by` places from `id`, stopping at either end. */
  step(id, by) {
    if (this.ids.length === 0) return null;
    const i = this.index.get(id) ?? (by > 0 ? -1 : this.ids.length);
    return this.ids[Math.min(Math.max(i + by, 0), this.ids.length - 1)];
  }

  /** The first shot of the group `by` groups from that of `id`, stopping at either end. */
  stepGroup(id, by) {
    if (this.groups.length === 0) return null;
    const i = this.groupIndex.get(id) ?? (by > 0 ? -1 : this.groups.length);
    return this.groups[Math.min(Math.max(i + by, 0), this.groups.length - 1)].ids[0];
  }

  /** `id` if it is shown, else the first shown shot after it in `all` (every shot in order), else the last one. */
  nearest(id, all) {
    if (this.index.has(id)) return id;
    const from = all.indexOf(id);
    if (from >= 0) for (const other of all.slice(from + 1)) if (this.index.has(other)) return other;
    return this.ids.at(-1) ?? null;
  }
}

/** The shots with the decisions not yet stored laid over them. Shots no pending write touches keep their object. */
function overlay(shots, pending) {
  if (pending.length === 0) return shots;
  const out = new Map(shots);
  for (const op of pending) {
    for (const [id, fields] of op.changes) {
      const s = out.get(id);
      if (s) out.set(id, { ...s, ...fields });
    }
  }
  return out;
}

function withShots(shots, list) {
  const out = new Map(shots);
  for (const s of list) if (out.has(s.id)) out.set(s.id, s);
  return out;
}

const EMPTY = { eventId: null, data: null, shots: new Map(), groups: [], pending: [], written: [], error: null,
                notice: null, failure: null };

/**
 * State of `useReview`. `notice`: a passing message (`{ text }`), `failure`: writes the server refused since the
 * user last dismissed them (`{ text, count }`), kept until dismissed. `written`: answers of the writes stored while
 * a load was under way, laid over that load's answer.
 */
export function reduce(state, action) {
  switch (action.type) {
    case 'load': {
      // writes stored after the request started may be missing from its answer: lay them over it again
      const written = state.written.filter((w) => w.seq > action.since);
      const byId = new Map(action.data.groups.flatMap((g) => g.shots).map((s) => [s.id, s]));
      const shots = written.reduce((m, w) => withShots(m, w.shots), byId);
      return { ...state, eventId: action.eventId, data: action.data, shots, groups: eventGroups(action.data),
               written, error: null };
    }
    case 'loadError':
      return { ...state, error: { eventId: action.eventId, message: action.message } };
    case 'pending':
      return { ...state, pending: [...state.pending, action.op] };
    case 'done':
      return { ...state, shots: withShots(state.shots, action.shots),
               pending: state.pending.filter((op) => op !== action.op),
               written: action.loading ? [...state.written, { seq: action.seq, shots: action.shots }] : [] };
    case 'failed':
      return { ...state, pending: state.pending.filter((op) => op !== action.op),
               failure: { text: action.message, count: (state.failure?.count ?? 0) + 1 } };
    case 'notice':
      return { ...state, notice: action.notice };
    case 'dismiss':
      return { ...state, failure: null };
    default:
      throw new Error(action.type);
  }
}

/**
 * Shots and decisions of event `eventId`, reloaded whenever `version` changes.
 *
 * Returns `{ data, groups, shots, error, notice, failure, decide, accept, undo, notify, dismiss }`: `shots` maps a
 * shot id to
 * the shot with its decision as last made here; `data` is null while the event loads. Decisions are written one at
 * a time in the order they were made, so an undo always reaches the server after the decision it undoes.
 */
export function useReview(eventId, version) {
  const [state, dispatch] = useReducer(reduce, EMPTY);
  const done = useRef(0);                  // writes stored so far
  const chain = useRef(Promise.resolve());
  const undoStack = useRef([]);            // decisions made on this page: { prev: Map shot id -> old decision }
  const loading = useRef(0);               // loads under way

  useEffect(() => { undoStack.current = []; }, [eventId]);
  useEffect(() => {
    let current = true;
    const since = done.current;
    loading.current += 1;
    api(`/events/${eventId}`).then(
      (data) => current && dispatch({ type: 'load', eventId, data, since }),
      (e) => current && dispatch({ type: 'loadError', eventId, message: e.message }),
    ).finally(() => { loading.current -= 1; });
    return () => { current = false; };
  }, [eventId, version]);

  const ours = state.eventId === eventId;
  const shots = useMemo(() => overlay(state.shots, state.pending), [state.shots, state.pending]);
  const latest = useRef(shots);
  latest.current = shots;

  /** Show `changes` (Map shot id -> fields) now and run `send` after the writes before it. */
  const write = useCallback((changes, send, { onDone, onFail, failText = 'Päätöstä ei tallennettu' } = {}) => {
    const op = { changes };
    dispatch({ type: 'pending', op });
    chain.current = chain.current.then(send).then(
      (res) => {
        done.current += 1;
        dispatch({ type: 'done', op, seq: done.current, shots: res.shots, loading: loading.current > 0 });
        onDone?.(res);
      },
      (e) => {
        dispatch({ type: 'failed', op, message: `${failText}: ${e.message}` });
        onFail?.();
      },
    );
  }, []);

  /** Put a write on the undo stack; it comes off again if the server stores nothing (refused, or no batch). */
  const remember = (prev) => {
    const entry = { prev };
    undoStack.current.push(entry);
    const drop = () => { undoStack.current = undoStack.current.filter((e) => e !== entry); };
    return { onDone: (res) => { if (res.batch == null) drop(); }, onFail: drop };
  };

  /** Set `user_verdict` and/or `rating` of a shot (null clears). Returns false when nothing changes. */
  const decide = useCallback((id, fields) => {
    const s = latest.current.get(id);
    if (!s || Object.entries(fields).every(([k, v]) => s[k] === v)) return false;
    const prev = new Map([[id, { user_verdict: s.user_verdict, rating: s.rating }]]);
    write(new Map([[id, fields]]), () => api(`/shots/${id}`, { method: 'PATCH', body: fields }), remember(prev));
    return true;
  }, [write]);

  /** Accept the suggestion for the shots of `group` that have no decision yet. Returns false when there are none. */
  const accept = useCallback((group) => {
    if (group.groupId == null) return false;
    const changes = new Map();
    const prev = new Map();
    for (const id of group.members ?? group.ids) {
      const s = latest.current.get(id);
      if (s && s.user_verdict == null && SUGGESTION[s.auto_verdict]) {
        changes.set(id, { user_verdict: SUGGESTION[s.auto_verdict] });
        prev.set(id, { user_verdict: s.user_verdict, rating: s.rating });
      }
    }
    if (changes.size === 0) return false;
    write(changes, () => api(`/groups/${eventId}/${group.groupId}/accept`, { method: 'POST', body: {} }),
      remember(prev));
    return true;
  }, [eventId, write]);

  /**
   * Undo the last decision batch, on the server: it may be one made before this page was opened, or in another
   * event. Returns the ids of the shots it is known to restore (empty when only the server knows).
   */
  const undo = useCallback(() => {
    const entry = undoStack.current.pop();
    write(entry ? entry.prev : new Map(), () => api('/decisions/undo', { method: 'POST', body: {} }), {
      onDone: (res) => {
        let text = null;
        if (res.batch == null) text = 'Ei kumottavaa.';
        else if (!res.shots.some((s) => latest.current.has(s.id))) text = 'Kumottiin päätös toisesta tapahtumasta.';
        if (text) dispatch({ type: 'notice', notice: { text } });
      },
      onFail: () => { if (entry) undoStack.current.push(entry); },
      failText: 'Kumoaminen epäonnistui',
    });
    return entry ? [...entry.prev.keys()] : [];
  }, [write]);

  /** Show a passing message, or clear it with null. */
  const notify = useCallback((text) => dispatch({ type: 'notice', notice: text == null ? null : { text } }), []);
  const dismiss = useCallback(() => dispatch({ type: 'dismiss' }), []);
  /** Resolves once every decision made so far has been stored or refused. */
  const settled = useCallback(() => chain.current, []);

  return {
    data: ours ? state.data : null, groups: ours ? state.groups : [], shots,
    error: state.error?.eventId === eventId ? state.error.message : null,
    notice: state.notice, failure: state.failure, decide, accept, undo, notify, dismiss, settled,
  };
}
