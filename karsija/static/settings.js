// Asetukset: the grouping and judging thresholds, calibrated against the event's sharpness distribution.
import { api } from './api.js';
import { fmt, html, useEffect, useLayoutEffect, useRef, useState } from './ui.js';

/** The sliders: `[key, label, min, max, step, title]`; `max` grows to fit a stored value or the event's sharpness. */
export const SLIDERS = [
  ['abs_blur', 'Terävyysraja', 0, 100, 0.5, 'tätä pienempi terävyys = epäterävä'],
  ['rel_blur', 'Osuus mediaanista', 0, 1, 0.01, 'alle tämä osuus tapahtuman mediaanista = epäterävä'],
  ['gap', 'Ryhmän aikaväli (s)', 0, 30, 0.5, 'enintään näin monta sekuntia peräkkäisten kuvien välillä samassa ryhmässä'],
  ['hamming', 'Samankaltaisuus (dHash-ero)', 0, 128, 1, 'suurin dHash-ero (0–256) samankaltaisille kuville'],
  ['sim_keep', 'Pidä lähes yhtä terävät', 0, 1, 0.05,
    'pidä ryhmän kuva, jonka terävyys on vähintään tämä osuus parhaasta (0 = vain paras)'],
];
const BINS = 24;
const H = 140;                       // px; the width is the figure's own, so the text keeps its size
const PAD = { top: 32, right: 8, bottom: 20, left: 8 };

/** Median as numpy computes it (the mean of the middle two of an even count); null for no values. */
export function median(values) {
  if (values.length === 0) return null;
  const v = [...values].sort((a, b) => a - b);
  const mid = v.length >> 1;
  return v.length % 2 ? v[mid] : (v[mid - 1] + v[mid]) / 2;
}

/** Sharpness under which a shot is judged blurry: the larger of `abs_blur` and `rel_blur` × median. */
export function blurLimit(settings, med) {
  return Math.max(settings.abs_blur, med == null ? 0 : settings.rel_blur * med);
}

/** Where `v` falls on the axis from 0 to `hi`, as a fraction (0–1). Logarithmic: sharpness spans decades. */
export function position(v, hi) {
  return Math.min(Math.max(Math.log10(1 + Math.max(v, 0)) / Math.log10(1 + hi), 0), 1);
}

/** Inverse of `position`. */
export function valueAt(fraction, hi) {
  return 10 ** (fraction * Math.log10(1 + hi)) - 1;
}

/**
 * Counts of `values` in `bins` equal steps of the axis from 0 to `hi`: `[{ lo, hi, count, under }]`, `under` the
 * values below `limit` (judged blurry), so a bin that holds the limit shows both parts.
 */
export function histogram(values, hi, bins = BINS, limit = 0) {
  const out = Array.from({ length: bins }, (_, i) => ({ lo: valueAt(i / bins, hi), hi: valueAt((i + 1) / bins, hi),
                                                        count: 0, under: 0 }));
  for (const v of values) {
    const bin = out[Math.min(Math.floor(position(v, hi) * bins), bins - 1)];
    bin.count += 1;
    if (v < limit) bin.under += 1;
  }
  return out;
}

/** Upper end of the axis (and of the Terävyysraja slider): the sharpest shot or the thresholds, rounded up. */
export function axisEnd(values, settings, med) {
  const top = Math.max(1, ...values, settings.abs_blur, med == null ? 0 : settings.rel_blur * med);
  const step = 10 ** Math.floor(Math.log10(top));
  return Math.ceil(top / step) * step;
}

/** Axis labels: 0 and the powers of ten up to `hi`. */
export function ticks(hi) {
  const out = [0];
  for (let t = 1; t <= hi && out.length < 20; t *= 10) out.push(t);
  return out;
}

/** The fields of `draft` that differ from `saved`: the body of `PUT /api/settings`. */
export function changes(draft, saved) {
  return Object.fromEntries(Object.entries(draft).filter(([k, v]) => saved[k] !== v));
}

