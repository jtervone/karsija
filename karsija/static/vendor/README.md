# Vendored libraries

The UI has no build step and loads nothing from the network: these ES modules are copied from the npm packages
as they are, with two changes: the `//# sourceMappingURL` comment is removed (the maps are not vendored) and
`hooks.js` imports `./preact.js` instead of the bare `preact`.

| File        | Package | Version | Source file in the package | Licence                  |
|-------------|---------|---------|----------------------------|--------------------------|
| `preact.js` | preact  | 11.2.0  | `dist/preact.mjs`          | MIT, `LICENSE-preact`    |
| `hooks.js`  | preact  | 11.2.0  | `hooks/dist/hooks.mjs`     | MIT, `LICENSE-preact`    |
| `htm.js`    | htm     | 3.1.1   | `dist/htm.mjs`             | Apache-2.0, `LICENSE-htm` |

To update: `npm pack preact htm`, unpack, copy the files above and repeat the two changes.
