// Vertailu: the shots of one group side by side, zoomed and panned together.
import { DECISIONS, DecisionBadge, Stars, VERDICTS } from './grid.js';
import { guessSize, layout, place, sharpText, sizeOf, useSize, zoomAt, zoomLabel, zoomScale } from './loupe.js';
import { SUGGESTION } from './review.js';
import { html, preload, useEffect, useLayoutEffect, useRef, useState } from './ui.js';

export const MAX_PANES = 4;
const PRELOAD_DELAY = 150;        // ms the view rests before the next panes' full previews load
const CLICK_SLOP = 4;             // px a press may move and still count as a click, not a drag
const DOUBLE_CLICK = 300;         // ms after a click that selected a pane in which another click does not zoom

// ---------------------------------------------------------------------------------------------------------------- //
// Which shots, in which order.

/**
 * The shots `ids` of a group best first: the one judged best, then by sharpness, those that could not be analysed
 * last. Otherwise in their own order (capture time).
 */
export function bestFirst(ids, shots) {
  const rank = (s) => (s.error ? 2 : s.auto_verdict === 'best' ? 0 : 1);
  return [...ids].sort((a, b) => {
    const x = shots.get(a);
    const y = shots.get(b);
    return rank(x) - rank(y) || (rank(x) === 1 ? (y.sharp ?? 0) - (x.sharp ?? 0) : 0);
  });
}

/** Whether the compare view can show `group` (see `filterGroups`): a group with at least two shots shown. */
export const comparable = (group) => group?.groupId != null && group.ids.length > 1;

/** The shot `by` places from `id` in `ids`, stopping at either end. */
export function stepWithin(ids, id, by) {
  return ids[Math.min(Math.max(ids.indexOf(id) + by, 0), ids.length - 1)];
}

/** The nearest group `by` (±1) groups from that of `id` that has at least two shots shown, or null. */
export function comparableGroup(order, id, by) {
  for (let i = (order.groupIndex.get(id) ?? (by > 0 ? -1 : order.groups.length)) + by;
    i >= 0 && i < order.groups.length; i += by) {
    if (comparable(order.groups[i])) return order.groups[i];
  }
  return null;
}

/**
 * First of the `size` panes shown out of `n` shots, so that shot `index` is among them: the panes move only as far
 * as the selection needs from where they started (`start`).
 */
export function windowStart(n, index, start, size = MAX_PANES) {
  if (n <= size) return 0;
  const s = Math.min(Math.max(start, index - size + 1), index);
  return Math.min(Math.max(s, 0), n - size);
}

// ---------------------------------------------------------------------------------------------------------------- //
// One viewport for every pane: null when fitted, else `{ cx, cy, edge }`: the image point at the centre of each pane
// as fractions of that pane's image (as the loupe's zoom), and the zoom as the length in CSS pixels of the image's
// long edge. So the same spot shows at the same size in every pane, even where the previews differ in size, shape
// or orientation. `stage`, `img` and `dpr` as in the loupe's `layout`.

const longEdge = (img) => Math.max(img.w, img.h);

/** Scale and top-left corner `{ s, x, y, zoomed }` of the image in a pane. Fitted when `view` would not enlarge it. */
export function paneLayout(stage, img, view, dpr = 1) {
  const fit = layout(stage, img, null, dpr);
  if (!view) return fit;
  const s = view.edge / longEdge(img);
  if (s <= fit.s * 1.01) return fit;
  return { s, x: place(stage.w, img.w * s, view.cx), y: place(stage.h, img.h * s, view.cy), zoomed: true };
}

/** The point of the image at the centre of a pane, as far as `view` can actually move it there. */
function centre(stage, img, view, dpr) {
  const { s, x, y } = paneLayout(stage, img, view, dpr);
  return { cx: (stage.w / 2 - x) / (img.w * s), cy: (stage.h / 2 - y) / (img.h * s) };
}

