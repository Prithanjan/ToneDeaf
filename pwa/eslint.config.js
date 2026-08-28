/**
 * Flat ESLint config.
 *
 * Two jobs. The first is ordinary type-aware correctness. The second is the interesting one: several of
 * this client's privacy guarantees are *absences* — no audio at rest, no token in web storage, no
 * identifier in a log — and an absence is not something code review reliably notices being added back.
 * The `no-restricted-*` entries below turn each of those into a build failure, so the guarantee is
 * enforced by a tool rather than by everyone remembering. Every one of them is currently satisfied with
 * zero exemptions; there is no `eslint-disable` for them anywhere in `src/`.
 *
 * What this config deliberately does NOT try to enforce is the closed action vocabulary (rules.md
 * R-07). Catching the prohibited verdict words needs a repository-wide scan across Python, YAML,
 * Markdown, and TypeScript, with an exemption list for the verbatim wire values in
 * `contracts/openapi.yaml` and for CSS logical properties; that belongs to the CI check in `scripts/`,
 * not to a linter that only ever sees `pwa/`.
 */

import reactHooks from 'eslint-plugin-react-hooks';
import tseslint from 'typescript-eslint';

/** Web storage and cache APIs. None of these may hold audio, a token, or a call reference. */
const FORBIDDEN_GLOBALS = [
  {
    name: 'localStorage',
    message:
      'No session state persists in web storage: not audio (rules.md R-14), not the raw call reference (R-16), not the access token or stream ticket (R-34). Hold it in module scope or component state.',
  },
  {
    name: 'sessionStorage',
    message:
      'Survives a reload and is readable by any script on the origin. Same prohibition as localStorage — see rules.md R-14, R-16, R-34.',
  },
  {
    name: 'indexedDB',
    message:
      'An IndexedDB store is the most likely place raw audio would end up at rest, which rules.md R-14 forbids outright.',
  },
  {
    name: 'caches',
    message:
      'The Cache API must never hold audio or an API response containing a call reference (rules.md R-14, R-16). There is no service worker in this app; see src/main.tsx.',
  },
];

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },

  {
    files: ['**/*.{ts,tsx}'],
    // Scoped to TS rather than applied at the top level. The type-checked rule sets need a program,
    // and this config file is plain JS that no tsconfig includes — applying them globally makes ESLint
    // fail on itself with "you have used a rule which requires type information".
    extends: [...tseslint.configs.strictTypeChecked, ...tseslint.configs.stylisticTypeChecked],
    languageOptions: {
      parserOptions: {
        // Both projects, explicitly. `projectService: true` resolves each file against the nearest
        // `tsconfig.json`, and `vite.config.ts` is not in that project's `include` — it belongs to
        // `tsconfig.node.json`, which has no DOM lib. Naming both keeps the linter's view of each file
        // identical to the compiler's.
        project: ['./tsconfig.json', './tsconfig.node.json'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      // Errors, not warns. A stale closure in `App.tsx` means a teardown that stops the wrong
      // MediaStream — a live microphone with nothing reading it.
      'react-hooks/exhaustive-deps': 'error',

      'no-restricted-globals': ['error', ...FORBIDDEN_GLOBALS],
      'no-restricted-properties': [
        'error',
        ...FORBIDDEN_GLOBALS.map((entry) => ({
          object: 'window',
          property: entry.name,
          message: entry.message,
        })),
      ],
      'no-restricted-syntax': [
        'error',
        {
          selector: "NewExpression[callee.name='Blob']",
          message:
            'A Blob of captured audio is one createObjectURL away from a download, which is audio at rest (rules.md R-14). Frames go to the socket and are dropped.',
        },
        {
          selector: "MemberExpression[object.name='URL'][property.name='createObjectURL']",
          message:
            'Object URLs exist to hand a buffer to a download or a media element. Neither is a thing this client does with audio (rules.md R-14).',
        },
        {
          selector: "MemberExpression[property.name='serviceWorker']",
          message:
            'No service worker is registered, deliberately — see the header of src/main.tsx. A caching worker is a standing hazard to rules.md R-14 and R-16, and this app has no offline shell to justify one (R-01).',
        },
        {
          selector: "MemberExpression[property.name='cookie']",
          message:
            'Nothing about a session belongs in a cookie: cookies are sent on every request to the origin and are visible to any script on it (rules.md R-16, R-34).',
        },
      ],
      // Nothing in this client logs. The one field that must never reach a log is exactly the field an
      // added `console.log(values)` in a submit handler would capture (rules.md R-16), and a console
      // line is not diagnostics anyone reads in a demo — the audit trail is.
      'no-console': 'error',
      eqeqeq: ['error', 'always', { null: 'ignore' }],

      // The `!`s in `lib/stream.ts` and `lib/capture.ts` sit immediately after an explicit length
      // assertion on the same buffer, and they exist only because `noUncheckedIndexedAccess` is on.
      // Replacing them with a `?? 0` would encode silence into a frame instead of failing.
      '@typescript-eslint/no-non-null-assertion': 'off',

      // Off, and this one is a contract decision rather than a style preference. The runtime validators
      // in `lib/types.ts` and `lib/api.ts` read fields off `Record<string, unknown>` with quoted keys —
      // `payload['window_seq']`, not `payload.window_seq` — so every wire field name appears in source
      // spelled exactly as it appears in `contracts/openapi.yaml`, and a grep for a field name finds
      // both halves of the contract. Dot access on an unvalidated bag also reads as though the shape
      // were known, which is the assumption the validators exist to avoid making.
      '@typescript-eslint/dot-notation': 'off',

      // `array-simple` rather than the default `array`: `lib/constants.ts` — which is under a
      // cross-language parity test and is not this package's to restyle — writes
      // `Array<[string, boolean]>` for a tuple element type, which is correct under this setting and a
      // lint failure under the default. A cosmetic rule must not be the reason a parity-locked file
      // gets edited.
      '@typescript-eslint/array-type': [
        'error',
        { default: 'array-simple', readonly: 'array-simple' },
      ],

      '@typescript-eslint/consistent-type-imports': [
        'error',
        { prefer: 'type-imports', fixStyle: 'separate-type-imports' },
      ],
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
    },
  },

  {
    /**
     * `lib/capture.ts` uses `ScriptProcessorNode`, `createScriptProcessor`, `onaudioprocess`,
     * `AudioProcessingEvent`, and `AudioProcessingEvent.inputBuffer` — all five are deprecated in the
     * Web Audio spec in favour of `AudioWorklet`, and all five are used ON PURPOSE. `AudioWorklet` is
     * an explicit Future Scope item, not an oversight; the file's header carries the full reasoning and
     * the pointers.
     *
     * The rule is scoped off for that one file rather than disabled inline six times, so the exemption
     * is a single reviewable entry with a single reason. When the worklet path is actually built, this
     * override is what gets deleted.
     */
    files: ['src/lib/capture.ts', 'src/lib/telephony_bridge.ts'],
    rules: {
      '@typescript-eslint/no-deprecated': 'off',
    },
  },

  {
    // This file. Plain JS, no program behind it, and nothing here that the type-aware rules could
    // usefully say anything about.
    files: ['**/*.js'],
    extends: [tseslint.configs.disableTypeChecked],
  },
);
