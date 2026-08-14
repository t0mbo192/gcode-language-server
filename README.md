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

## Installing

- **Marketplace**: search "G-Code Language Server" (publisher `t0mbo192`).
  The platform-specific builds (Windows x64, Linux x64, macOS Apple
  Silicon) contain a standalone server executable — **no Python required**.
  There is no bundled build for Intel Macs: PyInstaller can't
  cross-compile and GitHub no longer allocates its Intel runners, so those
  machines take the universal build below.
- **From a `.vsix` file** (offline / locked-down work machines): Extensions
  panel → `···` menu → *Install from VSIX…*, or
  `code --install-extension gcode-language-server-win32-x64-0.3.0.vsix`.
  Prefer the platform-specific file — it's self-contained. The universal
  file works anywhere but needs Python 3 plus
  `pip install "pygls>=1.3,<2.0"` on the machine.

## Quick start (development)

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

## Tests

```powershell
python -m unittest discover -s tests -t tests -v
# or: npm test
```

133 tests, no test framework to install — the engine has zero dependencies
and its tests keep it that way. `tests/test_server.py` needs `pygls` (it
tests the LSP layer) and skips itself cleanly if you haven't installed it.
CI runs the suite on Python 3.9, 3.12 and 3.13 before any `.vsix` is built.

| file | what it covers |
|---|---|
| `test_gcode_parser.py` | tokenizing, modal state, one test per lint rule, and a malformed-input class — an editor buffer is linted on every keystroke, so it gets seen mid-word, mid-number and mid-paste |
| `test_dialects.py` | dialect detection priority, table **invariants**, and the M-code coverage each control is documented as having |
| `test_examples.py` | golden output for every file in `examples/` — the README quotes these, so they're pinned exactly |
| `test_server.py` | LSP translation: diagnostic ranges, hover, completion, debounce and document lifecycle |

The invariant tests in `test_dialects.py` are the ones that earn their keep.
`dialects.py` is hand-maintained tables, and the realistic failure isn't
"someone rewrote the engine", it's "someone pasted a code in the wrong
format at 2am". A key of `M08` instead of `M8` matches nothing, silently,
because every lookup goes through `normalize_code()`. So the suite asserts
things like *every table key is already in normalized form*, *every code the
engine treats as coolant is also documented*, and *`package.json`'s dialect
list equals `DIALECTS`* — checks that hold for edits nobody has written a
test for yet.

That last kind found a real bug the first time it ran: `CANNED_CYCLES` told
the engine that `G73`, `G74`, `G76` and `G85`–`G89` start cutting, but
`_BASE_G` had never documented them — so `G73` peck drilling, about as
common as machining gets, was drawing an "unknown G-code" note on every
Fanuc-family program. They're documented now.

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
| `fanuc` | default | full rule set; Fanuc's own `M0–M30` plus the builder-assigned codes common across Fanuc-based machines — `M29` rigid tapping, `M48/M49` override cancel, `M41/M42` gear ranges, `M10/M11` rotary clamp, `M60` pallet change, `M198` DNC call |
| `siemens` | `.mpf` `.spf` | drops the G43 rules (length comp comes from the tool edge); `G70/G71` are inch/metric input, not lathe cycles; Siemens' predefined M set — `M17` end-of-subprogram, `M40–M45` gear stages, `M70` spindle-to-axis. `M98/M99` are **removed**: subprograms are called by name and return with `M17`/`RET`; the only dialect with **extended addressing** — `M2=3` is "spindle 2, code M3" |
| `linuxcnc` | `.ngc` | adds `G33`, `G38.2`, `G64`, `G76`, and the full RS-274/NGC M set — `M62–M65` synchronized vs. immediate digital output, `M66–M68` input wait and analog out, `M70–M73` modal-state stack, `M61` set-tool-without-changing, `M48–M53` overrides, plus the user-defined `M100–M199` block (documented and lint-clean, hidden from completion) |
| `marlin` | `.gcode` `.gc` | no spindle/comp rules; ~100 printer M-codes across temperature, SD, job control, motion tuning, probing/leveling, drivers and EEPROM; also the laser/router codes the same firmware implements (`M3/M4/M5`, `M7/M8/M9` air assist); `M30` deletes an SD file(!) and `M29` stops an SD write rather than arming rigid tapping |
| `okuma` | `.min` | adds `G15/G16` work-coordinate codes; M table deliberately minimal (see below) |
| `mazak` | `.eia` | Fanuc-like G side; the coolant rule accepts the full Mazak coolant family — `M51` through-spindle, `M50` air blast, `M163` TSC off. Mazak M-codes vary by model — verify the table against your machine |
| `haas` | magic comment or setting only (Haas posts write plain `.nc`) | Fanuc-like plus Haas G-codes (`G12/G13` circular pockets, `G70–G72` bolt patterns(!), `G103`, `G154` offsets, `G187`, `G234/G254/G255`); coolant rule accepts the whole Haas family — `M88/M89` TSC, `M73/M74` through-tool air, `M83/M84` air jet, `M7` shower — so a TSC-only tool programming `M88` alone passes |
| `heidenhain` | `.i` | TNC controls in DIN/ISO mode. The `T` word IS the tool change (no `M6`), so the coolant check re-arms on it — except on `G99` tool-definition and `G51` preselect lines; `M13`/`M14` are combo codes counted as spindle **and** coolant; cycles are define-then-call (`G200`… stores, `G79` cuts). Full of traps the hovers call out: `G28` = mirror, `G43/G44` = paraxial comp, `G54` = datum shift, `G98/G99` = label/tool-def, `M99` = cycle call |
| `klartext` | `.h` `.hnc` (see note below) | Heidenhain's conversational format (`L X+30 RL F250`, `TOOL CALL 5`) — **not G-code**. Every rule is deliberately off: a Klartext file gets no squiggles instead of wrong ones. Real Klartext linting needs its own parser — future work |

