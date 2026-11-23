// Loupe: one shot at full preview size, fitted or at 100 %, with its metadata beside it.
import { api } from './api.js';
import { DECISIONS, VERDICTS, stars } from './grid.js';
import { SUGGESTION } from './review.js';
import { fmt, html, preload, useEffect, useRef, useState } from './ui.js';

const PRELOAD_DELAY = 150;        // ms the loupe rests on a shot before its neighbours load (not while a key is held)
const CLICK_SLOP = 4;             // px a press may move and still count as a click, not a drag

// ---------------------------------------------------------------------------------------------------------------- //
// Where the image goes. `stage` and `img` are sizes `{ w, h }`: the stage in CSS pixels, the image in its own pixels.
// `zoom` is null when fitted, else the image point at the stage's centre as fractions of the image `{ cx, cy }`: it
// stays put from shot to shot, so the same spot of the next shot of a burst shows at 100 %. `dpr` is the device
// pixel ratio: at 100 % one image pixel is one screen pixel.

/** On one axis: where an image of `len` starts in a stage of `size`, centred on fraction `c`, never leaving a gap. */
function place(size, len, c) {
  if (len <= size) return (size - len) / 2;
  return Math.min(0, Math.max(size - len, size / 2 - c * len));
}

/** Scale (CSS px per image pixel) and top-left corner `{ s, x, y }` of the image in the stage. Fitting never enlarges. */
export function layout(stage, img, zoom, dpr = 1) {
  if (!zoom) {
    const s = Math.min(stage.w / img.w, stage.h / img.h, 1 / dpr);
    return { s, x: (stage.w - img.w * s) / 2, y: (stage.h - img.h * s) / 2 };
  }
  const s = 1 / dpr;
  return { s, x: place(stage.w, img.w * s, zoom.cx), y: place(stage.h, img.h * s, zoom.cy) };
}

/** `zoom` as far as the image can actually move: the centre that `layout` shows for it. */
export function clampZoom(stage, img, zoom, dpr = 1) {
  const { s, x, y } = layout(stage, img, zoom, dpr);
  return { cx: (stage.w / 2 - x) / (img.w * s), cy: (stage.h / 2 - y) / (img.h * s) };
}

const clamp01 = (v) => Math.min(Math.max(v, 0), 1);

/** 100 % with the image point under `point` (stage coordinates, fitted) staying under it; `point` null: the centre. */
export function zoomAt(stage, img, point, dpr = 1) {
  const fit = layout(stage, img, null, dpr);
  const p = point ?? { x: stage.w / 2, y: stage.h / 2 };
  const fx = clamp01((p.x - fit.x) / (img.w * fit.s));
  const fy = clamp01((p.y - fit.y) / (img.h * fit.s));
  const s = 1 / dpr;
  return clampZoom(stage, img, { cx: fx + (stage.w / 2 - p.x) / (img.w * s), cy: fy + (stage.h / 2 - p.y) / (img.h * s) },
    dpr);
}

