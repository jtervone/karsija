// Tapahtuma grid: the shots of an event as cards, similar shots together in a box per group.
import { Component, html } from './ui.js';
import { SUGGESTION } from './review.js';

export const VERDICTS = { best: 'paras', keep: 'hyvä', similar: 'samankaltainen', blurry: 'epäterävä', error: 'virhe' };
const DECISIONS = { keep: 'pidä', reject: 'hylkää' };

export function stars(rating) {
  return rating === 0 ? '☆' : '★'.repeat(rating);
}

/**
 * One shot. Its colour is the automatic verdict, the corner badge the user's own decision. Re-renders only when its
 * shot (a new object when its decision changes), being current, or its group's best sharpness changes, so moving or
 * deciding touches one or two cards of a long event. The current card scrolls itself into view.
 */
class Card extends Component {
  shouldComponentUpdate(next) {
    return next.shot !== this.props.shot || next.current !== this.props.current || next.best !== this.props.best;
  }

  setEl = (el) => { this.el = el; };

  componentDidMount() {
    if (this.props.current) this.el.scrollIntoView({ block: 'nearest' });
  }

  componentDidUpdate(prev) {
    if (this.props.current && !prev.current) this.el.scrollIntoView({ block: 'nearest' });
  }

  render({ shot, current, best, onPick, onOpen }) {
    const auto = shot.auto_verdict;
    const suggestion = SUGGESTION[auto];
    const decided = shot.user_verdict;
    const ratio = best > 0 && !shot.error ? shot.sharp / best : null;
    return html`<figure ref=${this.setEl} class=${`card verdict-${auto ?? 'none'}${current ? ' current' : ''}${decided ? ` ${decided}` : ''}`}
        data-shot=${shot.id} aria-current=${current ? 'true' : undefined}
        onClick=${() => onPick(shot.id)} onDblClick=${() => onOpen(shot.id)}>
      <div class="thumb">
        <img src=${shot.preview.grid} alt="" loading="lazy" decoding="async" draggable="false" />
        ${decided && html`<span class=${`decision ${decided}`} title=${`oma päätös: ${DECISIONS[decided]}`}>
          ${decided === 'keep' ? '✓' : '✕'}</span>`}
        ${shot.rating != null && html`<span class="stars" title=${`${shot.rating} tähteä`}>${stars(shot.rating)}</span>`}
      </div>
      <figcaption>
        <div class="line">
          <span class="auto">${VERDICTS[auto] ?? 'ei arviota'}${suggestion && html` → ${DECISIONS[suggestion]}`}</span>
          ${!shot.error && html`<span class="sharp" title="terävyys${ratio != null ? ' ja osuus ryhmän terävimmästä' : ''}">
            ${Math.round(shot.sharp)}${ratio != null && ratio < 1 ? ` · ${Math.round(ratio * 100)} %` : ''}</span>`}
        </div>
        <div class="reason" title=${shot.reason || undefined}>${shot.reason || ' '}</div>
        <div class="name" title=${shot.path}>${shot.name}</div>
      </figcaption>
    </figure>`;
  }
}

/** Sharpness of the sharpest analysed shot of each group of more than one shot, by group key. */
export function groupBest(groups, shots) {
  const out = new Map();
  for (const g of groups) {
    if (g.ids.length < 2) continue;
    const sharp = g.ids.map((id) => shots.get(id)).filter((s) => s && !s.error).map((s) => s.sharp);
    if (sharp.length) out.set(g.key, Math.max(...sharp));
  }
  return out;
}

/** A group in its box, or a single shot. Re-renders only when one of its shots changes or the cursor enters or leaves. */
class Block extends Component {
  shouldComponentUpdate(next) {
    const { group, shots, cursor, best } = this.props;
    if (next.group !== group || next.best !== best) return true;
    const had = group.ids.includes(cursor);
    if (had !== next.group.ids.includes(next.cursor) || (had && cursor !== next.cursor)) return true;
    return group.ids.some((id) => shots.get(id) !== next.shots.get(id));
  }

  render({ group, shots, cursor, best, onPick, onOpen }) {
    const card = (id) => html`<${Card} key=${id} shot=${shots.get(id)} current=${id === cursor} best=${best}
      onPick=${onPick} onOpen=${onOpen} />`;
    if (group.members.length < 2) return card(group.ids[0]);
    const hidden = group.members.length - group.ids.length;
    return html`<section class="group">
      <header>${group.members.length} samankaltaista${hidden > 0 && html` <span class="muted">(${hidden} piilossa)</span>`}</header>
      <div class="cards">${group.ids.map(card)}</div>
    </section>`;
  }
}

/**
 * `groups`: the shown groups (see `filterGroups`), `shots` by id, `best` from `groupBest`, `cursor` the current
 * shot. A group of one shot is a card without a box.
 */
export function Grid({ groups, shots, best, cursor, onPick, onOpen }) {
  return html`<div class="grid">
    ${groups.map((g) => html`<${Block} key=${g.key} group=${g} shots=${shots} cursor=${cursor} best=${best.get(g.key)}
      onPick=${onPick} onOpen=${onOpen} />`)}
  </div>`;
}
