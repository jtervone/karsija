// JSON API of the server and its progress events.
import { useEffect, useState } from './ui.js';

export class ApiError extends Error {
  /** `status`: the HTTP status, 0 when the server could not be reached. */
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

/** `fetch` of `/api<path>`; `body` is sent as JSON. Throws an `ApiError` with the server's message. */
export async function api(path, { method = 'GET', body } = {}) {
  const init = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError('palvelimeen ei saada yhteyttä');
  }
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : `virhe ${resp.status}`;
    throw new ApiError(detail, resp.status);
  }
  return data;
}

/**
 * Follow the server-sent events: `onJob(job)` for every job update, `onOpen()` whenever the stream (re)connects,
 * `onError()` when it is lost (the browser reconnects on its own). Returns a function that closes the stream.
 */
export function followEvents({ onJob, onOpen, onError }) {
  const source = new EventSource('/api/events/stream');
  source.addEventListener('job', (e) => onJob(JSON.parse(e.data)));
  source.addEventListener('open', () => onOpen());
  source.addEventListener('error', () => onError());
  return () => source.close();
}

/**
 * Data of `api(url)`, reloaded whenever `version` changes. The previous data of the same URL stays on screen while
 * reloading.
 */
export function useApi(url, version) {
  const [state, setState] = useState({ url, data: null, error: null });
  useEffect(() => {
    let current = true;
    api(url).then(
      (data) => current && setState({ url, data, error: null }),
      (e) => current && setState((s) => ({ url, data: s.url === url ? s.data : null, error: e.message })),
    );
    return () => { current = false; };
  }, [url, version]);
  return state.url === url ? state : { data: null, error: null };
}
