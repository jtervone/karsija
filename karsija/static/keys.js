// Keyboard layer shared by the review views (grid, loupe, compare): which key does what, and the help overlay.
import { html, useEffect, useRef } from './ui.js';

/** The shortcuts as the help overlay lists them. */
export const SHORTCUTS = [
  ['← →', 'edellinen / seuraava kuva'],
  ['↑ ↓', 'edellinen / seuraava ryhmä'],
  ['P', 'pidä'],
  ['X', 'hylkää'],
  ['0–5', 'tähdet'],
  ['U', 'takaisin ehdotukseen (poistaa oman päätöksen ja tähdet)'],
  ['Välilyönti', 'hyväksy ryhmän ehdotus ja siirry seuraavaan ryhmään'],
  ['Enter', 'avaa kuva'],
  ['C', 'vertaile ryhmän kuvia'],
  ['Z', '100 % / sovita (kuva- ja vertailunäkymässä)'],
  ['Esc', 'takaisin ruudukkoon'],
  ['Ctrl+Z', 'kumoa viimeisin päätös'],
  ['?', 'näytä tai piilota pikanäppäimet'],
];

const KEYS = {
  ArrowLeft: { type: 'move', by: -1 },
  ArrowRight: { type: 'move', by: 1 },
  ArrowUp: { type: 'group', by: -1 },
  ArrowDown: { type: 'group', by: 1 },
  p: { type: 'keep' },
  x: { type: 'reject' },
  u: { type: 'reset' },
  z: { type: 'zoom' },
  c: { type: 'compare' },
  ' ': { type: 'accept' },
  Enter: { type: 'open' },
  Escape: { type: 'back' },
  '?': { type: 'help' },
};

/**
 * Action of a `keydown` event, or null when the key means nothing here. `{ type }` is one of move and group (with
 * `by` ±1), keep, reject, rate (with `stars` 0–5), reset, zoom, compare, accept, open, back, undo, help.
 * Only moving repeats while a key is held: holding P or Space must not decide shot after shot.
 */
export function keyAction(e) {
  if (e.altKey || e.isComposing) return null;
  if (e.ctrlKey || e.metaKey) {
    return !e.shiftKey && !e.repeat && e.key.toLowerCase() === 'z' ? { type: 'undo' } : null;
  }
  let action = KEYS[e.key] ?? KEYS[e.key.toLowerCase()] ?? null;
  if (!action && /^[0-5]$/.test(e.key)) action = { type: 'rate', stars: Number(e.key) };
  if (action && e.repeat && action.type !== 'move' && action.type !== 'group') return null;
  return action;
}

/** Elements whose own keys come first: text fields, and Enter on a link or button. */
function ownKey(e) {
  const el = e.target;
  if (!(el instanceof Element)) return false;
  if (el.closest('textarea, select, [contenteditable=""], [contenteditable="true"]')) return true;
  if (el.matches('input') && !['checkbox', 'radio', 'button'].includes(el.type)) return true;
  return e.key === 'Enter' && !!el.closest('a, button');
}

/**
 * Calls `onAction(action)` for every shortcut pressed on the page. The key's default (scrolling, a checkbox
 * toggled by Space) is prevented unless `onAction` returns false.
 */
export function useKeys(onAction) {
  const handler = useRef(onAction);
  handler.current = onAction;
  useEffect(() => {
    const onKey = (e) => {
      if (e.defaultPrevented || ownKey(e)) return;
      const action = keyAction(e);
      if (action && handler.current(action) !== false) e.preventDefault();
    };
    addEventListener('keydown', onKey);
    return () => removeEventListener('keydown', onKey);
  }, []);
}

export function ShortcutHelp({ onClose }) {
  return html`<div class="overlay" onClick=${onClose}>
    <section class="help" role="dialog" aria-label="Pikanäppäimet" onClick=${(e) => e.stopPropagation()}>
      <h2>Pikanäppäimet</h2>
      <dl>${SHORTCUTS.map(([keys, text]) => html`<dt><kbd>${keys}</kbd></dt><dd>${text}</dd>`)}</dl>
      <p class="muted">Kortin väri on automaattinen arvio, merkki kortin kulmassa oma päätöksesi.</p>
      <button onClick=${onClose}>Sulje</button>
    </section>
  </div>`;
}
