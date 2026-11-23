// Toteuta (carry out an event's decisions in the library) and its history, from which an apply is restored.
import { api, useApi } from './api.js';
import { fmt, fmtTime, html, useCallback, useEffect, useRef, useState } from './ui.js';

/** `1 kuva`, `3 kuvaa`. */
const count = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;

export const NOTHING_TO_DO = 'Ei toteutettavaa: yhtään kuvaa ei ole hylätty, eikä tehdyistä päätöksistä ole '
  + 'kirjoitettavaa XMP:hen. Tee ensin päätöksiä (P pidä, X hylkää).';

/**
 * What an apply plan would do, as the confirmation states it:
 * `Siirretään 3 kuvaa (10 tiedostoa), kirjoitetaan 8 XMP:tä (2 uutta, 6 päivitettyä), 1 tarkastamatta.`
 * `empty`: there is nothing to do. `ready`: there is something to do and no conflict prevents it.
 */
export function planSummary(plan) {
  const files = plan.move.reduce((n, m) => n + m.files.length, 0);
  const created = plan.xmp_create.length;
  const updated = plan.xmp_update.length;
  const xmps = created + updated;
  const parts = [
    `siirretään ${count(plan.move.length, 'kuva', 'kuvaa')}${files ? ` (${count(files, 'tiedosto', 'tiedostoa')})` : ''}`,
    `kirjoitetaan ${count(xmps, 'XMP', 'XMP:tä')}${xmps ? ` (${fmt(created)} uutta, ${fmt(updated)} päivitettyä)` : ''}`,
    `${fmt(plan.unreviewed)} tarkastamatta`,
  ];
  const text = parts.join(', ');
  const empty = plan.move.length + xmps === 0;
  return {
    text: `${text[0].toUpperCase()}${text.slice(1)}.`,
    empty,
    ready: plan.conflicts.length === 0 && !empty,
  };
}

/**
 * One apply of the history: `siirretty 3 kuvaa, kirjoitettu 8 XMP:tä`. An apply that did not finish says how far
 * it got: `keskeytyi: tehty 4 / 11 toimenpidettä`.
 */
export function applyText(a) {
  if (a.status === 'failed' || a.status === 'running') {
    return `${a.status === 'failed' ? 'keskeytyi' : 'kesken'}: tehty ${fmt(a.done)} / ${count(a.ops, 'toimenpide',
      'toimenpiteestä')}`;
  }
  return `siirretty ${count(a.shots_moved, 'kuva', 'kuvaa')}, `
    + `kirjoitettu ${count(a.xmp_created + a.xmp_updated, 'XMP', 'XMP:tä')}`;
}

/**
 * The answer of a restore as a message: `{ text, dir, warn }`. `dir`: the folder the kept files went to, null when
 * none did; `warn` when moved files were not found.
 */
export function restoreText(res) {
  const parts = ['Palautettu.'];
  if (res.missing.length) {
    parts.push(`${count(res.missing.length, 'siirretty tiedosto puuttui', 'siirrettyä tiedostoa puuttui')}: `
      + `${res.missing.join(', ')}.`);
  }
  if (res.changed.length) {
    parts.push(`${count(res.changed.length, 'sivutiedosto oli muokattu', 'sivutiedostoa oli muokattu')} `
      + 'toteutuksen jälkeen; muokattu versio kopioitiin talteen ennen palautusta.');
  }
  if (res.created.length) parts.push(`${count(res.created.length, 'toteutuksen luoma XMP siirrettiin', 'toteutuksen luomaa XMP:tä siirrettiin')} talteen.`);
  if (res.log) parts.push('Toteutuksen loki siirrettiin talteen.');
  const kept = res.created.length || res.changed.length || res.log;
  return { text: parts.join(' '), dir: kept ? res.undone_dir : null, warn: res.missing.length > 0 };
}

const STATUS = { running: 'kesken', done: 'toteutettu', failed: 'keskeytyi', undone: 'palautettu' };

