# G-Code Language Server

Semantic diagnostics, hover docs, and completions for CNC G-code — "the
linter G-code never had." A Python language server does all the thinking;
a ~40-line TypeScript shim connects it to VS Code. Because the smart part
speaks the Language Server Protocol, the same server also works in Neovim,
Kate, and Zed.

> This project was scaffolded from a Claude chat design session — the full
> transcript is in [docs/claude-chat-transcript.md](docs/claude-chat-transcript.md).

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
   is also the exact spot where an existing parser (e.g. gcode-tools) plugs
   in: keep `GCodeParser.check_line()` and nothing else changes.
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

**Honest caveat:** these are Fanuc-flavored starting points, written to be
tuned by someone who actually runs machines. They're data-driven on purpose
— enabling/disabling rules per dialect and expanding the code tables all
happens in `dialects.py` without touching the engine.

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

Magic comment example (first 5 lines of the file):

```gcode
(DIALECT: SIEMENS)
```

See it working: `examples/demo_marlin.gcode` and `examples/demo_siemens.nc`.

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
