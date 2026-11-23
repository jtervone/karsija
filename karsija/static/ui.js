// Preact + htm from the vendored copies: no build step, nothing from the network.
import { Component, h, render } from './vendor/preact.js';
import htm from './vendor/htm.js';

export { Component, render };
export { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from './vendor/hooks.js';
export const html = htm.bind(h);

const numbers = new Intl.NumberFormat('fi-FI');
const dates = new Intl.DateTimeFormat('fi-FI', { dateStyle: 'short', timeStyle: 'short' });

export const fmt = (n) => numbers.format(n);
export const fmtTime = (seconds) => dates.format(new Date(seconds * 1000));

const preloaded = new Set();
/** Load an image into the browser's cache, once. */
export function preload(url) {
  if (preloaded.has(url)) return;
  if (preloaded.size > 5000) preloaded.clear();
  preloaded.add(url);
  new Image().src = url;
}
