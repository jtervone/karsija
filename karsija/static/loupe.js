// Loupe: one shot at full preview size, fitted or zoomed in, with its metadata beside it.
import { api } from './api.js';
import { DECISIONS, DecisionBadge, Stars, VERDICTS } from './grid.js';
import { SUGGESTION } from './review.js';
import { fmt, html, preload, useEffect, useLayoutEffect, useRef, useState } from './ui.js';

const PRELOAD_DELAY = 150;        // ms the loupe rests on a shot before its neighbours load (not while a key is held)
const CLICK_SLOP = 4;             // px a press may move and still count as a click, not a drag
const DOUBLE_CLICK = 300;         // ms after a click toggled the zoom in which another click does nothing
const GRID_EDGE = 400;            // long edges of the previews (previews.SIZES)
const FULL_EDGE = 2560;

// ---------------------------------------------------------------------------------------------------------------- //
// Where the image goes. `stage` and `img` are sizes `{ w, h }`: the stage in CSS pixels, the image in its own pixels.
// `zoom` is null when fitted, else the image point at the stage's centre as fractions of the image `{ cx, cy }`: it
// stays put from shot to shot, so the same spot of the next shot of a burst shows zoomed in. `dpr` is the device
// pixel ratio.

const MIN_ENLARGE = 1.5;          // zooming in to one image pixel per screen pixel must enlarge at least this much

/** On one axis: where an image of `len` starts in a stage of `size`, centred on fraction `c`, never leaving a gap. */
export function place(size, len, c) {
  if (len <= size) return (size - len) / 2;
  return Math.min(0, Math.max(size - len, size / 2 - c * len));
}

/** Scale of the fitted image: all of it, never enlarged past one image pixel per screen pixel. */
function fitScale(stage, img, dpr) {
  return Math.min(stage.w / img.w, stage.h / img.h, 1 / dpr);
}

/**
 * Scale (CSS px per image pixel) when zoomed in: one image pixel per screen pixel (100 %), or, where that hardly
 * enlarges the fitted image (a HiDPI screen), one per CSS pixel (200 % on a 2× screen). Null when even that would
 * not enlarge it: the image already shows whole at full size.
 */
export function zoomScale(stage, img, dpr = 1) {
  const fit = fitScale(stage, img, dpr);
  if (1 / dpr >= fit * MIN_ENLARGE) return 1 / dpr;
  return 1 > fit * 1.01 ? 1 : null;
}

/** The zoom in screen pixels as text: `100 %`, `200 %`. */
export const zoomLabel = (s, dpr = 1) => `${Math.round(s * dpr * 100)} %`;

/**
 * Scale and top-left corner `{ s, x, y, zoomed }` of the image in the stage. `zoomed` is false when `zoom` is null or
 * zooming in would not enlarge the image.
 */
export function layout(stage, img, zoom, dpr = 1) {
  const s = zoom ? zoomScale(stage, img, dpr) : null;
  if (s == null) {
    const fit = fitScale(stage, img, dpr);
    return { s: fit, x: (stage.w - img.w * fit) / 2, y: (stage.h - img.h * fit) / 2, zoomed: false };
  }
  return { s, x: place(stage.w, img.w * s, zoom.cx), y: place(stage.h, img.h * s, zoom.cy), zoomed: true };
}

/** `zoom` as far as the image can actually move: the centre that `layout` shows for it. */
export function clampZoom(stage, img, zoom, dpr = 1) {
  const { s, x, y } = layout(stage, img, zoom, dpr);
  return { cx: (stage.w / 2 - x) / (img.w * s), cy: (stage.h / 2 - y) / (img.h * s) };
}

const clamp01 = (v) => Math.min(Math.max(v, 0), 1);

/**
 * Zoomed in with the image point under `point` (stage coordinates, fitted; beside the image: its nearest edge)
 * staying under it; `point` null: the centre. Null when zooming in would not enlarge the image.
 */
export function zoomAt(stage, img, point, dpr = 1) {
  const s = zoomScale(stage, img, dpr);
  if (s == null) return null;
  const fit = layout(stage, img, null, dpr);
  const p = point ?? { x: stage.w / 2, y: stage.h / 2 };
  const fx = clamp01((p.x - fit.x) / (img.w * fit.s));
  const fy = clamp01((p.y - fit.y) / (img.h * fit.s));
  return clampZoom(stage, img, { cx: fx + (stage.w / 2 - p.x) / (img.w * s), cy: fy + (stage.h / 2 - p.y) / (img.h * s) },
    dpr);
}

/** `zoom` dragged by `dx`, `dy` CSS pixels (the image follows the pointer). */
export function pan(stage, img, zoom, dx, dy, dpr = 1) {
  const { s } = layout(stage, img, zoom, dpr);
  const from = clampZoom(stage, img, zoom, dpr);
  return clampZoom(stage, img, { cx: from.cx - dx / (img.w * s), cy: from.cy - dy / (img.h * s) }, dpr);
}