const count = (n) => `${fmt(n)} ${n === 1 ? 'kuva' : 'kuvaa'}`;
const round = (v) => fmt(Math.round(v * 10) / 10);

/** Width of the element of `ref` in CSS pixels, following its changes; `initial` until it is laid out. */
function useWidth(ref, initial) {
  const [width, setWidth] = useState(initial);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => { if (el.clientWidth > 0) setWidth(el.clientWidth); };
    measure();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return width;
}

/**
 * Sharpness of the event's shots on a logarithmic axis, with vertical lines at `abs_blur` and `rel_blur` × median.
 * The part of each bar under the larger of them (the shots judged blurry) is red.
 */
export function SharpnessHistogram({ values, settings }) {
  const figure = useRef(null);
  const W = useWidth(figure, 480);
  const med = median(values);
  const hi = axisEnd(values, settings, med);
  const limit = blurLimit(settings, med);
  const bins = histogram(values, hi, BINS, limit);
  const most = Math.max(1, ...bins.map((b) => b.count));
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const base = PAD.top + plotH;
  const x = (v) => PAD.left + position(v, hi) * plotW;
  const bw = plotW / bins.length;
  const tall = (n) => (n / most) * plotH;
  const lines = [
    ['abs', settings.abs_blur, `raja ${round(settings.abs_blur)}`, 'Terävyysraja'],
    ...(med == null ? [] : [['rel', settings.rel_blur * med,
      `${fmt(settings.rel_blur)} × mediaani = ${round(settings.rel_blur * med)}`,
      `Osuus mediaanista: ${fmt(settings.rel_blur)} × ${round(med)} = ${round(settings.rel_blur * med)}`]]),
  ];
  const below = values.filter((v) => v < limit).length;
  return html`<figure class="sharpness" ref=${figure}>
    <svg width=${W} height=${H} viewBox=${`0 0 ${W} ${H}`} role="img"
        aria-label=${`Terävyysjakauma: ${count(values.length)}, ${count(below)} alle rajan ${round(limit)}`}>
      ${bins.map((b, i) => {
        if (b.count === 0) return null;
        const left = PAD.left + i * bw + 1;
        const width = Math.max(bw - 2, 1);
        const red = tall(b.under);
        const grey = tall(b.count - b.under);
        const gap = red > 0 && grey > 0 ? 2 : 0;       // a surface gap between the two parts of a split bar
        return html`<g key=${i}>
          <title>terävyys ${round(b.lo)}–${round(b.hi)}: ${count(b.count)}${b.under > 0
            ? `, ${b.under === b.count ? 'kaikki' : fmt(b.under)} alle rajan` : ''}</title>
          ${red > 0 && html`<rect class="bar under" x=${left} width=${width} y=${base - red} height=${red} rx="2" />`}
          ${grey > 0 && html`<rect class="bar" x=${left} width=${width} y=${base - red - grey}
            height=${Math.max(grey - gap, 1)} rx="2" />`}
        </g>`;
      })}
      <line class="axis" x1=${PAD.left} x2=${W - PAD.right} y1=${base} y2=${base} />
      ${ticks(hi).map((t) => {
        const at = x(t);
        const anchor = t === 0 ? 'start' : at > W - PAD.right - 24 ? 'end' : 'middle';
        return html`<text key=${t} class="tick" x=${anchor === 'end' ? W - PAD.right : at} y=${H - 5}
          text-anchor=${anchor}>${fmt(t)}</text>`;
      })}
      ${med != null && html`<line class="median" x1=${x(med)} x2=${x(med)} y1=${PAD.top + plotH / 3} y2=${base}>
        <title>mediaani ${round(med)}</title></line>`}
      ${lines.map(([key, v, label, title], i) => {
        const at = x(v);
        const right = at > W / 2;          // the label on the side with room
        return html`<g key=${key} class=${`limit ${key}`}>
          <title>${title}</title>
          <line x1=${at} x2=${at} y1=${PAD.top - 4} y2=${base} />
          <text x=${right ? at - 4 : at + 4} y=${12 + i * 14} text-anchor=${right ? 'end' : 'start'}>${label}</text>
        </g>`;
      })}
    </svg>
    <figcaption class="muted">
      Terävyys (logaritminen asteikko)${med != null && `, mediaani ${round(med)} (pisteviiva)`}.
      Rajan ${round(limit)} alle jää <span class="under-count">${count(below)}</span>, jotka arvioidaan
      epäteräviksi (punaiset).
    </figcaption>
  </figure>`;
}