/**
 * The confirmation of Toteuta: the plan, its conflicts, and the button that carries it out. While the apply runs,
 * the dialog cannot be closed (`onRunning(true)` tells the view, whose Esc must wait too). `onDone(text)` after a
 * successful apply.
 */
export function ApplyDialog({ ev, settled, onClose, onChanged, onRunning, onDone }) {
  const [state, setState] = useState({ plan: null, error: null, busy: true });
  const cancel = useRef(null);

  const load = useCallback(async () => {
    setState({ plan: null, error: null, busy: true });
    try {
      await settled();               // the plan must include the decisions just made
      setState({ plan: await api(`/events/${ev.id}/apply-plan`), error: null, busy: false });
    } catch (e) {
      setState({ plan: null, error: `Suunnitelmaa ei voi laatia: ${e.message}`, busy: false });
    }
  }, [ev.id, settled]);
  useEffect(() => { load(); }, [load]);
  useEffect(() => { cancel.current?.focus(); }, []);

  const confirm = async () => {
    setState((s) => ({ ...s, busy: true, running: true, error: null }));
    onRunning(true);
    try {
      const { apply } = await api(`/events/${ev.id}/apply`, {
        method: 'POST', body: { confirm_token: state.plan.confirm_token },
      });
      onRunning(false);
      onChanged();
      onDone(`Toteutettu: ${applyText(apply)}${apply.shots_moved ? ` (kansioon ${state.plan.reject_dir})` : ''}.`);
      onClose();
    } catch (e) {
      onRunning(false);
      setState((s) => ({ ...s, busy: false, running: false, error: `Toteutus epäonnistui: ${e.message}`, failed: true }));
      onChanged();                   // a failed apply is in the history, to be restored
    }
  };
  const close = () => { if (!state.running) onClose(); };

  const { plan } = state;
  const summary = plan && planSummary(plan);
  return html`<div class="overlay" onClick=${close}>
    <section class="help apply" role="dialog" aria-modal="true" aria-label="Toteuta" onClick=${(e) => e.stopPropagation()}>
      <h2>Toteuta: ${ev.name}</h2>
      ${!plan && !state.error && html`<p class="muted">Lasketaan…</p>`}
      ${summary && summary.empty && html`<p class="plan-summary">${NOTHING_TO_DO}</p>
        ${plan.unreviewed > 0 && html`<p class="muted">${count(plan.unreviewed, 'kuva', 'kuvaa')} tarkastamatta.</p>`}`}
      ${summary && !summary.empty && html`
        <p class="plan-summary">${summary.text}</p>
        <p class="muted">Hylätyt kuvat siirretään kansioon <code>${plan.reject_dir}</code> RAW- ja JPEG-tiedostoineen ja
          XMP-sivutiedostoineen. Kuvien, joista olet tehnyt päätöksen, tähdet ja värimerkki kirjoitetaan XMP:hen;
          kuviin ilman päätöstä ei kosketa. Toteutuksen loki tallennetaan samaan kansioon. Mitään ei poisteta eikä
          korvata, ja toteutuksen voi palauttaa.</p>
        ${plan.user_values.length > 0 && html`<p class="muted">${count(plan.user_values.length, 'sivutiedosto jätetään',
          'sivutiedostoa jätetään')} ennalleen: niissä on oma tähtiluokitus tai värimerkki, eikä kuvalla ole omia
          tähtiä karsijassa.</p>`}
        ${plan.move.length > 0 && html`<details><summary>Siirrettävät tiedostot</summary>
          <ul class="paths">${plan.move.flatMap((m) => m.files).map((f) => html`<li><code>${f}</code></li>`)}</ul>
        </details>`}
        ${plan.xmp_create.length + plan.xmp_update.length > 0 && html`<details><summary>Kirjoitettavat XMP:t</summary>
          <ul class="paths">
            ${plan.xmp_create.map((f) => html`<li><code>${f}</code> (uusi)</li>`)}
            ${plan.xmp_update.map((f) => html`<li><code>${f}</code></li>`)}
          </ul>
        </details>`}`}
      ${plan && plan.conflicts.length > 0 && html`<div class="error">
        <p>Toteutus ei onnistu, ennen kuin nämä on korjattu:</p>
        <ul class="paths">${plan.conflicts.map((c) => html`<li><code>${c.path}</code>: ${c.reason}</li>`)}</ul>
      </div>`}
      ${state.running && html`<p class="muted" role="status">Toteutetaan… älä sulje sivua.</p>`}
      ${state.error && html`<p class="error" role="alert">${state.error}</p>`}
      <div class="buttons">
        ${state.failed
          ? html`<button onClick=${load} disabled=${state.busy}>Tarkista uudelleen</button>`
          : !summary?.empty && html`<button class="primary" onClick=${confirm} disabled=${state.busy || !summary?.ready}
              title=${plan && plan.conflicts.length ? 'Korjaa ensin ristiriidat' : ''}>
              ${state.running ? 'Toteutetaan…' : 'Toteuta'}</button>`}
        <button ref=${cancel} onClick=${close} disabled=${state.running}>
          ${state.failed || summary?.empty ? 'Sulje' : 'Peruuta'}</button>
      </div>
    </section>
  </div>`;
}