/**
 * The view zoomed in on a pane as the loupe zooms (see `zoomScale`, `zoomAt`): 100 % of this pane's image, the
 * point under `point` staying under it. Null when zooming in would not enlarge the image.
 */
export function viewAt(stage, img, point, dpr = 1) {
  const s = zoomScale(stage, img, dpr);
  if (s == null) return null;
  return { ...zoomAt(stage, img, point, dpr), edge: s * longEdge(img) };
}

/** `view` dragged by `dx`, `dy` CSS pixels in a pane (its image follows the pointer). */
export function panView(stage, img, view, dx, dy, dpr = 1) {
  const { s } = paneLayout(stage, img, view, dpr);
  const from = centre(stage, img, view, dpr);
  const to = { ...view, cx: from.cx - dx / (img.w * s), cy: from.cy - dy / (img.h * s) };
  return { ...to, ...centre(stage, img, to, dpr) };
}

// ---------------------------------------------------------------------------------------------------------------- //

/** The full previews of `shots`, once the view rests. */
function usePreloadFull(shots) {
  const urls = shots.map((s) => s.preview.full).join(' ');
  useEffect(() => {
    const timer = setTimeout(() => { for (const s of shots) preload(s.preview.full); }, PRELOAD_DELAY);
    return () => clearTimeout(timer);
  }, [urls]);
}

/** Sharpness (and share of the group's sharpest), automatic verdict and the user's decision under a pane. */
function PaneInfo({ shot, best }) {
  const auto = shot.auto_verdict;
  const suggestion = SUGGESTION[auto];
  const decided = shot.user_verdict;
  const sharp = sharpText(shot, best);
  const ratio = best > 0 && !shot.error ? shot.sharp / best : null;
  return html`<div class="pane-info">
    <div class="line">
      <span class="auto" title=${shot.reason || undefined}>
        ${VERDICTS[auto] ?? 'ei arviota'}${suggestion && ` → ${DECISIONS[suggestion]}`}</span>
      <span class=${`decided ${decided ?? ''}`}>
        ${decided ? DECISIONS[decided] : html`<span class="muted">ei päätöstä</span>`}
        ${shot.rating != null && ' '}<${Stars} rating=${shot.rating} /></span>
    </div>
    <div class="line">
      <span class="sharp" title=${sharp ? `terävyys ${sharp}` : shot.error}>${shot.error
        ? shot.error : `${Math.round(shot.sharp)}${ratio != null ? ` · ${Math.round(ratio * 100)} %` : ''}`}</span>
      <span class="name" title=${shot.path}>${shot.name}</span>
    </div>
  </div>`;
}

/**
 * One shot in its pane. `view` and `setView` are the shared viewport; `zooms.current` maps the shot's id to a
 * function giving the view zoomed in at a point of this pane (null when it cannot zoom), `hover.current` is
 * `{ id, point }` of the pane under the pointer. A click selects the pane, a click on the selected one calls
 * `onToggle(id, point)`; a drag on a zoomed pane pans every pane.
 */
