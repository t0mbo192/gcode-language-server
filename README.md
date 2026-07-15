# G-Code Language Server

Semantic diagnostics, hover docs, and completions for CNC G-code — "the
linter G-code never had." A Python language server does all the thinking;
a ~40-line TypeScript shim connects it to VS Code. Because the smart part
speaks the Language Server Protocol, the same server also works in Neovim,
Kate, and Zed.

## Why this exists

The popular G-Code Syntax extension is a colorizer and counter: TextMate
grammar plus pattern matching. It has no model of machine state, so it can't
tell you that a `G1` has no feedrate, that cutter comp is still active at
`M30`, or that you moved Z after a tool change without `G43`. Catching those
requires tracking **modal state** line by line — which is exactly what a
linter-style language server can do and a grammar never can.

## Architecture

Two processes talking JSON-RPC over stdin/stdout:

```
┌────────────────────────┐        LSP (JSON-RPC on stdio)        ┌──────────────────────────┐
│ VS Code                │  ──── textDocument/didOpen ────────▶  │ python server/server.py  │
│                        │  ──── textDocument/didChange ──────▶  │   (pygls glue — no       │
│  src/extension.ts      │  ◀─── publishDiagnostics ──────────   │    G-code knowledge)     │
│  (thin shim: spawns    │  ──── textDocument/hover ──────────▶  │        │                 │
│   the server, nothing  │  ◀─── markdown tooltip ────────────   │        ▼                 │
│   else)                │                                       │  gcode_parser.py         │
│                        │                                       │  (modal-state engine)    │
│  syntaxes/*.json       │                                       │        │                 │
│  (static colors only — │                                       │        ▼                 │
│   NOT the LSP)         │                                       │  dialects.py             │
└────────────────────────┘                                       │  (rules + docs as data)  │
                                                                 └──────────────────────────┘
```

The lifecycle, message by message:

1. You open `part1.nc` → VS Code sees `.nc` registered to language `gcode`
   (in `package.json`) → activates the extension.
2. `extension.ts` spawns `python server/server.py` as a child process.
3. Handshake: VS Code sends `initialize`; the server replies "I support
   diagnostics, hover, completion."
4. VS Code sends `textDocument/didOpen` with the full file text.
5. The server walks the modal state line by line and pushes back
   `publishDiagnostics` — VS Code draws the squiggles and fills Problems.
6. Every edit → `didChange` → debounced 300 ms → re-lint. Hover on `G43` →
   `hover` request → markdown tooltip from the dialect tables.

## Reading order for a code review

1. **[server/gcode_parser.py](server/gcode_parser.py)** — the modal-state
   engine. Zero dependencies, heavily commented, runnable standalone. This
   is also the exact spot where a different parsing engine would plug in:
   keep `GCodeParser.check_line()` and nothing else changes.
2. **[server/dialects.py](server/dialects.py)** — every dialect fact as
   plain data tables: which rules apply, which codes exist, hover text.
3. **[server/server.py](server/server.py)** — the pygls glue. Translation
   only, no G-code knowledge.
4. **[src/extension.ts](src/extension.ts)** — the entire VS Code side.

## Quick start

```powershell
# Prove the engine first — no editor involved:
python server\gcode_parser.py examples\demo.nc

# Then the full extension:
pip install -r server\requirements.txt
npm install
npm run compile
code .
# press F5 → an Extension Development Host window opens
# open examples\demo.nc → squiggles
```

`examples/demo.nc` contains 7 deliberate mistakes, each marked with an
arrow comment. Expected output from the CLI run:

```
examples/demo.nc — dialect: fanuc
  line 8:  warning [no-g43-after-toolchange]
  line 9:  error   [g43-missing-h]
  line 11: warning [feed-missing]
  line 13: error   [arc-missing-center]
  line 17: warning [spindle-off]
  line 18: info    [unknown-code]
  line 19: warning [comp-active-at-end]
  7 problem(s) found
```

## The lint rules