/** The event's applies, newest first. Only the newest one not yet restored can be restored. */
export function ApplyHistory({ eventId, version, onChanged }) {
  const { data, error } = useApi(`/events/${eventId}/applies`, version);
  const [confirming, setConfirming] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => { setConfirming(null); setMessage(null); }, [eventId]);

  const restore = async (id) => {
    setBusy(true);
    setMessage(null);
    try {
      setMessage(restoreText(await api(`/applies/${id}/restore`, { method: 'POST', body: {} })));
    } catch (e) {
      setMessage({ text: `Palautus epäonnistui: ${e.message}`, error: true });
    } finally {
      setBusy(false);
      setConfirming(null);
      onChanged();
    }
  };

  const shown = message && html`<p class=${`restore-message ${message.error ? 'error' : message.warn ? 'candidates' : 'muted'}`}
    role="status">${message.text}${message.dir && html` Kansio: <code>${message.dir}</code>`}</p>`;
  if (error) return html`<p class="error">Toteutushistoriaa ei voi ladata: ${error}</p>`;
  if (!data || data.applies.length === 0) return shown;
  const restorable = data.applies.find((a) => a.status !== 'undone')?.id;
  return html`<section class="applies">
    <h2>Toteutukset</h2>
    ${shown}
    <ul>${data.applies.map((a) => html`<li key=${a.id} class=${`apply-${a.status}`}>
      <span>${fmtTime(a.started)}</span>
      <span>${applyText(a)}</span>
      <span class="status">${STATUS[a.status]}${a.status === 'undone' && a.undone_at ? ` ${fmtTime(a.undone_at)}` : ''}</span>
      <span class="action">
        ${a.id === restorable && (confirming === a.id
          ? html`<span class="confirm">Palautetaanko kuvat ja XMP:t ennalleen?
              <button class="primary" disabled=${busy} onClick=${() => restore(a.id)}>
                ${busy ? 'Palautetaan…' : 'Palauta'}</button>
              <button disabled=${busy} onClick=${() => setConfirming(null)}>Peruuta</button></span>`
          : html`<button onClick=${() => { setConfirming(a.id); setMessage(null); }}>Palauta karsitut</button>`)}
        ${a.status !== 'undone' && a.id !== restorable && html`<span class="muted">palauta ensin uudempi</span>`}
      </span>
      ${a.error && html`<div class="error">${a.error}</div>`}
      <div class="muted log">loki: <code>${a.log}</code></div>
    </li>`)}</ul>
  </section>`;
}
