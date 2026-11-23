// Kirjasto: the years and event folders of the library, their counts and analysis.
import { useApi } from './api.js';
import { fmt, fmtTime, html, useState } from './ui.js';

/** Progress of a queued or running job: a bar and its label. */
export function JobProgress({ job }) {
  let label;
  if (job.status === 'queued') label = 'Jonossa';
  else if (!job.total) label = 'Etsitään kuvia…';
  else label = `Analysoidaan ${fmt(job.done)} / ${fmt(job.total)}`;
  // no value while the number of shots is not known yet: an indeterminate bar
  const value = job.status === 'running' && job.total ? job.done : undefined;
  return html`<div class="progress">
    <progress max=${job.total || 1} value=${value} aria-label=${label}></progress>
    <span>${label}</span>
  </div>`;
}

function Counts({ ev }) {
  if (ev.analysed_at == null) {
    return html`<td colspan="4" class="muted">
      <span class="badge unanalysed">Ei analysoitu</span>
      ${ev.shots > 0 && html` (${fmt(ev.shots)} kuvaa tallessa aiemmasta analyysistä)`}
    </td>`;
  }
  const empty = ev.shots === 0;
  return html`
    <td class="num" title=${`analysoitu ${fmtTime(ev.analysed_at)}`}>${fmt(ev.shots)}</td>
    <td class="num">${ev.candidates > 0 ? html`<span class="candidates">${fmt(ev.candidates)}</span>` : '0'}</td>
    <td class="num">${empty ? '–' : fmt(ev.reviewed)}</td>
    <td class="num">${empty ? '–' : ev.unreviewed === 0 ? html`<span class="reviewed">valmis</span>` : fmt(ev.unreviewed)}</td>`;
}

function EventRow({ ev, job, failed, allBusy, start }) {
  const busy = job || allBusy;
  return html`<tr>
    <td class="name"><a href=${`#/event/${ev.id}`}>${ev.name}</a></td>
    <${Counts} ev=${ev} />
    <td class="action">
      ${job ? html`<${JobProgress} job=${job} />`
            : html`<button disabled=${busy} onClick=${() => start(ev.id)}
                     title=${allBusy ? 'Koko kirjaston analyysi on käynnissä' : ''}>Analysoi</button>`}
      ${failed && !job && html`<div class="error">Analyysi epäonnistui: ${failed.error}</div>`}
    </td>
  </tr>`;
}

export function Kirjasto({ jobs }) {
  const { data, error } = useApi('/library', jobs.version);
  const [startError, setStartError] = useState(null);

  const start = (scope) => {
    setStartError(null);
    jobs.start(scope).catch((e) => setStartError(e.message));
  };
  const pending = Object.values(jobs.active);
  const allJob = pending.find((j) => j.scope === 'all');
  const byEvent = Object.fromEntries(pending.filter((j) => j.scope !== 'all').map((j) => [j.scope, j]));
  const allFailed = jobs.failed.all;

  const events = data ? data.years.flatMap((y) => y.events) : [];
  const analysed = events.filter((ev) => ev.analysed_at != null);
  const sum = (key) => analysed.reduce((n, ev) => n + ev[key], 0);

  return html`<main class="page">
    <header class="top">
      <div>
        <h1>Kirjasto</h1>
        ${data && html`<div class="muted root">${data.root}</div>`}
      </div>
      <div class="actions">
        ${allJob ? html`<${JobProgress} job=${allJob} />`
                 : html`<button class="primary" onClick=${() => start('all')}>Analysoi kaikki</button>`}
      </div>
    </header>
    ${startError && html`<p class="error">Analyysiä ei voi aloittaa: ${startError}</p>`}
    ${allFailed && !allJob && html`<p class="error">Kirjaston analyysi epäonnistui: ${allFailed.error}</p>`}
    ${error && html`<p class="error">Kirjastoa ei voi ladata: ${error}</p>`}
    ${!data && !error && html`<p class="muted">Ladataan…</p>`}
    ${data && events.length === 0 && html`<p class="muted">
      Kirjastossa ei ole tapahtumakansioita. Kuvien pitää olla kansioissa <code>VVVV/VVVV-KK-PP-nimi</code>.</p>`}
    ${events.length > 0 && html`<p class="summary muted">
      ${fmt(events.length)} tapahtumaa, ${fmt(analysed.length)} analysoitu ·
      ${fmt(sum('shots'))} kuvaa · <span class="candidates">${fmt(sum('candidates'))} karsittavaa</span> ·
      ${fmt(sum('unreviewed'))} tarkastamatta</p>`}
    ${data && data.years.map((y) => html`<section class="year" key=${y.year}>
      <h2>${y.year}</h2>
      <table class="events">
        <thead><tr>
          <th>Tapahtuma</th><th class="num">Kuvia</th><th class="num">Karsittavia</th>
          <th class="num">Tarkastettu</th><th class="num">Tarkastamatta</th><th></th>
        </tr></thead>
        <tbody>
          ${y.events.map((ev) => html`<${EventRow} key=${ev.id} ev=${ev} job=${byEvent[String(ev.id)]}
            failed=${jobs.failed[String(ev.id)]} allBusy=${!!allJob} start=${start} />`)}
        </tbody>
      </table>
    </section>`)}
  </main>`;
}