/** `zoom` dragged by `dx`, `dy` CSS pixels (the image follows the pointer). */
export function pan(stage, img, zoom, dx, dy, dpr = 1) {
  const s = 1 / dpr;
  const from = clampZoom(stage, img, zoom, dpr);
  return clampZoom(stage, img, { cx: from.cx - dx / (img.w * s), cy: from.cy - dy / (img.h * s) }, dpr);
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
function useSize(ref) {
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

/** The shot's metadata. `best`: sharpness of its group's sharpest shot, when the group has more than one. */
function Meta({ shot, best, position, exif, exifError }) {
  const auto = shot.auto_verdict;
  const suggestion = SUGGESTION[auto];
  const decided = shot.user_verdict;
  let sharp = null;
  if (!shot.error) {
    sharp = fmt(Math.round(shot.sharp));
    if (best > 0) {
      sharp += shot.sharp >= best ? ' · ryhmän terävin' : ` · ${Math.round((shot.sharp / best) * 100)} % ryhmän terävimmästä`;
    }
  }
  const e = exif ?? {};
  return html`<aside class=${`meta verdict-${auto ?? 'none'}`} aria-label="Kuvan tiedot">
    <p class="position muted">${position}</p>
    <dl>
      <dt>Oma päätös</dt>
      <dd class=${`decided ${decided ?? ''}`}>
        ${decided ? DECISIONS[decided] : html`<span class="muted">
          ei päätöstä${suggestion ? `, ehdotus ${DECISIONS[suggestion]}` : ''}</span>`}
        ${shot.rating != null && html` <span class="stars" title=${`${shot.rating} tähteä`}>${stars(shot.rating)}</span>`}
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
      <${Row} label="Valotusaika" value=${fmtExposure(e.exposure_time)} />
      <${Row} label="Aukko" value=${fmtAperture(e.f_number)} />
      <${Row} label="Herkkyys" value=${fmtIso(e.iso)} />
      <${Row} label="Polttoväli" value=${fmtFocal(e.focal_length, e.focal_length_35mm)} />
      <${Row} label="Kamera" value=${e.camera} />
      <${Row} label="Objektiivi" value=${e.lens} />
      <dt>Tiedostot</dt>
      <dd class="files">${shot.files.map((f) => html`<div key=${f} title=${f}>${f.split('/').pop()}</div>`)}</dd>
    </dl>
    ${exifError && html`<p class="error">Kuvaustietoja ei saatu: ${exifError}</p>`}
  </aside>`;
}

/**
 * The loupe of `shot`. `best`: sharpness of its group's sharpest shot (see `groupBest`), `position`: where it is, as
 * text, `neighbours`: the shots ←/→ lead to, whose previews load ahead. `keys.current` is set to the loupe's own key
 * handler (Z), which the event view calls.
 *
 * Fitted at first. Z or a click goes to 100 % around the pointer (Z without the pointer over the image: around the
 * centre), and back; at 100 % the image is dragged to pan. The zoom stays when moving to another shot.
 */
export function Loupe({ shot, best, position, neighbours, keys }) {
  const stageRef = useRef(null);
  const stage = useSize(stageRef);
  const [zoom, setZoom] = useState(null);
  const [img, setImg] = useState(null);           // { src, w, h } of the full preview once loaded, or { src, error }
  const [dragging, setDragging] = useState(false);
  const pointer = useRef(null);                    // pointer position over the stage, null when not over it
  const drag = useRef(null);
  const { exif, error: exifError } = useExif(shot);
  usePreloadNeighbours(neighbours);

  const src = shot.preview.full;
  const loaded = img?.src === src && !img.error ? img : null;
  const dpr = globalThis.devicePixelRatio || 1;
  const box = loaded && stage ? layout(stage, loaded, zoom, dpr) : null;

  const toggle = (point) => {
    if (!loaded || !stage) return;
    setZoom(zoom ? null : zoomAt(stage, loaded, point, dpr));
  };
  keys.current = (action) => {
    if (action.type !== 'zoom') return false;
    toggle(pointer.current);
    return true;
  };
  useEffect(() => () => { keys.current = null; }, []);

  const at = (e) => {
    const r = stageRef.current.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };
  const onPointerDown = (e) => {
    if (e.button !== 0) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, from: zoom, moved: false };
  };
  const onPointerMove = (e) => {
    pointer.current = at(e);
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < CLICK_SLOP) return;
    d.moved = true;
    if (d.from && loaded && stage) {
      setDragging(true);
      setZoom(pan(stage, loaded, d.from, dx, dy, dpr));
    }
  };
  const onPointerUp = (e) => {
    const d = drag.current;
    drag.current = null;
    setDragging(false);
    if (d && !d.moved) toggle(at(e));
  };
  const onPointerCancel = () => {
    drag.current = null;
    setDragging(false);
  };

  const placed = box && { width: `${loaded.w * box.s}px`, height: `${loaded.h * box.s}px`,
                          transform: `translate(${box.x}px, ${box.y}px)` };
  const decided = shot.user_verdict;
  const cls = `stage${zoom ? ' zoomed' : ''}${dragging ? ' dragging' : ''}${decided ? ` ${decided}` : ''}`;
  return html`<div class="loupe">
    <div ref=${stageRef} class=${cls}
        onPointerDown=${onPointerDown} onPointerMove=${onPointerMove} onPointerUp=${onPointerUp}
        onPointerCancel=${onPointerCancel} onPointerLeave=${() => { if (!drag.current) pointer.current = null; }}
        title=${zoom ? 'Vedä siirtääksesi, klikkaa sovittaaksesi (Z)' : 'Klikkaa: 100 % (Z)'}>
      <img class=${placed ? 'placed' : 'fitted'} style=${placed || undefined} src=${shot.preview.grid} alt=""
        draggable="false" />
      <img key=${src} class=${placed ? 'placed' : 'fitted'} style=${placed || undefined} src=${src} alt=${shot.name}
        draggable="false" decoding="async"
        onLoad=${(e) => setImg({ src, w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })}
        onError=${() => setImg({ src, error: true })} />
      ${img?.src === src && img.error && html`<p class="stage-error">Esikatselua ei voi näyttää.</p>`}
      ${decided && html`<span class=${`decision ${decided}`} title=${`oma päätös: ${DECISIONS[decided]}`}>
        ${decided === 'keep' ? '✓' : '✕'}</span>`}
      ${zoom && html`<span class="zoom-label">100 %</span>`}
    </div>
    <${Meta} shot=${shot} best=${best} position=${position} exif=${exif} exifError=${exifError} />
  </div>`;
}
