// Event views: the Tapahtuma grid, and the frame of the loupe and compare views, which share its keys and decisions.
import { Grid, groupBest } from './grid.js';
import { ShortcutHelp, useKeys } from './keys.js';
import { CANDIDATE, Order, filterGroups, useReview } from './review.js';
import { fmt, html, useCallback, useEffect, useMemo, useRef, useState } from './ui.js';

const TITLES = { event: 'Tapahtuma', loupe: 'Kuva', compare: 'Vertailu' };
const FILTERS = [
  ['candidates', 'Vain ehdokkaat', 'automaattisesti samankaltaisiksi tai epäteräviksi arvioidut'],
  ['groups', 'Vain ryhmät', 'ryhmät, joissa on useampi kuva'],
  ['unreviewed', 'Vain tarkastamattomat', 'kuvat ilman omaa päätöstä'],
];
const NEIGHBOURS = [-4, 16];         // grid previews loaded ahead of the current shot, behind and ahead

/** After a filter is clicked, give Space back to the shortcuts; a filter reached with Tab keeps its focus. */
function blurFilter() {
  setTimeout(() => { if (document.activeElement?.closest('.filters')) document.activeElement.blur(); }, 0);
}

const preloaded = new Set();
function preload(url) {
  if (preloaded.has(url)) return;
  if (preloaded.size > 5000) preloaded.clear();
  preloaded.add(url);
  new Image().src = url;
}

/** Load the grid previews around `current` and the first ones of the next group, also while an arrow key is held. */
function usePreload(order, shots, current) {
  useEffect(() => {
    if (current == null) return;
    const i = order.index.get(current) ?? 0;
    const ids = order.ids.slice(Math.max(i + NEIGHBOURS[0], 0), i + NEIGHBOURS[1]);
    const next = order.stepGroup(current, 1);
    if (next != null) ids.push(...order.group(next).ids.slice(0, 8));
    for (const id of ids) preload(shots.get(id).preview.grid);
  }, [order, current]);     // not on decisions: they do not change the previews
}

function Counts({ ev, shots }) {
  const reviewed = useMemo(() => {
    let n = 0;
    for (const s of shots.values()) if (s.user_verdict != null) n += 1;
    return n;
  }, [shots]);
  const candidates = useMemo(() => {
    let n = 0;
    for (const s of shots.values()) if (CANDIDATE.has(s.auto_verdict)) n += 1;
    return n;
  }, [shots]);
  const total = shots.size;
  if (ev.analysed_at == null && total === 0) return html`<span class="badge unanalysed">Ei analysoitu</span>`;
  return html`<span class="muted counts">
    ${fmt(total)} kuvaa · <span class="candidates">${fmt(candidates)} karsittavaa</span> ·
    ${fmt(reviewed)} tarkastettu, ${total - reviewed === 0 && total > 0
      ? html`<span class="reviewed">kaikki tarkastettu</span>` : `${fmt(total - reviewed)} tarkastamatta`}
    ${ev.analysed_at == null && html` <span class="badge unanalysed">Ei analysoitu</span>`}
  </span>`;
}

