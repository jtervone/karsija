// Event, loupe and compare views: only a frame with the event's counts until the review views arrive.
import { useApi } from './api.js';
import { fmt, html } from './ui.js';

const TITLES = { event: 'Tapahtuma', loupe: 'Kuva', compare: 'Vertailu' };

export function EventView({ route, version }) {
  const { data, error } = useApi(`/events/${route.event}`, version);
  const ev = data?.event;
  return html`<main class="page">
    <p class="crumbs">
      <a href="#/">← Kirjasto</a>
      ${route.view !== 'event' && ev && html` · <a href=${`#/event/${ev.id}`}>${ev.name}</a>`}
    </p>
    <h1>${ev ? ev.name : TITLES[route.view]}</h1>
    ${error && html`<p class="error">${error}</p>`}
    ${ev && html`<p class="muted">
      ${ev.analysed_at == null ? html`<span class="badge unanalysed">Ei analysoitu</span>`
        : html`${fmt(ev.shots)} kuvaa · <span class="candidates">${fmt(ev.candidates)} karsittavaa</span> ·
               ${fmt(ev.reviewed)} tarkastettu, ${fmt(ev.unreviewed)} tarkastamatta`}
    </p>`}
    <p class="muted">${TITLES[route.view]}näkymä tulee myöhemmin.</p>
  </main>`;
}