| rule id | severity | what it catches |
|---|---|---|
| `feed-missing` | warning | `G1/G2/G3` cutting move with no `F` ever set |
| `spindle-off` | warning | cutting move while the spindle is stopped |
| `g43-missing-h` | error | `G43`/`G44` without an `H` word |
| `no-g43-after-toolchange` | warning | Z motion after `M6` before any `G43` |
| `comp-active-at-end` | warning | `G41`/`G42` still active at `M2`/`M30` |
| `arc-missing-center` | error | `G2`/`G3` written with no `I`/`J`/`K`/`R` |
| `unknown-code` | info | G/M code not in the dialect's table |
| `no-coolant-for-tool` | warning | a tool's first cut with no coolant-on code since its `M6` (which codes count is per-dialect) |

**Honest caveat:** these are Fanuc-flavored starting points, written to be
tuned by someone who actually runs machines. They're data-driven on purpose
— enabling/disabling rules per dialect and expanding the code tables all
happens in `dialects.py` without touching the engine.

### A rule from the shop floor: the coolant check

`no-coolant-for-tool` is the first rule contributed from running real
machines rather than from a textbook: **every tool must turn its coolant on
(`M7`/`M8`) before its first cut.** Details that matter:

- It fires **once per tool change**, on the first cutting move or canned
  cycle (`G81`...) after the `M6` — not on every dry line, because
  intentional dry cutting exists (cast iron, graphite). That's also why
  it's a warning, not an error.
- Coolant on the same line as the cut counts, just like a same-line `F`.
- Which M-codes mean "coolant" is **per-dialect data** (`coolant_on` /
  `coolant_off` on each `Dialect`), because numbers collide across
  controls: `M51` is through-spindle coolant on a Mazak but a
  spindle-override switch on LinuxCNC. The Mazak dialect ships with
  `M7/M8/M50/M51` on and `M9/M163` off; add your machines' codes there.
- **Through-spindle-coolant-only tools are not false positives.** A
  coolant-through drill in a Haas that programs `M88` and never `M8` is
  doing it right, so `M88` is in the Haas `coolant_on` set (as are the air
  options `M73`/`M83` — air to the cut is a strategy, not a forgotten
  `M8`). Same idea on Mazak with `M50`/`M51`.
- **Combo codes work.** Heidenhain's `M13`/`M14` mean spindle-on *and*
  coolant-on in one number; they sit in both the dialect's `spindle_on`
  and `coolant_on` sets and the engine credits both. On Heidenhain the
  check also re-arms on the `T` word itself, because a TNC tool call *is*
  the tool change — there's no `M6` to hang it on.
- Marlin (3D printing) deliberately skips it — `M106` is a fan, not
  coolant.

`examples/demo_coolant.nc` walks through all three cases — a tool that does
it right, a tool that cuts dry, and a dry drilling cycle:

```
examples/demo_coolant.nc — dialect: fanuc
  line 21: warning [no-coolant-for-tool]   T2 starts cutting dry
  line 29: warning [no-coolant-for-tool]   T3 drills dry (canned cycles count)
  2 problem(s) found
```

## File recognition

Everything keys off the language id, not the extension, so recognition
lives in exactly one array in `package.json`. Three mechanisms, in order:

1. **`extensions`** — `.nc .cnc .ngc .tap .gcode .gc .mpf .spf .eia .ptp
   .min .din .iso .hnc .ncc .prg`
2. **`filenamePatterns`** — catches Fanuc-style programs saved as bare
   `O1234` with no extension at all
3. **`firstLine`** — content sniffing: a lone `%` on line 1 (kept tight as
   `^%\s*$` so it doesn't claim PostScript or MATLAB files)

Shop-specific oddballs can be mapped without republishing, in VS Code
settings:

```json
"files.associations": {
  "*.uni": "gcode",
  "MOLD*": "gcode"
}
```

## Dialects

Priority when picking a file's dialect: the `gcode.dialect` setting (if not
`auto`) → a magic comment near the top of the file → the file extension →
Fanuc as the default.