/**
 * Size to lay out a shot whose full preview has not loaded yet, from its loaded grid preview `grid`: the last loaded
 * full preview's `last` when the shape is the same (a burst), else the grid preview's scaled up to the long edge of
 * `last` (likely the same camera; a RAW's embedded preview is often under 2560 px), or without one to 2560 px. An
 * approximation: the real size replaces it once loaded. An original under 400 px is previewed at its own size.
 */
export function guessSize(grid, last) {
  if (!grid) return null;
  if (last && Math.abs(grid.w / grid.h - last.w / last.h) < 0.01) return last;
  const edge = Math.max(grid.w, grid.h);
  const k = edge < GRID_EDGE ? 1 : (last ? Math.max(last.w, last.h) : FULL_EDGE) / edge;
  return { w: grid.w * k, h: grid.h * k };
}

// ---------------------------------------------------------------------------------------------------------------- //
// Metadata as text. Each returns null when the value is missing.

const one = new Intl.NumberFormat('fi-FI', { maximumFractionDigits: 1 });
const seconds = new Intl.DateTimeFormat('fi-FI', { dateStyle: 'short', timeStyle: 'medium' });

/** Exposure time in seconds: `1/250 s`, `0,4 s`, `2 s`. */
export function fmtExposure(t) {
  if (!(t > 0)) return null;
  const n = 1 / t;
  if (t < 1 && (n >= 10 || Math.abs(n - Math.round(n)) < 0.05)) return `1/${Math.round(n)} s`;
  return `${one.format(t)} s`;
}

export const fmtAperture = (f) => (f > 0 ? `f/${one.format(f)}` : null);
export const fmtIso = (iso) => (iso > 0 ? `ISO ${fmt(iso)}` : null);

/** `50 mm`, with the 35 mm equivalent when the camera stores a different one: `50 mm (kinovastaava 75 mm)`. */
export function fmtFocal(mm, equiv) {
  if (!(mm > 0)) return null;
  const extra = equiv > 0 && Math.round(equiv) !== Math.round(mm) ? ` (kinovastaava ${fmt(Math.round(equiv))} mm)` : '';
  return `${one.format(mm)} mm${extra}`;
}

/** A fraction as a percentage, `< 0,1 %` for a trace. */
export function fmtShare(fraction) {
  if (fraction == null) return null;
  const pct = fraction * 100;
  return pct > 0 && pct < 0.05 ? '< 0,1 %' : `${one.format(pct)} %`;
}

// ---------------------------------------------------------------------------------------------------------------- //

const details = new Map();       // full preview URL (it changes with the files) -> promise of the shot's EXIF

/** EXIF of `shot`, from the shot's own API (the event's list leaves it out), each shot asked once. */
function shotExif(shot) {
  const key = shot.preview.full;
  let found = details.get(key);
  if (!found) {
    if (details.size > 2000) details.clear();
    found = api(`/shots/${shot.id}`).then((d) => d.exif ?? {});
    found.catch(() => details.delete(key));      // asked again next time
    details.set(key, found);
  }
  return found;
}

function useExif(shot) {
  const key = shot.preview.full;
  const [state, setState] = useState({ key: null, exif: null, error: null });
  useEffect(() => {
    let current = true;
    shotExif(shot).then(
      (exif) => current && setState({ key, exif, error: null }),
      (e) => current && setState({ key, exif: null, error: e.message }),
    );
    return () => { current = false; };
  }, [key]);
  return state.key === key ? state : { exif: null, error: null };
}

/** The previous and next shots' full previews and EXIF, once the loupe rests on a shot. */
function usePreloadNeighbours(neighbours) {
  const urls = neighbours.map((s) => s.preview.full).join(' ');
  useEffect(() => {
    const timer = setTimeout(() => {
      for (const s of neighbours) {
        preload(s.preview.full);
        shotExif(s).catch(() => {});
      }
    }, PRELOAD_DELAY);
    return () => clearTimeout(timer);
  }, [urls]);
}