export function EventView({ route, version }) {
  const eventId = route.event;
  const review = useReview(eventId, version);
  const { data, groups, shots } = review;
  const [filters, setFilters] = useState({ candidates: false, groups: false, unreviewed: false });
  const [cursor, setCursor] = useState(null);
  const [help, setHelp] = useState(false);

  useEffect(() => { setCursor(null); }, [eventId]);

  // What is shown changes when the filters change or the event reloads, not with each decision: a shot decided
  // under "Vain tarkastamattomat" stays until then, so the cursor does not jump away from it.
  const latest = useRef(shots);
  latest.current = shots;
  const shown = useMemo(() => filterGroups(groups, latest.current, filters), [groups, filters]);
  const order = useMemo(() => new Order(shown), [shown]);
  const all = useMemo(() => groups.flatMap((g) => g.ids), [groups]);
  const best = useMemo(() => groupBest(groups, latest.current), [groups]);
  // first time in: the first shot not reviewed yet
  const first = useMemo(() => order.ids.find((id) => latest.current.get(id).user_verdict == null) ?? order.ids[0],
    [order]);
  const current = cursor == null ? first ?? null : order.nearest(cursor, all);

  usePreload(order, shots, current);

  const grid = `#/event/${eventId}`;
  const currentGroup = current == null ? null : order.group(current);

  // The loupe and compare views keep the current shot (its group) in their address, so a reload or the back
  // button returns to it. A new address (a link, Back) moves the cursor first; until the cursor has followed it,
  // the address is not rewritten.
  const followed = useRef(null);
  useEffect(() => {
    if (!data) return;
    const target = route.view === 'event' ? null : `${route.view} ${route.shot ?? route.group}`;
    if (target !== followed.current) {
      followed.current = target;
      if (route.view === 'loupe' && route.shot !== current) {
        setCursor(route.shot);
        return;
      }
      if (route.view === 'compare' && currentGroup?.groupId !== route.group) {
        const g = order.groups.find((x) => x.groupId === route.group);
        if (g) {
          setCursor(g.ids[0]);
          return;
        }
      }
    }
    let want = null;
    if (route.view === 'loupe' && current != null) want = `${grid}/loupe/${current}`;
    if (route.view === 'compare' && currentGroup?.groupId != null) want = `${grid}/compare/${currentGroup.groupId}`;
    if (want && location.hash !== want) history.replaceState(null, '', want);
  });

  const toolbar = useRef(null);
  useEffect(() => {
    // the cards scroll to just below the toolbar, whose height changes as it wraps
    const el = toolbar.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => {
      document.documentElement.style.setProperty('--toolbar-h', `${el.offsetHeight}px`);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!review.notice) return undefined;
    const timer = setTimeout(() => review.notify(null), 6000);
    return () => clearTimeout(timer);
  }, [review.notice, review.notify]);

  const open = useCallback((id) => { location.hash = `${grid}/loupe/${id}`; }, [grid]);

  useKeys((action) => {
    if (help) {
      if (action.type === 'help' || action.type === 'back') setHelp(false);
      return true;
    }
    if (action.type === 'help') {
      setHelp(true);
      return true;
    }
    if (action.type === 'back') {
      if (route.view !== 'event') location.hash = grid;
      else if (review.failure || review.notice) {
        review.dismiss();
        review.notify(null);
      } else return false;
      return true;
    }
    if (current == null || action.type === 'zoom') return false;     // zoom: the loupe and compare views, later
    if (action.type === 'open' && route.view !== 'event') return false;
    if (cursor == null) setCursor(current);      // the first unreviewed shot, until then only derived
    switch (action.type) {
      case 'move': setCursor(order.step(current, action.by)); break;
      case 'group': setCursor(order.stepGroup(current, action.by)); break;
      case 'keep': review.decide(current, { user_verdict: 'keep' }); break;
      case 'reject': review.decide(current, { user_verdict: 'reject' }); break;
      case 'rate': review.decide(current, { rating: action.stars }); break;
      case 'reset': review.decide(current, { user_verdict: null, rating: null }); break;
      case 'accept': {
        const group = order.group(current);
        review.accept(group);
        const next = order.stepGroup(current, 1);
        if (order.group(next) !== group) setCursor(next);
        break;
      }
      case 'undo': {
        const back = review.undo().find((id) => order.index.has(id));
        if (back != null) setCursor(back);
        break;
      }
      case 'open':
        open(current);
        break;
      case 'compare':
        if (currentGroup?.groupId == null || currentGroup.members.length < 2) {
          review.notify('Kuvalla ei ole ryhmää, jonka kuvia vertailla.');
        } else location.hash = `${grid}/compare/${currentGroup.groupId}`;
        break;
      default:
        return false;
    }
    return true;
  });

  const ev = data?.event;
  const toggle = (key) => setFilters((f) => ({ ...f, [key]: !f[key] }));
  return html`<main class=${`page${route.view === 'event' ? ' wide' : ''}`}>
    <p class="crumbs">
      <a href="#/">← Kirjasto</a>
      ${route.view !== 'event' && ev && html` · <a href=${grid}>${ev.name}</a>`}
    </p>
    <header class="toolbar" ref=${toolbar}>
      <h1>${ev ? ev.name : TITLES[route.view]}</h1>
      ${ev && html`<${Counts} ev=${ev} shots=${shots} />`}
      ${route.view === 'event' && ev && html`<div class="filters" onPointerUp=${blurFilter}>
        ${FILTERS.map(([key, label, title]) => html`<label key=${key} title=${title}>
          <input type="checkbox" checked=${filters[key]} onChange=${() => toggle(key)} /> ${label}</label>`)}
      </div>`}
      <button class="help-button" onClick=${() => setHelp(true)} title="Pikanäppäimet (?)">?</button>
      ${(review.failure || review.notice) && html`<div class="notices">
        ${review.failure && html`<div class="notice error" role="alert">
          ${review.failure.text}${review.failure.count > 1 && ` (yhteensä ${review.failure.count} tallentamatta)`}
          <button class="dismiss" onClick=${review.dismiss} title="Sulje (Esc)" aria-label="Sulje">×</button>
        </div>`}
        ${review.notice && html`<div class="notice" role="status">${review.notice.text}</div>`}
      </div>`}
    </header>
    ${review.error && !data && html`<p class="error">Tapahtumaa ei voi ladata: ${review.error}</p>`}
    ${!data && !review.error && html`<p class="muted">Ladataan…</p>`}
    ${data && route.view === 'event' && (shots.size === 0
      ? html`<p class="muted">${ev.analysed_at == null
        ? html`Tapahtumaa ei ole analysoitu. Analysoi se <a href="#/">Kirjastossa</a>.`
        : 'Tapahtumassa ei ole kuvia.'}</p>`
      : order.ids.length === 0
        ? html`<p class="muted">Yksikään kuva ei täytä valittuja ehtoja.</p>`
        : html`<${Grid} groups=${shown} shots=${shots} best=${best} cursor=${current} onPick=${setCursor}
            onOpen=${open} />`)}
    ${data && route.view !== 'event' && html`<p class="muted">
      ${TITLES[route.view]}näkymä tulee myöhemmin. Siihen asti tässä näkyy nykyinen
      ${route.view === 'loupe' ? 'kuva' : 'ryhmä'} päätöksineen; pikanäppäimet toimivat zoomausta (Z) lukuun
      ottamatta, ja Esc palaa ruudukkoon.</p>
      ${current != null && html`<${Grid} groups=${route.view === 'loupe'
        ? [{ key: currentGroup.key, members: [current], ids: [current] }] : [currentGroup]}
        shots=${shots} best=${best} cursor=${current} onPick=${setCursor} onOpen=${open} />`}`}
    ${help && html`<${ShortcutHelp} onClose=${() => setHelp(false)} />`}
  </main>`;
}
