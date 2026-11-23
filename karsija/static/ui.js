// Preact + htm from the vendored copies: no build step, nothing from the network.
import { Component, h, render } from './vendor/preact.js';
import htm from './vendor/htm.js';

export { Component, render };
export { useCallback, useEffect, useMemo, useReducer, useRef, useState } from './vendor/hooks.js';
export const html = htm.bind(h);

const numbers = new Intl.NumberFormat('fi-FI');
const dates = new Intl.DateTimeFormat('fi-FI', { dateStyle: 'short', timeStyle: 'short' });

export const fmt = (n) => numbers.format(n);
export const fmtTime = (seconds) => dates.format(new Date(seconds * 1000));