/** Size of the element `ref` in CSS pixels, `{ w, h }`, or null before it is laid out. */
export function useSize(ref) {
  const [size, setSize] = useState(null);
  useEffect(() => {
    const el = ref.current;
    const measure = () => setSize((old) => (old && old.w === el.clientWidth && old.h === el.clientHeight
      ? old : { w: el.clientWidth, h: el.clientHeight }));
    measure();
    if (typeof ResizeObserver === 'undefined') {
      addEventListener('resize', measure);
      return () => removeEventListener('resize', measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return size;
}

function Row({ label, value, title }) {
  return value == null || value === '' ? null : html`<dt>${label}</dt><dd title=${title}>${value}</dd>`;
}

/**
 * Sharpness of `shot` as text, with its share of `best` (the sharpness of its group's sharpest shot, when the group
 * has more than one): `1 234 · 82 % ryhmän terävimmästä`. Null for a shot that could not be analysed.
 */
export function sharpText(shot, best) {
  if (shot.error) return null;
  const sharp = fmt(Math.round(shot.sharp));
  if (!(best > 0)) return sharp;
  if (shot.sharp >= best) return `${sharp} · ryhmän terävin`;
  return `${sharp} · ${Math.round((shot.sharp / best) * 100)} % ryhmän terävimmästä`;
}

/** The shot's metadata. `best`: sharpness of its group's sharpest shot, when the group has more than one. */
function Meta({ shot, best, position, exif, exifError }) {
  const auto = shot.auto_verdict;
  const suggestion = SUGGESTION[auto];
  const decided = shot.user_verdict;
  const sharp = sharpText(shot, best);
  const e = exif ?? {};
  const loading = exif == null && !exifError;
  const exifRow = (label, value) => html`<${Row} label=${label} value=${loading ? '…' : value} />`;
  return html`<aside class=${`meta verdict-${auto ?? 'none'}`} aria-label="Kuvan tiedot">
    <p class="position muted">${position}</p>
    <dl>
      <dt>Oma päätös</dt>
      <dd class=${`decided ${decided ?? ''}`}>
        ${decided ? DECISIONS[decided] : html`<span class="muted">
          ei päätöstä${suggestion ? `, ehdotus ${DECISIONS[suggestion]}` : ''}</span>`}
        ${shot.rating != null && ' '}<${Stars} rating=${shot.rating} />
      </dd>
      <dt>Arvio</dt>
      <dd><span class="auto">${VERDICTS[auto] ?? 'ei arviota'}</span>${suggestion && ` → ${DECISIONS[suggestion]}`}
        ${shot.reason && html`<div class="reason">${shot.reason}</div>`}</dd>
      ${shot.error && html`<${Row} label="Virhe" value=${shot.error} />`}
      <${Row} label="Terävyys" value=${sharp} />
      ${!shot.error && html`
        <${Row} label="Puhki palaneet" value=${fmtShare(shot.clip_hi)} title="ylivalottuneiden pikselien osuus" />
        <${Row} label="Tukkoiset varjot" value=${fmtShare(shot.clip_lo)} title="täysin mustien pikselien osuus" />`}
      <${Row} label="Kuvattu" value=${shot.taken > 0 ? seconds.format(new Date(shot.taken * 1000)) : null} />
      ${exif && Object.keys(exif).length === 0 && html`<dt>Kuvaustiedot</dt><dd class="muted">ei EXIF-tietoja</dd>`}
      ${exifRow('Valotusaika', fmtExposure(e.exposure_time))}
      ${exifRow('Aukko', fmtAperture(e.f_number))}
      ${exifRow('Herkkyys', fmtIso(e.iso))}
      ${exifRow('Polttoväli', fmtFocal(e.focal_length, e.focal_length_35mm))}
      ${exifRow('Kamera', e.camera)}
      ${exifRow('Objektiivi', e.lens)}
      <dt>Tiedostot</dt>
      <dd class="files">${shot.files.map((f) => html`<div key=${f} title=${f}>${f.split('/').pop()}</div>`)}</dd>
    </dl>
    ${exifError && html`<p class="error">Kuvaustietoja ei saatu: ${exifError}</p>`}
  </aside>`;
}

/** Natural size `{ src, w, h }` of a loaded image, from its `load` event. */
export const sizeOf = (src, e) => ({ src, w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight });

/**
 * The loupe of `shot`. `best`: sharpness of its group's sharpest shot (see `groupBest`), `position`: where it is, as
 * text, `neighbours`: the shots ←/→ lead to, whose previews load ahead. `keys.current` is set to the loupe's own key
 * handler (Z), which the event view calls.
 *
 * Fitted at first. Z or a click zooms in (see `zoomScale`) around the pointer (beside the image: its nearest edge;
 * Z without the pointer over the stage: the centre), and back; zoomed in, the image is dragged to pan. The zoom stays
 * when moving to another shot; until the next full preview has loaded, its grid preview stands in at the same place.
 */
export function Loupe({ shot, best, position, neighbours, keys }) {
  const stageRef = useRef(null);
  const stage = useSize(stageRef);
  const [zoom, setZoom] = useState(null);
  const [img, setImg] = useState(null);           // { src, w, h } of the full preview once loaded, or { src, error }
  const [grid, setGrid] = useState(null);         // { src, w, h } of the grid preview once loaded
  const last = useRef(null);                       // size of the last full preview loaded
  const [dragging, setDragging] = useState(false);
  const pointer = useRef(null);                    // pointer position over the stage, null when not over it
  const drag = useRef(null);
  const toggled = useRef(-Infinity);               // time of the last click that toggled the zoom
  const { exif, error: exifError } = useExif(shot);
  usePreloadNeighbours(neighbours);

  const src = shot.preview.full;
  const failed = img?.src === src && !!img.error;
  const loaded = img?.src === src && !img.error ? img : null;
  const size = failed ? null : loaded ?? guessSize(grid?.src === shot.preview.grid ? grid : null, last.current);
  const dpr = globalThis.devicePixelRatio || 1;
  const box = size && stage ? layout(stage, size, zoom, dpr) : null;
  const canZoom = !!box && (box.zoomed || zoomScale(stage, size, dpr) != null);

  /** Zoom in at `point`, or back to fitted. False when nothing changes. */
  const toggle = (point) => {
    if (zoom) {
      setZoom(null);
      return true;
    }
    if (!canZoom) return false;
    setZoom(zoomAt(stage, size, point, dpr));
    return true;
  };
  useEffect(() => {
    // scrolling (narrow screens) moves the stage under a pointer that has not moved: Z zooms around the centre then
    const forget = () => { if (!drag.current) pointer.current = null; };
    addEventListener('scroll', forget, { passive: true });
    return () => removeEventListener('scroll', forget);
  }, []);
  useLayoutEffect(() => {
    keys.current = (action) => action.type === 'zoom' && toggle(pointer.current);
    return () => { keys.current = null; };
  });

  const at = (e) => {
    const r = stageRef.current.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, from: box?.zoomed ? zoom : null, moved: false };
  };
  const onPointerMove = (e) => {
    pointer.current = at(e);
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < CLICK_SLOP) return;
    d.moved = true;
    if (d.from && size && stage) {
      setDragging(true);
      setZoom(pan(stage, size, d.from, dx, dy, dpr));
    }
  };
  const onPointerUp = (e) => {
    const d = drag.current;
    drag.current = null;
    setDragging(false);
    // the second click of a double-click (which opens the loupe from the grid) would zoom straight back
    if (!d || d.moved || e.timeStamp - toggled.current < DOUBLE_CLICK) return;
    if (toggle(at(e))) toggled.current = e.timeStamp;
  };
  const onPointerCancel = () => {
    drag.current = null;
    setDragging(false);
  };

  const placed = box && { width: `${size.w * box.s}px`, height: `${size.h * box.s}px`,
                          transform: `translate(${box.x}px, ${box.y}px)` };
  const decided = shot.user_verdict;
  const cls = `stage${box?.zoomed ? ' zoomed' : ''}${canZoom ? '' : ' still'}${dragging ? ' dragging' : ''}`
    + `${decided ? ` ${decided}` : ''}`;
  let title;
  if (box?.zoomed) title = 'Vedä siirtääksesi, klikkaa sovittaaksesi (Z)';
  else if (canZoom) title = `Klikkaa: ${zoomLabel(zoomScale(stage, size, dpr), dpr)} (Z)`;
  return html`<div class="loupe">
    <div ref=${stageRef} class=${cls}
        onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onPointerUp=${onPointerUp}
        onPointerCancel=${onPointerCancel} onPointerLeave=${() => { if (!drag.current) pointer.current = null; }}
        title=${title}>
      <img key=${shot.preview.grid} class=${`${placed ? 'placed' : 'fitted'}${loaded ? ' covered' : ''}`}
        style=${placed || undefined}
        src=${shot.preview.grid} alt="" draggable="false"
        onLoad=${(e) => setGrid(sizeOf(shot.preview.grid, e))} />
      ${!failed && html`<img key=${src} class=${placed ? 'placed' : 'fitted'} style=${placed || undefined} src=${src}
        alt=${shot.name} draggable="false" decoding="async"
        onLoad=${(e) => {
          last.current = sizeOf(src, e);
          setImg(last.current);
        }}
        onError=${() => setImg({ src, error: true })} />`}
      <${DecisionBadge} decided=${decided} />
      ${failed && html`<p class="stage-label error">Täysikokoista esikatselua ei voi näyttää; näkyvissä on vain
        ruudukon pikkukuva, josta terävyyttä ei voi arvioida.</p>`}
      ${!failed && (box?.zoomed || !loaded) && html`<span class="stage-label">
        ${[box?.zoomed && zoomLabel(box.s, dpr), !loaded && 'ladataan…'].filter(Boolean).join(' · ')}</span>`}
    </div>
    <${Meta} shot=${shot} best=${best} position=${position} exif=${exif} exifError=${exifError} />
  </div>`;
}
