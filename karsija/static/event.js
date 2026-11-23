// Event views: the Tapahtuma grid, the loupe and the compare view, which share its keys and decisions.
import { ApplyDialog, ApplyHistory } from './apply.js';
import { Compare, MAX_PANES, bestFirst, comparable, comparableGroup, stepWithin } from './compare.js';
import { Grid, groupBest } from './grid.js';
import { ShortcutHelp, useKeys } from './keys.js';
import { Loupe } from './loupe.js';
import { CANDIDATE, Order, filterGroups, useReview } from './review.js';
import { fmt, html, preload, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from './ui.js';

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

/**
 * Where `id` is among the shown shots: `Kuva 12 / 340 · ryhmässä 2 / 5`, and the filters that hide the others
 * (`filters` as in `filterGroups`): `· suodatettu: vain ehdokkaat`.
 */
export function position(order, id, filters = {}) {
  const group = order.group(id);
  const parts = [`Kuva ${fmt(order.index.get(id) + 1)} / ${fmt(order.ids.length)}`];
  if (group && group.members.length > 1) {
    const hidden = group.members.length - group.ids.length;
    parts.push(`ryhmässä ${group.ids.indexOf(id) + 1} / ${group.ids.length}${hidden > 0 ? `, ${hidden} piilossa` : ''}`);
  }
  return withFilters(parts, filters);
}

/** `parts` joined with ` · `, and the active filters. */
function withFilters(parts, filters) {
  const active = FILTERS.filter(([key]) => filters[key]).map(([, label]) => label.toLowerCase());
  if (active.length) parts.push(`suodatettu: ${active.join(', ')}`);
  return parts.join(' · ');
}

/**
 * Where the compare view of the group of `id` is among the groups it can show (see `comparableGroup`):
 * `Ryhmä 2 / 7 · 6 kuvaa, paras ensin, 1 piilossa`, and the filters as in `position`.
 */
export function comparePosition(order, id, filters = {}) {
  const group = order.group(id);
  const comparable = order.groups.filter((g) => g.groupId != null && g.ids.length > 1);
  const i = comparable.indexOf(group);
  const hidden = group.members.length - group.ids.length;
  const shown = `${group.ids.length} ${group.ids.length === 1 ? 'kuva' : 'kuvaa, paras ensin'}`;
  return withFilters([i < 0 ? 'Ryhmä' : `Ryhmä ${i + 1} / ${comparable.length}`,
    `${shown}${hidden > 0 ? `, ${hidden} piilossa` : ''}`], filters);
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

export function EventView({ route, version: jobsVersion }) {
  const eventId = route.event;
  const [applied, setApplied] = useState(0);     // grows when an apply or restore may have changed the event
  const version = jobsVersion + applied;
  const review = useReview(eventId, version, applied);
  const { data, groups, shots } = review;
  const [filters, setFilters] = useState({ candidates: false, groups: false, unreviewed: false });
  const [cursor, setCursor] = useState(null);
  const [help, setHelp] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyRunning, setApplyRunning] = useState(false);
  const changed = useCallback(() => setApplied((n) => n + 1), []);

  useEffect(() => { setCursor(null); setApplying(false); }, [eventId]);

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
  // The compare view's shots, and the shot of a group it starts at: its best. Sorted by the shots as last loaded:
  // decisions change neither the verdicts nor the sharpness the order is made of.
  const compared = useMemo(() => (currentGroup ? bestFirst(currentGroup.ids, latest.current) : []), [currentGroup]);
  const firstOf = (g) => bestFirst(g.ids, latest.current)[0];

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
          setCursor(firstOf(g));
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

  // Back in the grid (Esc, Back): where it was left, moved only as far as the current shot needs. The listener goes
  // before the view changes (a layout effect), so the shorter page of the loupe does not overwrite the place.
  const gridScroll = useRef(null);
  const loaded = data != null;
  useLayoutEffect(() => {
    if (route.view !== 'event' || !loaded) return undefined;
    if (gridScroll.current?.event === eventId) {
      scrollTo(0, gridScroll.current.y);
      document.querySelector('.card.current')?.scrollIntoView({ block: 'nearest' });
    }
    gridScroll.current = { event: eventId, y: scrollY };
    const onScroll = () => { gridScroll.current = { event: eventId, y: scrollY }; };
    addEventListener('scroll', onScroll, { passive: true });
    return () => removeEventListener('scroll', onScroll);
  }, [route.view, eventId, loaded]);

  const open = useCallback((id) => { location.hash = `${grid}/loupe/${id}`; }, [grid]);
  const viewKeys = useRef(null);         // the loupe's or compare view's own key handler (Z)
  const comparing = route.view === 'compare';
  /** In the compare view: go to the comparable group `by` groups away; false when there is none. */
  const compareGroup = (by) => {
    const g = comparableGroup(order, current, by);
    if (g) setCursor(firstOf(g));
    return !!g;
  };

  useKeys((action) => {
    if (applying) {
      // the dialog's buttons keep Space and Enter; nothing is decided under it
      if (action.type !== 'back') return false;
      if (!applyRunning) setApplying(false);       // a running apply's dialog waits for its answer
      return true;
    }
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
    if (action.type === 'zoom') return route.view !== 'event' && viewKeys.current ? viewKeys.current(action) : false;
    if (current == null) return false;
    if (action.type === 'open' && route.view === 'loupe') return false;
    if (cursor == null) setCursor(current);      // the first unreviewed shot, until then only derived
    switch (action.type) {
      case 'move':
        // in compare within the group: the panes follow the selection
        setCursor(comparing ? stepWithin(compared, current, action.by) : order.step(current, action.by));
        break;
      case 'group':
        if (!comparing) setCursor(order.stepGroup(current, action.by));
        else if (!compareGroup(action.by)) {
          review.notify(`Ei ${action.by > 0 ? 'seuraavaa' : 'edellistä'} vertailtavaa ryhmää.`);
        }
        break;
      case 'keep': review.decide(current, { user_verdict: 'keep' }); break;
      case 'reject': review.decide(current, { user_verdict: 'reject' }); break;
      case 'rate': review.decide(current, { rating: action.stars }); break;
      case 'reset': review.decide(current, { user_verdict: null, rating: null }); break;
      case 'accept': {
        const group = order.group(current);
        review.accept(group);
        if (comparing) {
          if (!compareGroup(1)) {
            // Space skips the shots that cannot be compared: say how many are left to review in the grid
            let left = 0;
            for (const [id, s] of shots) if (s.user_verdict == null && !group.members.includes(id)) left += 1;
            review.notify(`Tämä oli viimeinen vertailtava ryhmä. ${left > 0
              ? `Ruudukossa on vielä ${fmt(left)} kuvaa ilman päätöstä. ` : ''}Esc palaa ruudukkoon.`);
          }
          break;
        }
        const next = order.stepGroup(current, 1);
        if (order.group(next) !== group) setCursor(next);
        break;
      }
      case 'undo': {
        const back = review.undo().find((id) => order.index.has(id));
        if (back == null) break;
        setCursor(back);
        if (comparing && !comparable(order.group(back))) {
          // the undone shot cannot be compared: show it in the loupe instead
          location.hash = `${grid}/loupe/${back}`;
          review.notify('Kumottu päätös on kuvalla, jota ei voi vertailla; se näkyy kuvanäkymässä.');
        }
        break;
      }
      case 'open':
        open(current);
        break;
      case 'compare':
        if (currentGroup?.groupId == null || currentGroup.members.length < 2) {
          review.notify('Kuvalla ei ole ryhmää, jonka kuvia vertailla.');
        } else if (currentGroup.ids.length < 2) {
          review.notify('Ryhmästä näkyy suodattimien jälkeen vain tämä kuva.');
        } else location.hash = `${grid}/compare/${currentGroup.groupId}`;
        break;
      default:
        return false;
    }
    return true;
  });

  const ev = data?.event;
  const toggle = (key) => setFilters((f) => ({ ...f, [key]: !f[key] }));
  const nextGroup = comparing && current != null ? comparableGroup(order, current, 1) : null;
  return html`<main class=${`page wide${route.view === 'event' ? '' : ' loupe-page'}`}>
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
      ${route.view === 'event' && ev && shots.size > 0 && html`<button class="apply-button"
        onClick=${() => setApplying(true)} title="Siirrä hylätyt kuvat karsittuihin ja kirjoita päätökset XMP:hen">
        Toteuta</button>`}
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
    ${data && route.view === 'loupe' && (current == null
      ? html`<p class="muted">Ei näytettävää kuvaa. <a href=${grid}>Takaisin ruudukkoon</a></p>`
      : html`<${Loupe} shot=${shots.get(current)} best=${best.get(currentGroup.key)} keys=${viewKeys}
          position=${position(order, current, filters)}
          neighbours=${[order.step(current, -1), order.step(current, 1)].filter((id) => id !== current)
            .map((id) => shots.get(id))} />`)}
    ${data && comparing && (!comparable(currentGroup)
      ? html`<p class="muted">Ei vertailtavaa: ${current == null ? 'ei näytettäviä kuvia' : html`kuvalla ei ole ryhmää,
          tai suodattimet jättävät ryhmästä näkyviin vain sen. <a href=${`${grid}/loupe/${current}`}>Avaa kuva</a> ·`}
          <a href=${grid}>Takaisin ruudukkoon</a></p>`
      : html`<${Compare} group=${currentGroup} ids=${compared} shots=${shots} current=${current}
          best=${best.get(currentGroup.key)} position=${comparePosition(order, current, filters)} keys=${viewKeys}
          next=${nextGroup ? bestFirst(nextGroup.ids, shots).slice(0, MAX_PANES) : []} onPick=${setCursor}
          notify=${review.notify} />`)}
    ${data && route.view === 'event' && html`<${ApplyHistory} eventId=${eventId} version=${version}
      onChanged=${changed} />`}
    ${help && html`<${ShortcutHelp} onClose=${() => setHelp(false)} />`}
    ${applying && ev && html`<${ApplyDialog} ev=${ev} settled=${review.settled} onClose=${() => setApplying(false)}
      onChanged=${changed} onRunning=${setApplyRunning} onDone=${review.notify} />`}
  </main>`;
}