/**
 * The thresholds of every event, with the sharpness of this one (`data` of `/api/events/<id>`). The histogram's
 * lines follow the sliders at once; Käytä stores them, and the server groups and judges every analysed event again
 * from the stored metrics. `onApplied(answer)` then reloads the event. `hidden`: closed, keeping a changed draft.
 */
export function SettingsPanel({ data, hidden, onApplied, onClose }) {
  const saved = data.settings;
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  // stored settings that changed (here or elsewhere) replace the draft; a reload of the same ones keeps it
  useEffect(() => { setDraft(saved); }, [JSON.stringify(saved)]);

  const values = data.sharpness;
  const med = median(values);
  const body = changes(draft, saved);
  const dirty = Object.keys(body).length > 0;
  const defaults = data.default_settings;
  const atDefaults = defaults && Object.keys(changes(draft, defaults)).length === 0;

  const apply = async () => {
    setBusy(true);
    setError(null);
    try {
      onApplied(await api('/settings', { method: 'PUT', body }));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const max = (key, base) => (key === 'abs_blur' ? Math.max(base, axisEnd(values, draft, med))
    : Math.max(base, draft[key]));

  return html`<section class="settings-panel" aria-label="Asetukset" hidden=${hidden}>
    <header>
      <h2>Asetukset</h2>
      <span class="muted">koskevat kaikkia tapahtumia</span>
      <button class="dismiss" onClick=${onClose} title="Sulje (Esc)" aria-label="Sulje">×</button>
    </header>
    <div class="settings-body">
      ${values.length > 0
        ? html`<${SharpnessHistogram} values=${values} settings=${draft} />`
        : html`<p class="muted">Tapahtumassa ei ole analysoituja kuvia, joiden terävyyttä näyttää.</p>`}
      <div class="sliders">
        ${SLIDERS.map(([key, label, min, base, step, title]) => html`<label key=${key} title=${title}>
          <span>${label}</span>
          <input type="range" min=${min} max=${max(key, base)} step=${step} value=${draft[key]} disabled=${busy}
            onInput=${(e) => setDraft((d) => ({ ...d, [key]: Number(e.currentTarget.value) }))} />
          <output class=${saved[key] !== draft[key] ? 'changed' : ''}>${fmt(draft[key])}</output>
        </label>`)}
        <div class="buttons">
          <button class="primary" disabled=${!dirty || busy} onClick=${apply}
            title="Tallenna ja arvioi kaikki analysoidut tapahtumat uudelleen (kuvia ei analysoida uudelleen)">
            ${busy ? 'Arvioidaan…' : 'Käytä'}</button>
          <button disabled=${!dirty || busy} onClick=${() => setDraft(saved)}>Peru muutokset</button>
          <button disabled=${atDefaults || busy} onClick=${() => setDraft({ ...draft, ...defaults })}>Oletukset</button>
        </div>
        ${error && html`<p class="error">Asetuksia ei tallennettu: ${error}</p>`}
        <p class="muted note">Omat päätöksesi säilyvät, vaikka kuvan ryhmä tai arvio muuttuisi. Muutokset tulevat
          voimaan vasta Käytä-painikkeesta; Oletukset palauttaa oletusarvot.</p>
      </div>
    </div>
  </section>`;
}
