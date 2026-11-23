// Asetukset: the grouping and judging thresholds, calibrated against the event's sharpness distribution.
import { api } from './api.js';
import { fmt, html, useEffect, useState } from './ui.js';

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
const W = 480;
const H = 120;
const PAD = { top: 26, right: 8, bottom: 18, left: 8 };

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

/** Counts of `values` in `bins` equal steps of the axis from 0 to `hi`: `[{ lo, hi, count }]`. */
export function histogram(values, hi, bins = BINS) {
  const out = Array.from({ length: bins }, (_, i) => ({ lo: valueAt(i / bins, hi), hi: valueAt((i + 1) / bins, hi),
                                                        count: 0 }));
  for (const v of values) out[Math.min(Math.floor(position(v, hi) * bins), bins - 1)].count += 1;
  return out;
}

/** Upper end of the axis (and of the Terävyysraja slider): the sharpest shot or the thresholds, rounded up. */
export function axisEnd(values, settings, med) {
  const top = Math.max(1, ...values, settings.abs_blur, med == null ? 0 : settings.rel_blur * med);
  const step = 10 ** Math.floor(Math.log10(top));
  return Math.ceil(top / step) * step;
}

/** The fields of `draft` that differ from `saved`: the body of `PUT /api/settings`. */
export function changes(draft, saved) {
  return Object.fromEntries(Object.entries(draft).filter(([k, v]) => saved[k] !== v));
}

const count = (n) => `${fmt(n)} ${n === 1 ? 'kuva' : 'kuvaa'}`;
const round = (v) => fmt(Math.round(v * 10) / 10);

/**
 * Sharpness of the event's shots on a logarithmic axis, with vertical lines at `abs_blur` and `rel_blur` × median.
 * The bars under the larger of them (the shots judged blurry) are red.
 */
export function SharpnessHistogram({ values, settings }) {
  const med = median(values);
  const hi = axisEnd(values, settings, med);
  const limit = blurLimit(settings, med);
  const bins = histogram(values, hi);
  const most = Math.max(1, ...bins.map((b) => b.count));
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const x = (v) => PAD.left + position(v, hi) * plotW;
  const bw = plotW / bins.length;
  const ticks = [0];
  for (let t = 1; t <= hi; t *= 10) ticks.push(t);
  const lines = [
    ['abs', settings.abs_blur, `raja ${round(settings.abs_blur)}`, 'Terävyysraja'],
    ...(med == null ? [] : [['rel', settings.rel_blur * med, `${fmt(settings.rel_blur)} × mediaani = ${round(settings.rel_blur * med)}`,
      `Osuus mediaanista: ${fmt(settings.rel_blur)} × ${round(med)} = ${round(settings.rel_blur * med)}`]]),
  ];
  const below = values.filter((v) => v < limit).length;
  return html`<figure class="sharpness">
    <svg viewBox=${`0 0 ${W} ${H}`} role="img"
        aria-label=${`Terävyysjakauma: ${count(values.length)}, ${count(below)} alle rajan ${round(limit)}`}>
      ${bins.map((b, i) => b.count > 0 && html`<rect key=${i} class=${b.hi <= limit ? 'bar under' : 'bar'}
          x=${PAD.left + i * bw + 1} width=${Math.max(bw - 2, 1)}
          y=${PAD.top + plotH - (b.count / most) * plotH} height=${(b.count / most) * plotH} rx="2">
        <title>terävyys ${round(b.lo)}–${round(b.hi)}: ${count(b.count)}</title>
      </rect>`)}
      <line class="axis" x1=${PAD.left} x2=${W - PAD.right} y1=${PAD.top + plotH} y2=${PAD.top + plotH} />
      ${ticks.map((t) => html`<text key=${t} class="tick" x=${x(t)} y=${H - 4}
          text-anchor=${t === 0 ? 'start' : 'middle'}>${fmt(t)}</text>`)}
      ${med != null && html`<line class="median" x1=${x(med)} x2=${x(med)} y1=${PAD.top} y2=${PAD.top + plotH}>
        <title>mediaani ${round(med)}</title></line>`}
      ${lines.map(([key, v, label, title], i) => {
        const at = x(v);
        const right = at > W / 2;          // the label on the side with room
        return html`<g key=${key} class=${`limit ${key}`}>
          <title>${title}</title>
          <line x1=${at} x2=${at} y1=${PAD.top - 4} y2=${PAD.top + plotH} />
          <text x=${right ? at - 4 : at + 4} y=${10 + i * 11} text-anchor=${right ? 'end' : 'start'}>${label}</text>
        </g>`;
      })}
    </svg>
    <figcaption class="muted">
      Terävyys (logaritminen asteikko)${med != null && `, mediaani ${round(med)}`}.
      Rajan ${round(limit)} alle jää <span class="candidates">${count(below)}</span>, jotka arvioidaan
      epäteräviksi.
    </figcaption>
  </figure>`;
}

/**
 * The thresholds of every event, with the sharpness of this one (`data` of `/api/events/<id>`). The histogram's
 * lines follow the sliders at once; Käytä stores them, and the server groups and judges every analysed event again
 * from the stored metrics. `onApplied(answer)` then reloads the event.
 */
export function SettingsPanel({ data, onApplied, onClose }) {
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

  return html`<section class="settings-panel" aria-label="Asetukset">
    <header>
      <h2>Asetukset</h2>
      <span class="muted">koskevat kaikkia tapahtumia</span>
      <button class="dismiss" onClick=${onClose} title="Sulje" aria-label="Sulje">×</button>
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
        <p class="muted note">Omat päätöksesi säilyvät, vaikka kuvan ryhmä tai arvio muuttuisi.</p>
      </div>
    </div>
  </section>`;
}
