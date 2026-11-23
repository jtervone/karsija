// Kirjasto: the years and event folders of the library, their counts and analysis.
import { useApi } from './api.js';
import { fmt, fmtTime, html, useState } from './ui.js';

/** Progress of a queued or running job: a bar and its label. */
export function JobProgress({ job }) {
  // the server counts the shots to analyse first; until then (and when every shot comes from the cache) total is 0
  let label;
  if (job.status === 'queued') label = 'Jonossa';
  else if (!job.total) label = 'Analysoidaan…';
  else if (job.done >= job.total) label = 'Ryhmitellään…';
  else label = `Analysoidaan ${fmt(job.done)} / ${fmt(job.total)}`;
  // no value while the number of shots is not known yet: an indeterminate bar
  const value = job.status === 'running' && job.total ? job.done : undefined;
  return html`<div class="progress">
    <progress max=${job.total || 1} value=${value} aria-label=${label}></progress>
    <span>${label}</span>
  </div>`;
}

/** When the event was analysed; 0 is an analysis of an older version whose time is not known. */
function analysedText(at) {
  return at ? `analysoitu ${fmtTime(at)}` : 'analysoitu';
}

function Counts({ ev }) {
  if (ev.analysed_at == null) {
    return html`<td colspan="4" class="muted unanalysed-cell">
      <span class="badge unanalysed">Ei analysoitu</span>
      ${ev.shots > 0 && html` (${fmt(ev.shots)} kuvaa tallessa aiemmasta analyysistä)`}
    </td>`;
  }
  const empty = ev.shots === 0;
  const num = (label, content) => html`<td class="num" data-label=${label}>${content}</td>`;
  return html`
    ${num('Kuvia', fmt(ev.shots))}
    ${num('Karsittavia', empty ? '–'
      : ev.candidates > 0 ? html`<span class="candidates">${fmt(ev.candidates)}</span>` : '0')}
    ${num('Tarkastettu', empty ? '–' : fmt(ev.reviewed))}
    ${num('Tarkastamatta', empty ? '–'
      : ev.unreviewed === 0 ? html`<span class="reviewed">valmis</span>` : fmt(ev.unreviewed))}`;
}

function EventRow({ ev, job, failed, allBusy, start }) {
  let action;
  if (job) action = html`<${JobProgress} job=${job} />`;
  // a disabled button would not say why (and shows no tooltip in every browser)
  else if (allBusy) action = html`<span class="muted">mukana kirjaston analyysissä</span>`;
  else {
    action = html`<button onClick=${() => start(ev.id)}>
      ${ev.analysed_at == null ? 'Analysoi' : 'Analysoi uudelleen'}</button>`;
  }
  return html`<tr>
    <td class="name">
      <a href=${`#/event/${ev.id}`}>${ev.name}</a>
      ${ev.analysed_at != null && html`<div class="muted when">${analysedText(ev.analysed_at)}</div>`}
    </td>
    <${Counts} ev=${ev} />
    <td class="action">
      ${action}
      ${failed && !job && !(ev.analysed_at > failed.finished)
        && html`<div class="error">Analyysi epäonnistui: ${failed.error}</div>`}
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
          : events.length > 0 && html`<button class="primary" onClick=${() => start('all')}>Analysoi kaikki</button>`}
      </div>
    </header>
    ${startError && html`<p class="error">Analyysiä ei voi aloittaa: ${startError}</p>`}
    ${allFailed && !allJob && html`<p class="error">Kirjaston analyysi epäonnistui: ${allFailed.error}</p>`}
    ${error && html`<p class="error">Kirjastoa ei voi ladata: ${error}</p>`}
    ${!data && !error && html`<p class="muted">Ladataan…</p>`}
    ${data && events.length === 0 && html`<p class="muted">
      Kirjastossa ei ole tapahtumakansioita. Kuvien pitää olla kansioissa <code>VVVV/VVVV-KK-PP</code>, joiden
      nimen perään voi lisätä tapahtuman nimen (<code>2025/2025-06-14-juhannus</code>).</p>`}
    ${events.length > 0 && html`<p class="summary muted">
      ${fmt(events.length)} tapahtumaa, ${fmt(analysed.length)} analysoitu${analysed.length > 0 && html`.
      Analysoiduissa ${fmt(sum('shots'))} kuvaa · <span class="candidates">${fmt(sum('candidates'))} karsittavaa</span>
      · ${fmt(sum('unreviewed'))} tarkastamatta`}.</p>`}
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