function Pane({ shot, best, selected, view, setView, onSelect, onToggle, zooms, hover, last }) {
  const stageRef = useRef(null);
  const stage = useSize(stageRef);
  const [img, setImg] = useState(null);           // { src, w, h } of the full preview once loaded, or { src, error }
  const [grid, setGrid] = useState(null);         // { src, w, h } of the grid preview once loaded
  const [dragging, setDragging] = useState(false);
  const drag = useRef(null);
  const picked = useRef(-Infinity);                // time of the click that selected this pane

  const src = shot.preview.full;
  const failed = img?.src === src && !!img.error;
  const loaded = img?.src === src && !img.error ? img : null;
  const size = failed ? null : loaded ?? guessSize(grid?.src === shot.preview.grid ? grid : null, last.current);
  const dpr = globalThis.devicePixelRatio || 1;
  const box = size && stage ? paneLayout(stage, size, view, dpr) : null;
  const canZoom = !!box && (box.zoomed || zoomScale(stage, size, dpr) != null);

  useLayoutEffect(() => {
    zooms.current.set(shot.id, (point) => (box ? viewAt(stage, size, point, dpr) : null));
    return () => zooms.current.delete(shot.id);
  });

  const at = (e) => {
    const r = stageRef.current.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, from: box?.zoomed ? view : null, moved: false };
  };
  const onPointerMove = (e) => {
    hover.current = { id: shot.id, point: at(e) };
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < CLICK_SLOP) return;
    d.moved = true;
    if (d.from && size && stage) {
      setDragging(true);
      setView(panView(stage, size, d.from, dx, dy, dpr));
    }
  };
  const onPointerUp = (e) => {
    const d = drag.current;
    drag.current = null;
    setDragging(false);
    if (!d || d.moved) return;
    if (!selected) {
      onSelect(shot.id);
      picked.current = e.timeStamp;
    } else if (e.timeStamp - picked.current >= DOUBLE_CLICK) onToggle(shot.id, at(e));
  };
  const onPointerCancel = () => {
    drag.current = null;
    setDragging(false);
  };
  const onPointerLeave = () => {
    if (!drag.current && hover.current?.id === shot.id) hover.current = null;
  };

  const placed = box && { width: `${size.w * box.s}px`, height: `${size.h * box.s}px`,
                          transform: `translate(${box.x}px, ${box.y}px)` };
  const decided = shot.user_verdict;
  const cls = `stage${box?.zoomed ? ' zoomed' : ''}${!selected && !box?.zoomed ? ' pick' : ''}`
    + `${selected && !canZoom && !view ? ' still' : ''}${dragging ? ' dragging' : ''}${decided ? ` ${decided}` : ''}`;
  let title;
  const unzoomed = !!view && !!box && !box.zoomed;     // zoomed in elsewhere, but this image would not enlarge
  if (box?.zoomed) title = `Vedä siirtääksesi kaikkia, klikkaa ${selected ? 'sovittaaksesi (Z)' : 'valitaksesi'}`;
  else if (!selected) title = `${unzoomed ? 'Ei suurennettavissa. ' : ''}Klikkaa valitaksesi`;
  else if (view) {
    title = 'Ei suurennettavissa: esikatselu näkyy jo kokonaan täydessä koossa. Klikkaa sovittaaksesi kaikki (Z)';
  }
  else if (canZoom) title = `Klikkaa: ${zoomLabel(zoomScale(stage, size, dpr), dpr)} tästä, muut samaan kohtaan (Z)`;
  return html`<figure class=${`pane verdict-${shot.auto_verdict ?? 'none'}${selected ? ' selected' : ''}`}
      aria-current=${selected ? 'true' : undefined} data-shot=${shot.id}>
    <div ref=${stageRef} class=${cls} title=${title}
        onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onPointerUp=${onPointerUp}
        onPointerCancel=${onPointerCancel} onPointerLeave=${onPointerLeave}>
      <img key=${shot.preview.grid} class=${`${placed ? 'placed' : 'fitted'}${loaded ? ' covered' : ''}`}
        style=${placed || undefined} src=${shot.preview.grid} alt="" draggable="false"
        onLoad=${(e) => setGrid(sizeOf(shot.preview.grid, e))} />
      ${!failed && html`<img key=${src} class=${placed ? 'placed' : 'fitted'} style=${placed || undefined} src=${src}
        alt=${shot.name} draggable="false" decoding="async"
        onLoad=${(e) => {
          last.current = sizeOf(src, e);
          setImg(last.current);
        }}
        onError=${() => setImg({ src, error: true })} />`}
      <${DecisionBadge} decided=${decided} />
      ${failed && html`<p class="stage-label error">
        Täysikokoista esikatselua ei voi näyttää; vain ruudukon pikkukuva.</p>`}
      ${!failed && (box?.zoomed || unzoomed || !loaded) && html`<span class="stage-label">
        ${[box?.zoomed && zoomLabel(box.s, dpr), unzoomed && 'sovitettu, ei suurennettavissa', !loaded && 'ladataan…']
          .filter(Boolean).join(' · ')}</span>`}
    </div>
    <${PaneInfo} shot=${shot} best=${best} />
  </figure>`;
}