| dialect | selected by | notable differences |
|---|---|---|
| `fanuc` | default | full rule set |
| `siemens` | `.mpf` `.spf` | drops the G43 rules (length comp comes from the tool edge); `G70/G71` are inch/metric input, not lathe cycles |
| `linuxcnc` | `.ngc` | adds `G33`, `G38.2`, `G64`, `G76`, `M62/M63` |
| `marlin` | `.gcode` `.gc` | no spindle/comp rules; printer M-codes (`M104`, `M109`, ...); `M30` deletes an SD file(!) |
| `okuma` | `.min` | adds `G15/G16` work-coordinate codes |
| `mazak` | `.eia` | Fanuc-like G side; the coolant rule accepts the full Mazak coolant family — `M51` through-spindle, `M50` air blast, `M163` TSC off. Mazak M-codes vary by model — verify the table against your machine |
| `haas` | magic comment or setting only (Haas posts write plain `.nc`) | Fanuc-like plus Haas G-codes (`G12/G13` circular pockets, `G70–G72` bolt patterns(!), `G103`, `G154` offsets, `G187`, `G234/G254/G255`); coolant rule accepts the whole Haas family — `M88/M89` TSC, `M73/M74` through-tool air, `M83/M84` air jet, `M7` shower — so a TSC-only tool programming `M88` alone passes |
| `heidenhain` | `.i` | TNC controls in DIN/ISO mode. The `T` word IS the tool change (no `M6`), so the coolant check re-arms on it — except on `G99` tool-definition and `G51` preselect lines; `M13`/`M14` are combo codes counted as spindle **and** coolant; cycles are define-then-call (`G200`… stores, `G79` cuts). Full of traps the hovers call out: `G28` = mirror, `G43/G44` = paraxial comp, `G54` = datum shift, `G98/G99` = label/tool-def, `M99` = cycle call |
| `klartext` | `.h` `.hnc` (see note below) | Heidenhain's conversational format (`L X+30 RL F250`, `TOOL CALL 5`) — **not G-code**. Every rule is deliberately off: a Klartext file gets no squiggles instead of wrong ones. Real Klartext linting needs its own parser — future work |

Magic comment example (first 5 lines of the file):

```gcode
(DIALECT: SIEMENS)
```

**The `.h` note:** VS Code isn't told to claim `.h` files (that would hijack
every C header on your machine), so Klartext files don't open as G-code out
of the box. In a workspace that only holds NC programs, opt in yourself:

```jsonc
// .vscode/settings.json
"files.associations": { "*.h": "gcode" }
```

The server then maps `.h` → `klartext` on its own. `.hnc` is already claimed
and assumed to be Klartext too — if your `.hnc` files are ISO G-code, say so
with a magic comment or the `gcode.dialect` setting.

See it working: `examples/demo_marlin.gcode`, `examples/demo_siemens.nc`,
`examples/demo_mazak.eia` (through-spindle coolant satisfying the coolant
rule), `examples/demo_haas.nc` (a TSC-only tool and an air-blast-only tool
passing, a genuinely dry tool flagged), `examples/demo_heidenhain.i` (T-word
tool changes, `M13`, define-then-call cycles), and `examples/demo_klartext.h`
(zero diagnostics on purpose).

## Troubleshooting (Windows)

- **No squiggles, and Output → "G-Code Language Server" shows a spawn
  error**: plain `python` isn't on PATH or resolves to the Microsoft Store
  stub. Set `gcode.pythonPath` to a full interpreter path, e.g.
  `C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python313\\python.exe`.
- **`ModuleNotFoundError: pygls`**: `pip install -r server\requirements.txt`
  into the same interpreter `gcode.pythonPath` points at.
- **Squiggles lag while typing**: intended — `didChange` is debounced 300 ms
  (`_DEBOUNCE_SECONDS` in `server.py`) because CAM posts can be megabytes.
- **Wrong dialect chosen**: check the priority list above; the `source`
  field of every squiggle shows which dialect produced it, e.g.
  `gcode-ls (fanuc)`.

## Packaging (later)

`vsce package` produces a `.vsix` for the Marketplace, but users need
Python installed; bundling the server with PyInstaller removes that
requirement. Not wired up yet — deliberately, while the code is still
being reviewed and tuned.
