// Toteuta (carry out an event's decisions in the library) and its history, from which an apply is restored.
import { api, useApi } from './api.js';
import { fmt, fmtTime, html, useCallback, useEffect, useRef, useState } from './ui.js';

/** `1 kuva`, `3 kuvaa`. */
const count = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;

/**
 * What an apply plan would do, as the confirmation states it:
 * `Siirretään 3 kuvaa (10 tiedostoa), kirjoitetaan 8 XMP:tä (2 uutta, 6 päivitettyä), 1 tarkistamatta.`
 * `ready` is false when there is nothing to do or a conflict prevents it.
 */
export function planSummary(plan) {
  const files = plan.move.reduce((n, m) => n + m.files.length, 0);
  const created = plan.xmp_create.length;
  const updated = plan.xmp_update.length;
  const xmps = created + updated;
  const parts = [
    `siirretään ${count(plan.move.length, 'kuva', 'kuvaa')}${files ? ` (${count(files, 'tiedosto', 'tiedostoa')})` : ''}`,
    `kirjoitetaan ${count(xmps, 'XMP', 'XMP:tä')}${xmps ? ` (${fmt(created)} uutta, ${fmt(updated)} päivitettyä)` : ''}`,
    `${fmt(plan.unreviewed)} tarkistamatta`,
  ];
  const text = parts.join(', ');
  return {
    text: `${text[0].toUpperCase()}${text.slice(1)}.`,
    ready: plan.conflicts.length === 0 && plan.move.length + xmps > 0,
  };
}

/** One apply of the history: `siirretty 3 kuvaa, kirjoitettu 8 XMP:tä`. */
export function applyText(a) {
  return `siirretty ${count(a.shots_moved, 'kuva', 'kuvaa')}, `
    + `kirjoitettu ${count(a.xmp_created + a.xmp_updated, 'XMP', 'XMP:tä')}`;
}

const STATUS = { running: 'kesken', done: 'toteutettu', failed: 'keskeytyi', undone: 'palautettu' };

/** The confirmation of Toteuta: the plan, its conflicts, and the button that carries it out. */
export function ApplyDialog({ ev, settled, onClose, onChanged }) {
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
    setState((s) => ({ ...s, busy: true, error: null }));
    try {
      await api(`/events/${ev.id}/apply`, { method: 'POST', body: { confirm_token: state.plan.confirm_token } });
      onChanged();
      onClose();
    } catch (e) {
      setState((s) => ({ ...s, busy: false, error: `Toteutus epäonnistui: ${e.message}`, failed: true }));
      onChanged();                   // a failed apply is in the history, to be restored
    }
  };

  const { plan } = state;
  const summary = plan && planSummary(plan);
  return html`<div class="overlay" onClick=${onClose}>
    <section class="help apply" role="dialog" aria-modal="true" aria-label="Toteuta" onClick=${(e) => e.stopPropagation()}>
      <h2>Toteuta: ${ev.name}</h2>
      ${!plan && !state.error && html`<p class="muted">Lasketaan…</p>`}
      ${summary && html`
        <p class="plan-summary">${summary.text}</p>
        <p class="muted">Hylätyt kuvat siirretään kansioon <code>${plan.reject_dir}</code> RAW- ja JPEG-tiedostoineen ja
          XMP-sivutiedostoineen. Arvioitujen kuvien tähdet ja värimerkki kirjoitetaan XMP:hen; tarkistamattomiin
          kuviin ei kosketa. Mitään ei poisteta eikä korvata, ja toteutuksen voi palauttaa.</p>
        ${plan.user_values.length > 0 && html`<p class="muted">${count(plan.user_values.length, 'sivutiedosto jätetään',
          'sivutiedostoa jätetään')} ennalleen: niissä on oma tähtiluokitus tai värimerkki, eikä kuvalla ole omia
          tähtiä karsijassa.</p>`}
        ${plan.conflicts.length > 0 && html`<div class="error">
          <p>Toteutus ei onnistu, ennen kuin nämä on korjattu:</p>
          <ul class="paths">${plan.conflicts.map((c) => html`<li><code>${c.path}</code>: ${c.reason}</li>`)}</ul>
        </div>`}
        ${plan.move.length > 0 && html`<details><summary>Siirrettävät tiedostot</summary>
          <ul class="paths">${plan.move.flatMap((m) => m.files).map((f) => html`<li><code>${f}</code></li>`)}</ul>
        </details>`}
        ${plan.xmp_create.length + plan.xmp_update.length > 0 && html`<details><summary>Kirjoitettavat XMP:t</summary>
          <ul class="paths">
            ${plan.xmp_create.map((f) => html`<li><code>${f}</code> (uusi)</li>`)}
            ${plan.xmp_update.map((f) => html`<li><code>${f}</code></li>`)}
          </ul>
        </details>`}`}
      ${state.error && html`<p class="error">${state.error}</p>`}
      <div class="buttons">
        ${state.failed
          ? html`<button onClick=${load} disabled=${state.busy}>Tarkista uudelleen</button>`
          : html`<button class="primary" onClick=${confirm} disabled=${state.busy || !summary?.ready}>
              Toteuta</button>`}
        <button ref=${cancel} onClick=${onClose}>${state.failed ? 'Sulje' : 'Peruuta'}</button>
      </div>
    </section>
  </div>`;
}

/** The event's applies, newest first, each with Palauta karsitut until it is restored. */
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
      const res = await api(`/applies/${id}/restore`, { method: 'POST', body: {} });
      let text = 'Palautettu. ';
      if (res.missing.length) text += `${count(res.missing.length, 'siirretty tiedosto puuttui', 'siirrettyä tiedostoa puuttui')}: ${res.missing.join(', ')}. `;
      if (res.kept.length) text += `Toteutuksen luomat XMP:t ja sen loki ovat kansiossa ${res.undone_dir}.`;
      setMessage({ text, error: false });
    } catch (e) {
      setMessage({ text: `Palautus epäonnistui: ${e.message}`, error: true });
    } finally {
      setBusy(false);
      setConfirming(null);
      onChanged();
    }
  };

  if (error) return html`<p class="error">Toteutushistoriaa ei voi ladata: ${error}</p>`;
  if (!data || data.applies.length === 0) return message && html`<p class="muted">${message.text}</p>`;
  return html`<section class="applies">
    <h2>Toteutukset</h2>
    ${message && html`<p class=${message.error ? 'error' : 'muted'} role="status">${message.text}</p>`}
    <ul>${data.applies.map((a) => html`<li key=${a.id} class=${`apply-${a.status}`}>
      <span>${fmtTime(a.started)}</span>
      <span>${applyText(a)}</span>
      <span class="status">${STATUS[a.status]}${a.status === 'undone' && a.undone_at ? ` ${fmtTime(a.undone_at)}` : ''}</span>
      ${a.status !== 'undone' && (confirming === a.id
        ? html`<span class="confirm">Palautetaanko kuvat ja XMP:t ennalleen?
            <button class="primary" disabled=${busy} onClick=${() => restore(a.id)}>Palauta</button>
            <button disabled=${busy} onClick=${() => setConfirming(null)}>Peruuta</button></span>`
        : html`<button onClick=${() => { setConfirming(a.id); setMessage(null); }}>Palauta karsitut</button>`)}
      ${a.error && html`<div class="error">${a.error}</div>`}
      <div class="muted log">loki: <code>${a.log}</code></div>
    </li>`)}</ul>
  </section>`;
}
