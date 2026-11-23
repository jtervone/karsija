// karsija UI: hash router, analysis jobs followed over server-sent events, the views.
import { api, followEvents } from './api.js';
import { html, render, useCallback, useEffect, useRef, useState } from './ui.js';
import { Kirjasto } from './kirjasto.js';
import { EventView } from './event.js';

/**
 * `#/` library, `#/event/<id>`, `#/event/<id>/loupe/<shot>`, `#/event/<id>/compare/<group>`.
 * Returns `{ view, ...ids }`, `view` is `notfound` for anything else.
 */
export function parseRoute(hash) {
  const path = hash.replace(/^#/, '') || '/';
  if (path === '/') return { view: 'library' };
  let m = path.match(/^\/event\/(\d+)\/?$/);
  if (m) return { view: 'event', event: Number(m[1]) };
  m = path.match(/^\/event\/(\d+)\/loupe\/(\d+)\/?$/);
  if (m) return { view: 'loupe', event: Number(m[1]), shot: Number(m[2]) };
  m = path.match(/^\/event\/(\d+)\/compare\/(\d+)\/?$/);
  if (m) return { view: 'compare', event: Number(m[1]), group: Number(m[2]) };
  return { view: 'notfound' };
}

function useRoute() {
  const [route, setRoute] = useState(() => parseRoute(location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(location.hash));
    addEventListener('hashchange', onChange);
    return () => removeEventListener('hashchange', onChange);
  }, []);
  return route;
}

/**
 * Analysis jobs. `active`: queued and running jobs by id. `failed`: the last failed job of each scope ("all" or an
 * event id as a string) until that scope is analysed again. `version` grows whenever the stored results may have
 * changed (a job ended, the event stream reconnected): views reload their data when it does.
 */
function useJobs() {
  const [state, setState] = useState({ active: {}, failed: {}, version: 0, online: true });
  const ended = useRef(new Set());       // a POST answer may arrive after the job has already ended

  const update = useCallback((job) => {
    setState((s) => {
      if (job.status === 'done' || job.status === 'failed') {
        ended.current.add(job.id);
        const active = { ...s.active };
        delete active[job.id];
        const failed = { ...s.failed };
        if (job.status === 'failed') failed[job.scope] = job;
        return { ...s, active, failed, version: s.version + 1 };
      }
      if (ended.current.has(job.id)) return s;
      const old = s.active[job.id];
      if (old && old.status === 'running' && job.status === 'queued') return s;    // stale answer
      const failed = { ...s.failed };
      delete failed[job.scope];
      return { ...s, active: { ...s.active, [job.id]: job }, failed };
    });
  }, []);

  const opened = useRef(0);               // times the event stream has (re)connected
  const lost = useRef(false);             // the stream (or the server) was unreachable since the last connection
  const active = useRef(state.active);
  active.current = state.active;
  useEffect(() => followEvents({
    onJob: update,
    onOpen: () => {
      const reload = opened.current++ > 0 || lost.current;
      lost.current = false;
      setState((s) => ({ ...s, online: true }));
      // The first connection needs nothing: the views have just loaded their data. After a break, jobs may have
      // ended unseen (a server restart fails them): ask the server about each one, the stream lists those still
      // pending, and reload the data.
      if (!reload) return;
      const ids = Object.keys(active.current);
      setState((s) => ({ ...s, active: {}, version: s.version + 1 }));
      for (const id of ids) api(`/jobs/${id}`).then(update, () => {});
    },
    onError: () => {
      lost.current = true;
      setState((s) => ({ ...s, online: false }));
    },
  }), [update]);

  const start = useCallback(async (scope) => {
    const before = opened.current;
    let { job } = await api('/analyse', { method: 'POST', body: { scope } });
    // the stream reconnected meanwhile: the job may have ended unseen, ask the server how it is now
    if (opened.current !== before) job = await api(`/jobs/${job.id}`);
    update(job);
  }, [update]);

  return { ...state, start };
}

function NotFound() {
  return html`<main class="page">
    <p><a href="#/">← Kirjasto</a></p>
    <h1>Sivua ei ole</h1>
  </main>`;
}

function App() {
  const route = useRoute();
  const jobs = useJobs();
  useEffect(() => { scrollTo(0, 0); }, [route.view, route.event]);

  let view;
  if (route.view === 'library') view = html`<${Kirjasto} jobs=${jobs} />`;
  else if (route.view === 'notfound') view = html`<${NotFound} />`;
  else view = html`<${EventView} route=${route} version=${jobs.version} />`;
  return html`
    ${!jobs.online && html`<div class="offline" role="status">Yhteys palvelimeen katkesi, yhdistetään uudelleen…</div>`}
    ${view}`;
}

render(html`<${App} />`, document.getElementById('app'));