/**
 * Vertailu of `group` (see `filterGroups`): `ids` its shown shots best first (see `bestFirst`), at most
 * `MAX_PANES` of them at a time, those around `current`, the selected one. `best`: sharpness of the group's
 * sharpest shot, `position`: where `current` is, as text, `next`: the shots whose full previews load ahead (the
 * next group's). `onPick(id)` selects a shot, `notify(text)` shows a passing message. `keys.current` is set to the
 * view's own key handler (Z).
 *
 * Every pane shows the same viewport (see `paneLayout`): fitted at first; Z or a click on the selected pane zooms
 * every pane in around the same spot, and back; dragging a zoomed pane pans them all. A new group starts fitted.
 */
export function Compare({ group, ids, shots, current, best, position, next, onPick, notify, keys }) {
  const [state, setState] = useState({ key: null, view: null });
  const view = state.key === group.key ? state.view : null;
  const setView = (v) => setState({ key: group.key, view: v });
  // a group starts fitted, also one returned to: forget the zoom of the group left
  useEffect(() => { setState({ key: group.key, view: null }); }, [group.key]);
  const zooms = useRef(new Map());
  const hover = useRef(null);
  const last = useRef(null);                       // size of the last full preview loaded, for guessSize
  const placed = useRef({ key: null, start: 0 });

  const index = Math.max(ids.indexOf(current), 0);
  const start = windowStart(ids.length, index, placed.current.key === group.key ? placed.current.start : 0);
  placed.current = { key: group.key, start };
  const visible = ids.slice(start, start + MAX_PANES);
  usePreloadFull([ids[start - 1], ids[start + MAX_PANES], ...next].filter((id) => id != null)
    .map((id) => shots.get(id)));

  /**
   * Fit every pane, or zoom in at `point` of the pane of `id` (null: its centre); when that image would not enlarge,
   * at the centre of the first pane whose image would. False when nothing changes.
   */
  const toggle = (id, point) => {
    if (view) {
      setView(null);
      return true;
    }
    let v = zooms.current.get(id)?.(point);
    for (const other of visible) v = v ?? zooms.current.get(other)?.(null);
    if (!v) {
      notify('Yhtäkään näistä kuvista ei voi suurentaa: esikatselut näkyvät jo täydessä koossa.');
      return false;
    }
    setView(v);
    return true;
  };

  useEffect(() => {
    // scrolling (narrow screens) moves the panes under a pointer that has not moved: Z goes by the selection then
    const forget = () => { hover.current = null; };
    addEventListener('scroll', forget, { passive: true });
    return () => removeEventListener('scroll', forget);
  }, []);
  useLayoutEffect(() => {
    keys.current = (action) => {
      if (action.type !== 'zoom') return false;
      const h = hover.current && zooms.current.has(hover.current.id) ? hover.current : { id: current, point: null };
      return toggle(h.id, h.point);
    };
    return () => { keys.current = null; };
  });

  return html`<div class="compare">
    <p class="position muted">
      <span>${position}${ids.length > MAX_PANES
        && ` · näkyvissä ${start + 1}–${start + visible.length} / ${ids.length}`}</span>
      <span class="hint">Klikkaus valitsee, valitun klikkaus tai Z suurentaa kaikki, vetäminen siirtää kaikkia</span>
    </p>
    <div class="panes" data-count=${visible.length}>
      ${visible.map((id) => html`<${Pane} key=${id} shot=${shots.get(id)} best=${best} selected=${id === current}
        view=${view} setView=${setView} onSelect=${onPick} onToggle=${toggle} zooms=${zooms} hover=${hover}
        last=${last} />`)}
    </div>
  </div>`;
}