### Two kinds of M-code

The G-code side of a control is broadly standardised. The M-code side is
not, and it splits in two — which half you're looking at tells you how much
to trust any table, including the ones here:

- **Specified by the control.** `M0`–`M30` nearly everywhere, plus
  everything LinuxCNC and Marlin implement (open source, one
  implementation, one correct answer). Those hovers can be taken at face
  value.
- **Assigned by the machine builder.** Most numbers above `M30` on a Fanuc,
  Mazak, Okuma or Haas. Fanuc ships the control; Doosan decides that `M60`
  changes a pallet. The same number is a pallet change on one machine and a
  door opener on the next.

Builder-assigned codes say so in their hover text. **Check them against
your machine's own M-code list before trusting one** — that list is the
only authoritative document. Two places where this shaped the code:

- The `fanuc` dialect documents combo codes like `M13` (spindle *and*
  coolant on many Fanuc-based machines) but deliberately does **not** credit
  them as spindle-or-coolant. `fanuc` is also the fallback for files whose
  real control was never identified, and on a Haas `M13` releases the
  5th-axis brake — crediting it would silently suppress a real coolant
  warning. If your machine has the combo, add `M13`/`M14` to that dialect's
  `spindle_on` and `coolant_on` sets and it works immediately.
- The `okuma` M table is the thinnest in the file on purpose. OSP machines
  carry a long M list, but it's per-machine and per-option and the published
  lists disagree with each other. A wrong hover on a machinist's screen is
  worse than a missing one, so the guesses were left out for you to paste in
  from your own machine's list.

### Siemens extended addressing

One dialect changes what a *word* is, not just what a code means. On a
multi-spindle SINUMERIK — a turn-mill with a counter-spindle — an M-code is
aimed at one spindle by putting an index before the `=`:

```gcode
N80 S2=1500 M2=3     ; spindle 2: 1500 rpm, running clockwise
```

`M2=3` is **spindle 2, code M3**. Read as a plain letter-plus-number it's a
bare `M2`, which on this control is *program end* — so the modal state reset
mid-file and every line after it inherited a machine with no feed and no
spindle. A legal counter-spindle program raised six false warnings; it now
raises none.

The tokenizer only applies this to dialects that opt in, via
`Dialect.extended_address` (`{"M", "S"}` on Siemens, empty everywhere else),
so no other control's tokenizing changed. The spindle index itself is
discarded: `ModalState` tracks one spindle, so `M1=3` and `M2=3` both just
mean "a spindle is turning". Per-spindle state would be a modelling change
rather than a tokenizer one, and no rule needs it yet.
`examples/demo_siemens.nc` has a worked example.

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

See it working: `examples/demo_linuxcnc.ngc` (synchronized vs. immediate
outputs, the modal-state stack, a user-defined `M101`),
`examples/demo_marlin.gcode`, `examples/demo_siemens.nc`,
`examples/demo_mazak.eia` (through-spindle coolant satisfying the coolant
rule), `examples/demo_haas.nc` (a TSC-only tool and an air-blast-only tool
passing, a genuinely dry tool flagged), `examples/demo_heidenhain.i` (T-word
tool changes, `M13`, define-then-call cycles), and `examples/demo_klartext.h`
(zero diagnostics on purpose).

## Security

The threat model is small by construction, and worth stating so it stays
that way: **the server parses text and returns text.** It never executes
program content, never writes files, never opens a network connection, and
has no `eval`, `exec`, `pickle` or `subprocess` anywhere in it. G-code
arrives from an editor buffer — the least trustworthy input the project has,
since a file is linted on every keystroke and so is seen mid-word,
mid-number and mid-paste. Nothing on that path may raise or hang.

What that leaves, and what was done about it:

- **The interpreter path is machine-scoped.** `gcode.pythonPath` is spawned
  as a process, so `"scope": "machine"` in `package.json` means it can only
  be set in your own user settings. Without that, cloning a repo whose
  `.vscode/settings.json` set it to an arbitrary executable would run that
  executable the moment you opened a `.nc` file in the folder. The extension
  also declares `untrustedWorkspaces: supported`, which is honest once the
  path can't come from the workspace.
- **No shell.** `vscode-languageclient` is given a command and an argument
  array, never a command string, so there is no shell for a path to be
  injected through.
- **The regexes are linear.** The tokenizer runs on every line of
  multi-megabyte CAM output; a quadratic pattern would hang the server on a
  file you can't see is hostile. There is no nested quantifier in any of
  them, and `test_pathological_line_is_not_a_regex_bomb` keeps it that way.
- **Malformed numbers can't cost you the file's diagnostics.** Two crashes
  were found and fixed here. `normalize_code()` used `int()`, which refuses
  strings over 4300 digits on Python 3.11+ and raises `ValueError`; the T
  word used `int(float(...))`, which raises `OverflowError` once `float()`
  returns `inf`. Either exception unwound into `validate()`'s catch-all, so
  **one absurd line silently disabled linting for the whole file** — a
  linter that fails open is worse than one that fails loudly. Both now
  degrade to ignoring the unreadable word.
- **A malformed `didChangeConfiguration` can't take the handler down.**
  `{"gcode": null}` used to raise `AttributeError` mid-loop, leaving open
  files showing stale diagnostics. Every level of that payload is now
  type-checked, and `resolve_dialect()` ignores any dialect name it doesn't
  recognise.
- **Debounce timers don't outlive their document.** Closing a file now
  cancels its pending timer, so a keystroke made just before closing can't
  re-publish the squiggles that close cleared.

Not a vulnerability but worth knowing: the CI actions are pinned to major
versions (`actions/checkout@v4`), not commit SHAs. Pinning to SHAs would
harden the release pipeline against a compromised action.

## Troubleshooting (Windows)

- **No squiggles, and Output → "G-Code Language Server" shows a spawn
  error** (universal build or dev checkout only — platform builds carry
  their own server): plain `python` isn't on PATH or resolves to the
  Microsoft Store stub. Set `gcode.pythonPath` to a full interpreter path,
  e.g. `C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python313\\python.exe`.
- **`ModuleNotFoundError: pygls`**: `pip install -r server\requirements.txt`
  into the same interpreter `gcode.pythonPath` points at.
- **Bundled server ignored**: setting `gcode.pythonPath` to anything other
  than the default `python` deliberately overrides the bundled executable
  (it's the developer escape hatch for testing server changes). Reset the
  setting to go back to the bundled server.
- **Squiggles lag while typing**: intended — `didChange` is debounced 300 ms
  (`_DEBOUNCE_SECONDS` in `server.py`) because CAM posts can be megabytes.
- **Wrong dialect chosen**: check the priority list above; the `source`
  field of every squiggle shows which dialect produced it, e.g.
  `gcode-ls (fanuc)`.

## Packaging & releasing

Two package flavors come out of this repo:

- **Platform-specific** (`vsce package --target win32-x64` etc.) — carries
  a standalone server executable (`server/bin/gcode-ls(.exe)`) built with
  PyInstaller. Nothing to install on the user's machine — no Python, no pip.
- **Universal** — no binary inside; the extension falls back to
  `python server/server.py`, which needs Python 3 + pygls. Published as
  the catch-all for platforms without a native build.

How the extension picks a server at runtime (the logic lives in
[src/extension.ts](src/extension.ts)):

1. `gcode.pythonPath` set to anything but the default `python` → run
   `server/server.py` with that interpreter (developer escape hatch;
   beats the bundled exe on purpose).
2. A bundled `server/bin/gcode-ls(.exe)` exists → run it.
3. Otherwise → plain `python server/server.py`.

Local builds on Windows:

```powershell
npm run package:win        # PyInstaller bundle + win32-x64 .vsix
npm run package:universal  # removes server/bin, packs the Python fallback
```

PyInstaller can't cross-compile, so the other platforms are built by CI
([.github/workflows/build.yml](.github/workflows/build.yml)): every push
builds win32-x64, linux-x64, darwin-arm64, and universal as downloadable
artifacts, after the test job passes. Pushing a tag like `v0.3.0` publishes
all four to the Marketplace — that needs a `VSCE_PAT` repository secret (an
Azure DevOps personal access token with the *Marketplace → Manage* scope).

**No darwin-x64.** That leg was dropped once GitHub stopped allocating
`macos-13`, its last Intel runner — the job queued for 24 hours without
starting while the rest of the matrix finished in under 90 seconds. An
Intel binary needs an Intel runner, so Intel Macs use the universal build
(Python 3 + pygls) until there's a cross-compilation story worth trusting.
