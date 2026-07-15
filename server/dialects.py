"""
dialects.py — every piece of dialect knowledge in the project, as plain data.

There is deliberately no logic here beyond `resolve_dialect()`. Rules and
docs are tables so that tuning them (which you WILL do — the shipped rules
are Fanuc-flavored starting points) never means touching the engine in
gcode_parser.py or the LSP glue in server.py.

Three things live here:

  1. RULE IDS + which rules each dialect enables.
     Example: Marlin (3D printers) has no spindle and no tool-length
     compensation in the CNC sense, so those rules are simply absent
     from its rule set.

  2. KNOWN CODES + hover documentation, per dialect.
     The same table serves three features: hover tooltips, completion
     items, and the "unknown-code" lint (anything not in the table).

  3. DIALECT DETECTION: extension map, magic comment, and the priority
     order between them (explicit setting > magic comment > extension >
     default fanuc).
"""

from dataclasses import dataclass
import os
import re

# ---------------------------------------------------------------------------
# Rule identifiers
# ---------------------------------------------------------------------------
# These strings show up as the diagnostic "code" in VS Code's Problems panel,
# so keep them short and grep-able.

R_FEED_MISSING = "feed-missing"            # G1/G2/G3 but no F active
R_SPINDLE_OFF = "spindle-off"              # cutting move while spindle stopped
R_G43_NO_H = "g43-missing-h"               # G43/G44 without an H word
R_NO_TLO_AFTER_M6 = "no-g43-after-toolchange"  # Z move after M6, no G43 yet
R_COMP_AT_END = "comp-active-at-end"       # G41/G42 still active at M2/M30
R_ARC_NO_CENTER = "arc-missing-center"     # G2/G3 without I/J/K or R
R_UNKNOWN_CODE = "unknown-code"            # G/M code not in the dialect table
R_NO_COOLANT = "no-coolant-for-tool"       # a tool's first cut with coolant off

ALL_RULES = frozenset({
    R_FEED_MISSING, R_SPINDLE_OFF, R_G43_NO_H, R_NO_TLO_AFTER_M6,
    R_COMP_AT_END, R_ARC_NO_CENTER, R_UNKNOWN_CODE, R_NO_COOLANT,
})

# ---------------------------------------------------------------------------
# Coolant M-codes (drives the no-coolant-for-tool rule)
# ---------------------------------------------------------------------------
# The engine only needs to know which M-codes switch coolant on and off —
# everything else about the rule lives in gcode_parser.py. These two sets
# are the DEFAULTS; each Dialect can override them (see the coolant_on /
# coolant_off fields), because the same M number means different things on
# different controls: M51 is through-spindle coolant on a Mazak but a
# spindle-override switch on LinuxCNC. Codes listed in a coolant set but
# absent from that dialect's known_m table still count as coolant for the
# state machine; they'll just also raise the (info-level) unknown-code note
# until you document them in the table.

COOLANT_ON_CODES = frozenset({"M7", "M8"})    # mist, flood
COOLANT_OFF_CODES = frozenset({"M9"})

# Which M-codes start the spindle — dialect data for the same reason coolant
# is: Heidenhain's M13/M14 are combo codes (spindle CW/CCW AND coolant on in
# one number), so they belong in BOTH this set and coolant_on. The engine
# checks the two sets independently to make that possible.
SPINDLE_ON_CODES = frozenset({"M3", "M4"})

# Fanuc-style canned cycles cut material the moment the cycle word executes
# (the first hole is drilled on that very line). This is per-dialect data
# because Heidenhain works the other way around: G200+ only DEFINES a cycle,
# and nothing cuts until the call (G79) — so its set is just {"G79"}.
CANNED_CYCLES = frozenset({"G73", "G74", "G76", "G81", "G82", "G83", "G84",
                           "G85", "G86", "G87", "G88", "G89"})

# ---------------------------------------------------------------------------
# Hover docs shared by most milling controls (Fanuc-ish baseline)
# ---------------------------------------------------------------------------
# Keys are NORMALIZED codes: leading zeros stripped ("G01" -> "G1"),
# decimals kept ("G38.2"). gcode_parser.normalize_code() does that.

_BASE_G = {
    "G0": "**G0 — Rapid move.** Full-speed positioning. Never cut with it.",
    "G1": "**G1 — Linear feed move** at the active feedrate (`F`).",
    "G2": "**G2 — Clockwise arc.** Needs a center (`I`/`J`/`K`) or a radius (`R`).",
    "G3": "**G3 — Counter-clockwise arc.** Needs a center (`I`/`J`/`K`) or a radius (`R`).",
    "G4": "**G4 — Dwell.** Pause for the time given by `P` (ms on many controls) or `X` (seconds).",
    "G17": "**G17 — XY plane select.** Arcs and comp act in XY. The usual mill default.",
    "G18": "**G18 — XZ plane select.** Common on lathes.",
    "G19": "**G19 — YZ plane select.**",
    "G20": "**G20 — Inch units.**",
    "G21": "**G21 — Millimeter units.**",
    "G28": "**G28 — Return to machine home**, optionally through an intermediate point.",
    "G40": "**G40 — Cancel cutter compensation** (G41/G42).",
    "G41": "**G41 — Cutter compensation LEFT** of the programmed path. Offset from the `D` register.",
    "G42": "**G42 — Cutter compensation RIGHT** of the programmed path. Offset from the `D` register.",
    "G43": "**G43 — Tool length compensation (+).** Applies the length offset in the `H` register. Usually the first Z move after a tool change carries it.",
    "G44": "**G44 — Tool length compensation (−).** Rarely used; negative-direction variant of G43.",
    "G49": "**G49 — Cancel tool length compensation.**",
    "G53": "**G53 — Move in machine coordinates** (non-modal, ignores work offsets).",
    "G54": "**G54 — Work offset 1.** First of the standard fixture offsets.",
    "G55": "**G55 — Work offset 2.**",
    "G56": "**G56 — Work offset 3.**",
    "G57": "**G57 — Work offset 4.**",
    "G58": "**G58 — Work offset 5.**",
    "G59": "**G59 — Work offset 6.**",
    "G80": "**G80 — Cancel canned cycle.**",
    "G81": "**G81 — Drill cycle**: feed to depth, rapid out.",
    "G82": "**G82 — Drill cycle with dwell** at the bottom (spot facing, counterbores).",
    "G83": "**G83 — Peck drilling cycle.** Full retract between pecks (`Q` = peck depth).",
    "G84": "**G84 — Tapping cycle.** Feed and speed must match the thread pitch.",
    "G90": "**G90 — Absolute positioning.** Words are coordinates.",
    "G91": "**G91 — Incremental positioning.** Words are distances from the current point.",
    "G92": "**G92 — Set position register** (shift the coordinate system, no motion).",
    "G94": "**G94 — Feed per minute** mode.",
    "G95": "**G95 — Feed per revolution** mode (lathe-style feeds).",
    "G98": "**G98 — Canned cycle: return to initial level** between holes.",
    "G99": "**G99 — Canned cycle: return to R level** between holes.",
}

_BASE_M = {
    "M0": "**M0 — Program stop.** Unconditional; operator must press cycle start.",
    "M1": "**M1 — Optional stop.** Stops only if the op-stop switch is on.",
    "M2": "**M2 — Program end.**",
    "M3": "**M3 — Spindle on, clockwise** at the active `S` speed.",
    "M4": "**M4 — Spindle on, counter-clockwise.**",
    "M5": "**M5 — Spindle stop.**",
    "M6": "**M6 — Tool change** to the staged `T` number.",
    "M7": "**M7 — Mist coolant on.**",
    "M8": "**M8 — Flood coolant on.**",
    "M9": "**M9 — Coolant off.**",
    "M30": "**M30 — Program end and rewind.** The usual last line.",
    "M98": "**M98 — Call subprogram** (`P` = program number, `L` = repeat count).",
    "M99": "**M99 — Return from subprogram** (or loop to top if used in the main).",
}

# ---------------------------------------------------------------------------
# Hover docs for parameter letters (shown when you hover X, F, H, ...)
# ---------------------------------------------------------------------------

WORD_DOCS = {
    "X": "**X — X-axis coordinate** (or dwell time inside G4 on some controls).",
    "Y": "**Y — Y-axis coordinate.**",
    "Z": "**Z — Z-axis coordinate.**",
    "A": "**A — Rotary axis around X** (degrees).",
    "B": "**B — Rotary axis around Y** (degrees).",
    "C": "**C — Rotary axis around Z** (degrees).",
    "U": "**U — Secondary/incremental axis parallel to X** (control-dependent).",
    "V": "**V — Secondary axis parallel to Y** (control-dependent).",
    "W": "**W — Secondary axis parallel to Z** (control-dependent).",
    "E": "**E — Extruder position** (3D-printer dialects).",
    "I": "**I — Arc center offset along X** (from the start point).",
    "J": "**J — Arc center offset along Y** (from the start point).",
    "K": "**K — Arc center offset along Z** (from the start point).",
    "R": "**R — Arc radius**, or retract plane inside canned cycles.",
    "F": "**F — Feedrate.** Units/min under G94, units/rev under G95. Modal: stays active until changed.",
    "S": "**S — Spindle speed** (rpm, or surface speed under constant-surface-speed modes).",
    "T": "**T — Tool select.** Stages a tool; `M6` performs the change.",
    "H": "**H — Tool length offset register**, used by `G43`/`G44`.",
    "D": "**D — Tool diameter/radius offset register**, used by `G41`/`G42`.",
    "P": "**P — Parameter word**: dwell time, subprogram number, or cycle parameter, depending on context.",
    "Q": "**Q — Cycle parameter**, e.g. peck depth in `G83`.",
    "L": "**L — Repeat count** (subprograms, canned cycles).",
    "N": "**N — Line (sequence) number.** Ignored by the machine; used for restarts and searches.",
    "O": "**O — Program number** (Fanuc style).",
}

# ---------------------------------------------------------------------------
# The dialects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dialect:
    name: str            # internal id, matches settings/magic comments
    title: str           # human-readable, used in messages
    rules: frozenset     # subset of ALL_RULES this dialect enforces
    known_g: dict        # normalized G-code -> hover markdown
    known_m: dict        # normalized M-code -> hover markdown
    # Which M-codes switch coolant, for the no-coolant-for-tool rule.
    # Defaults cover the classic mist/flood/off trio; override per dialect
    # when the control has more (through-spindle, air blast, ...).
    coolant_on: frozenset = COOLANT_ON_CODES
    coolant_off: frozenset = COOLANT_OFF_CODES
    # Which M-codes start the spindle (see SPINDLE_ON_CODES above — combo
    # codes like Heidenhain M13/M14 go in this AND coolant_on).
    spindle_on: frozenset = SPINDLE_ON_CODES
    # True on controls where the T word ITSELF performs the tool change
    # (Heidenhain TOOL CALL / ISO "T5 G17 S4000") instead of staging a tool
    # for a later M6. Arms the per-tool coolant check at the T word.
    tool_change_on_t: bool = False
    # When tool_change_on_t: G words that make a T on the same line NOT a
    # change — tool definitions and preselects (Heidenhain G99 / G51).
    tool_def_codes: frozenset = frozenset()
    # Which G words start cutting the moment they execute (see the
    # CANNED_CYCLES comment above).
    cycle_codes: frozenset = CANNED_CYCLES


FANUC = Dialect(
    name="fanuc",
    title="Fanuc",
    rules=ALL_RULES,
    known_g=dict(_BASE_G),
    known_m=dict(_BASE_M),
)

# LinuxCNC speaks the RS-274/NGC dialect: Fanuc-like, plus some extras.
LINUXCNC = Dialect(
    name="linuxcnc",
    title="LinuxCNC",
    rules=ALL_RULES,
    known_g={
        **_BASE_G,
        "G33": "**G33 — Spindle-synchronized motion** (single-point threading).",
        "G38.2": "**G38.2 — Straight probe** toward the workpiece, error if no contact.",
        "G64": "**G64 — Path blending mode** (`P` = tolerance). Opposite of exact-stop G61.",
        "G76": "**G76 — Threading cycle** (lathe).",
    },
    known_m={
        **_BASE_M,
        "M62": "**M62 — Digital output ON**, synchronized with motion (LinuxCNC).",
        "M63": "**M63 — Digital output OFF**, synchronized with motion (LinuxCNC).",
    },
)

# Siemens SINUMERIK: tool length comp comes from the tool edge (D word on the
# tool, not a G43 H call), so the two G43-related rules are dropped. Watch
# out: G70/G71 mean inch/metric INPUT here — on a Fanuc lathe they are
# finishing/roughing cycles. Same code, different planet.
SIEMENS = Dialect(
    name="siemens",
    title="Siemens SINUMERIK",
    rules=ALL_RULES - {R_G43_NO_H, R_NO_TLO_AFTER_M6},
    known_g={
        code: doc for code, doc in _BASE_G.items()
        if code not in ("G43", "G44", "G49", "G80", "G81", "G82", "G83",
                        "G84", "G98", "G99", "G20", "G21")
    } | {
        "G70": "**G70 — Inch input** (Siemens). NOT the Fanuc finishing cycle.",
        "G71": "**G71 — Metric input** (Siemens). NOT the Fanuc roughing cycle.",
        "G64": "**G64 — Continuous-path (blending) mode** (Siemens).",
    },
    known_m={
        **_BASE_M,
        "M17": "**M17 — End of subprogram** (Siemens).",
    },
)

# Marlin (3D printers): no spindle, no cutter/tool-length comp, so only the
# geometry/feed rules apply. The M-code table is a different world.
# The coolant rule is deliberately absent too: a printer's M106 is a
# part-cooling fan, not coolant, and plenty of printer G-code never
# touches M7/M8 legitimately.
MARLIN = Dialect(
    name="marlin",
    title="Marlin (3D printer)",
    rules=frozenset({R_FEED_MISSING, R_ARC_NO_CENTER, R_UNKNOWN_CODE}),
    known_g={
        "G0": "**G0 — Rapid move.** In Marlin, treated the same as G1.",
        "G1": "**G1 — Linear move** at the active feedrate; `E` moves the extruder.",
        "G2": "**G2 — Clockwise arc.** Needs `I`/`J` or `R`.",
        "G3": "**G3 — Counter-clockwise arc.** Needs `I`/`J` or `R`.",
        "G4": "**G4 — Dwell.** `P` = milliseconds, `S` = seconds.",
        "G28": "**G28 — Auto-home** one or all axes.",
        "G29": "**G29 — Automatic bed leveling probe.**",
        "G90": "**G90 — Absolute positioning.**",
        "G91": "**G91 — Relative positioning.**",
        "G92": "**G92 — Set position**, most often `G92 E0` to zero the extruder.",
    },
    known_m={
        "M0": "**M0 — Unconditional stop**, wait for user.",
        "M82": "**M82 — Extruder absolute mode.**",
        "M83": "**M83 — Extruder relative mode.**",
        "M84": "**M84 — Disable steppers.**",
        "M104": "**M104 — Set hotend temperature** and continue (no wait).",
        "M106": "**M106 — Part-cooling fan on** (`S0–255`).",
        "M107": "**M107 — Part-cooling fan off.**",
        "M109": "**M109 — Set hotend temperature and WAIT** until reached.",
        "M114": "**M114 — Report current position.**",
        "M140": "**M140 — Set bed temperature** and continue (no wait).",
        "M190": "**M190 — Set bed temperature and WAIT** until reached.",
        "M600": "**M600 — Filament change.**",
        # Fun dialect trap: on a CNC this ends the program; on Marlin it
        # deletes a file from the SD card. Reason enough for dialect tables.
        "M30": "**M30 — Delete file from SD card** (Marlin!). On CNC controls this is program end.",
    },
)

OKUMA = Dialect(
    name="okuma",
    title="Okuma OSP",
    rules=ALL_RULES,
    known_g={
        **_BASE_G,
        "G15": "**G15 — Select work coordinate system** by number (`H` word) (Okuma).",
        "G16": "**G16 — Rotary axis coordinate designation** (Okuma).",
    },
    known_m=dict(_BASE_M),
)

# Mazak (MAZATROL Matrix / Smooth controls running EIA/ISO programs): the
# G-code side is thoroughly Fanuc-like; what's different is the M-code
# family — especially coolant, which goes well past mist/flood. CAUTION:
# Mazak M-code assignments genuinely vary by machine model and installed
# options (e.g. some machines put air-through-spindle on M132) — treat this
# table as the common Integrex/machining-center baseline and verify every
# code against YOUR machine's parameter list before trusting it.
MAZAK = Dialect(
    name="mazak",
    title="Mazak",
    rules=ALL_RULES,
    known_g=dict(_BASE_G),
    known_m={
        **_BASE_M,
        # Mazak's M7 is typically air/oil-mist blast rather than true mist
        # coolant — same number as Fanuc, different plumbing.
        "M7": "**M7 — Air blast / oil mist on** (Mazak). Air, not flood.",
        "M19": "**M19 — Spindle orientation** (stop at a fixed angle).",
        "M48": "**M48 — Feedrate override cancel OFF** — the override knob works.",
        "M49": "**M49 — Feedrate override cancel ON** — the knob is ignored.",
        "M50": "**M50 — Air blast on** (model-dependent: plain air on some machines, flood-air on others).",
        "M51": "**M51 — Through-spindle coolant on** (milling spindle).",
        "M163": "**M163 — Through-spindle coolant off** (Integrex family).",
    },
    # Air blast and through-spindle coolant both count as "coolant arrived
    # for this tool" — a deliberate air-blast strategy is not a forgotten M8.
    coolant_on=frozenset({"M7", "M8", "M50", "M51"}),
    coolant_off=frozenset({"M9", "M163"}),
)

# Haas (NGC / classic mill controls): the closest living relative of Fanuc —
# programs interchange almost line-for-line — but Haas added its own G-codes
# (bolt-hole patterns, G154 offsets, G187 smoothness) and a large coolant
# family. The coolant part matters for the no-coolant-for-tool rule: plenty
# of tools in a Haas carousel are plumbed for through-spindle coolant ONLY
# (gun drills, coolant-through end mills) and legitimately never see an M8,
# so M88 must count as coolant or every one of them is a false positive.
# Same for the air options (M73/M83): air to the cut is a chosen strategy,
# not a forgotten M8 — the Mazak table treats M50/M51 the same way.
HAAS = Dialect(
    name="haas",
    title="Haas",
    rules=ALL_RULES,
    known_g={
        **_BASE_G,
        "G12": "**G12 — Circular pocket milling, CW** (Haas). `I`/`K` radii, `D` comp.",
        "G13": "**G13 — Circular pocket milling, CCW** (Haas).",
        # Dialect trap, third meaning for the same numbers: Haas G70/G71/G72
        # are BOLT-HOLE PATTERNS — not Siemens inch/metric input, not Fanuc
        # lathe finish/rough cycles.
        "G70": "**G70 — Bolt hole circle** (Haas). `I` radius, `J` start angle, `L` holes. NOT inch input (Siemens) or a lathe finishing cycle (Fanuc).",
        "G71": "**G71 — Bolt hole arc** (Haas). NOT metric input (Siemens) or a lathe roughing cycle (Fanuc).",
        "G72": "**G72 — Bolt holes along an angle** (Haas).",
        "G103": "**G103 — Limit block lookahead** (Haas). `P0`–`P15` blocks; `P1` effectively disables lookahead.",
        "G154": "**G154 — Extended work offsets** `P1`–`P99` (Haas). The G54.1 equivalent.",
        "G187": "**G187 — Smoothness / accuracy control** (Haas). `P1` rough … `P3` finish, `E` tolerance.",
        "G234": "**G234 — Tool Center Point Control (TCPC)** for 5-axis (Haas option).",
        "G254": "**G254 — Dynamic Work Offset (DWO) on** for 3+2 work (Haas option).",
        "G255": "**G255 — Cancel Dynamic Work Offset (DWO)** (Haas).",
    },
    known_m={
        **_BASE_M,
        # Haas M7 is the shower/washdown option, not Fanuc's mist.
        "M7": "**M7 — Shower coolant on** (Haas option). Low-pressure washdown, not mist.",
        "M19": "**M19 — Orient spindle** (stop at a fixed angle; `P`/`R` = degrees).",
        "M31": "**M31 — Chip conveyor forward** (Haas).",
        "M33": "**M33 — Chip conveyor stop** (Haas).",
        "M34": "**M34 — P-Cool nozzle down** one position (Haas programmable-coolant option).",
        "M35": "**M35 — P-Cool nozzle up** one position (Haas).",
        "M73": "**M73 — Through-tool air blast on** (Haas TAB option). Air out the spindle, not liquid.",
        "M74": "**M74 — Through-tool air blast off** (Haas).",
        "M83": "**M83 — Auto air jet on** (Haas AAG option).",
        "M84": "**M84 — Auto air jet off** (Haas).",
        "M88": "**M88 — Through-spindle coolant (TSC) on** (Haas). High-pressure coolant out the tool tip — a TSC-plumbed tool may run this INSTEAD of M8.",
        "M89": "**M89 — Through-spindle coolant (TSC) off** (Haas). M9 does not turn TSC off.",
        "M97": "**M97 — Local subprogram call** (Haas). `P` = the N line to jump to in THIS program, `L` = repeats.",
    },
    # Everything that delivers coolant OR air to the cut satisfies the rule:
    # flood (M8), shower (M7), through-spindle coolant (M88), through-tool
    # air (M73), air jet (M83). A TSC-only tool that programs M88 alone is
    # doing it right. Note M9 only kills flood/shower on a Haas — TSC has
    # its own off code (M89), which is why both are in coolant_off.
    coolant_on=frozenset({"M7", "M8", "M73", "M83", "M88"}),
    coolant_off=frozenset({"M9", "M74", "M84", "M89"}),
)

# Heidenhain TNC controls speak TWO languages, so they get TWO dialects:
#
#   * "heidenhain" — DIN/ISO mode (.i files): G-code, but with Heidenhain
#     semantics. This one the engine can genuinely lint.
#   * "klartext"   — conversational format (.h files): `L X+30 RL F250`,
#     `TOOL CALL 5 Z S2500`. NOT G-code at all — see KLARTEXT below.
#
# The ISO mode differs from Fanuc in ways that are traps, not details, which
# is why known_g is built from scratch instead of on _BASE_G:
#   * The T word IS the tool change (TOOL CALL). There is no M6 in a normal
#     program — hence tool_change_on_t=True, so the per-tool coolant check
#     re-arms at each T. G99 defines a tool and G51 preselects one; a T on
#     those lines is NOT a change (tool_def_codes).
#   * Length comp comes from the tool table automatically at the tool call,
#     so both G43 rules are dropped — and G43/G44 here mean PARAXIAL comp.
#   * Cycles are define-then-call: G200+ (or old-style G83/G84) only stores
#     the cycle; G79 executes it. cycle_codes={"G79"} keeps the coolant
#     rule from treating a definition as a cut.
#   * M13/M14 are combo codes: spindle CW/CCW + coolant on in one number.
#     They sit in BOTH spindle_on and coolant_on — a program using M13 and
#     never M3/M8 is completely normal on these controls.
# As always: tables are a TNC 530/640-flavored starting point — verify
# against your machine's manual.
HEIDENHAIN = Dialect(
    name="heidenhain",
    title="Heidenhain (DIN/ISO)",
    rules=ALL_RULES - {R_G43_NO_H, R_NO_TLO_AFTER_M6},
    known_g={
        "G0": "**G0 — Rapid move.**",
        "G1": "**G1 — Linear feed move** at the active feedrate (`F`).",
        "G2": "**G2 — Clockwise arc.** Center via `I`/`J`/`K` or radius via `R`.",
        "G3": "**G3 — Counter-clockwise arc.**",
        "G4": "**G4 — Dwell.** `F` = seconds (Heidenhain ISO).",
        "G17": "**G17 — XY plane / tool axis Z.**",
        "G18": "**G18 — XZ plane / tool axis Y.**",
        "G19": "**G19 — YZ plane / tool axis X.**",
        # Dialect trap: this is NOT the Fanuc reference-return.
        "G28": "**G28 — Mirror image** (Heidenhain). NOT the Fanuc return-to-home.",
        "G29": "**G29 — Transfer the last position as the pole (CC)** (Heidenhain).",
        "G30": "**G30 — Blank form (BLK FORM) minimum point**, with the plane, e.g. `G30 G17 X+0 Y+0 Z-20`.",
        "G31": "**G31 — Blank form (BLK FORM) maximum point**, e.g. `G31 G90 X+100 Y+100 Z+0`.",
        "G40": "**G40 — Cancel radius compensation** (the Klartext `R0`).",
        "G41": "**G41 — Radius compensation LEFT** of the contour (Klartext `RL`).",
        "G42": "**G42 — Radius compensation RIGHT** of the contour (Klartext `RR`).",
        # Trap: not Fanuc tool-length comp — length comes from the tool call.
        "G43": "**G43 — Paraxial compensation: lengthen** (Heidenhain). NOT Fanuc tool-length comp — length offset is applied automatically by the tool call.",
        "G44": "**G44 — Paraxial compensation: shorten** (Heidenhain).",
        "G51": "**G51 — Tool preselect** (`T` = next tool into the changer). The `T` here is NOT a tool change.",
        "G53": "**G53 — Datum shift from the datum table** (Heidenhain).",
        "G54": "**G54 — Datum shift programmed in-line** (Heidenhain). NOT a Fanuc-style stored work offset.",
        "G70": "**G70 — Inch units** (Heidenhain, in the program header). Same number, third meaning: Siemens inch input, Fanuc lathe finishing, Haas bolt circle.",
        "G71": "**G71 — Millimeter units** (Heidenhain, e.g. `%name G71 *`).",
        "G79": "**G79 — Cycle call (CYCL CALL).** Executes the last defined cycle — THIS is the line that cuts.",
        # Trap: Cycle 19, not the Fanuc canned-cycle cancel.
        "G80": "**G80 — Working plane cycle (Cycle 19)** (Heidenhain). NOT the Fanuc canned-cycle cancel.",
        "G83": "**G83 — Pecking cycle DEFINITION** (old style). Stored only; `G79` executes it.",
        "G84": "**G84 — Tapping cycle DEFINITION** (old style). Stored only; `G79` executes it.",
        "G90": "**G90 — Absolute positioning.**",
        "G91": "**G91 — Incremental positioning.**",
        # Trap pair: nothing to do with Fanuc canned-cycle return planes.
        "G98": "**G98 — Set a label (LBL SET)** for jumps/repeats (Heidenhain). NOT the Fanuc return-to-initial-level.",
        "G99": "**G99 — Tool DEFINITION** (`T` number, `L` length, `R` radius) (Heidenhain). The `T` here is NOT a tool change.",
        "G200": "**G200 — Drilling cycle** definition (`Q` parameters). Call with `G79`.",
        "G201": "**G201 — Reaming cycle** definition.",
        "G202": "**G202 — Boring cycle** definition.",
        "G203": "**G203 — Universal drilling cycle** definition.",
        "G204": "**G204 — Back boring cycle** definition.",
        "G205": "**G205 — Universal pecking cycle** definition.",
        "G206": "**G206 — Tapping with floating chuck** cycle definition.",
        "G207": "**G207 — Rigid tapping** cycle definition.",
        "G208": "**G208 — Bore milling** cycle definition.",
        "G209": "**G209 — Tapping with chip breaking** cycle definition.",
        "G251": "**G251 — Rectangular pocket** cycle definition.",
        "G252": "**G252 — Circular pocket** cycle definition.",
        "G253": "**G253 — Slot milling** cycle definition.",
        "G254": "**G254 — Circular slot** cycle definition. (On Haas this number is DWO — dialect tables exist for a reason.)",
    },
    known_m={
        "M0": "**M0 — Program stop.**",
        "M1": "**M1 — Optional stop.**",
        "M2": "**M2 — Program end.**",
        "M3": "**M3 — Spindle on, clockwise.**",
        "M4": "**M4 — Spindle on, counter-clockwise.**",
        "M5": "**M5 — Spindle stop.**",
        "M6": "**M6 — Tool change.** Rare in Heidenhain programs — the tool call (`T`) normally performs the change itself.",
        "M8": "**M8 — Coolant on.**",
        "M9": "**M9 — Coolant off.**",
        "M13": "**M13 — Spindle CW + coolant on** in one code (= M3 + M8). Common on TNC programs.",
        "M14": "**M14 — Spindle CCW + coolant on** in one code (= M4 + M8).",
        "M30": "**M30 — Program end**, same as M2 on TNC controls.",
        "M89": "**M89 — MODAL cycle call**: the defined cycle runs at every following positioning block.",
        "M91": "**M91 — Coordinates in this block are machine-datum based** (Heidenhain).",
        "M92": "**M92 — Coordinates refer to the additional machine datum** (Heidenhain).",
        "M94": "**M94 — Reduce rotary axis display** to below 360°.",
        "M97": "**M97 — Machine small contour steps** (Heidenhain). NOT the Haas local-subprogram call.",
        "M98": "**M98 — Completely machine open contour corners** (Heidenhain). NOT the Fanuc subprogram call.",
        "M99": "**M99 — Blockwise cycle call** (Heidenhain). NOT the Fanuc subprogram return.",
        "M101": "**M101 — Automatic replacement with a twin tool** when tool life expires (machine-dependent).",
        "M102": "**M102 — Cancel M101.**",
        "M126": "**M126 — Rotary axes: shortest-path traverse.**",
        "M127": "**M127 — Cancel M126.**",
        "M128": "**M128 — TCPM on** (keep tool tip position when rotary axes move).",
        "M129": "**M129 — TCPM off.**",
        "M140": "**M140 — Retract along the tool axis** (`MB` = distance / `MB MAX`).",
    },
    # M13/M14 are the reason spindle_on is dialect data at all — one code,
    # both effects. No M7: TNC controls have no separate mist number.
    coolant_on=frozenset({"M8", "M13", "M14"}),
    coolant_off=frozenset({"M9"}),
    spindle_on=frozenset({"M3", "M4", "M13", "M14"}),
    tool_change_on_t=True,
    tool_def_codes=frozenset({"G99", "G51"}),
    cycle_codes=frozenset({"G79"}),
)

# Heidenhain Klartext (.h files) — the control's native conversational
# format: `L X+30 RL F250`, `TOOL CALL 5 Z S2500`, `CYCL DEF 200`. That is
# a different GRAMMAR, not a different code table, and this engine's
# letter+number tokenizer would read garbage into it ("CALL 5" tokenizes as
# L5). So this dialect is deliberately a mute: every rule off, every table
# empty — a Klartext file gets NO squiggles instead of WRONG squiggles.
# Linting Klartext for real means writing a second parser; until then,
# honesty beats noise.
KLARTEXT = Dialect(
    name="klartext",
    title="Heidenhain Klartext",
    rules=frozenset(),
    known_g={},
    known_m={},
)

DIALECTS = {d.name: d for d in (FANUC, LINUXCNC, SIEMENS, MARLIN, OKUMA,
                                MAZAK, HAAS, HEIDENHAIN, KLARTEXT)}
DEFAULT_DIALECT = "fanuc"

# ---------------------------------------------------------------------------
# Dialect detection
# ---------------------------------------------------------------------------
# The file extension usually tells you the dialect, because each CAM post
# writes a signature extension. Ambiguous ones (.nc could be anything) fall
# through to the default. Haas is deliberately absent: Haas posts write
# plain .nc, so it can only be chosen by the setting or a magic comment.

EXTENSION_DIALECTS = {
    ".mpf": "siemens",   # Siemens main program
    ".spf": "siemens",   # Siemens subprogram
    ".ngc": "linuxcnc",
    ".gcode": "marlin",  # 3D-printer flavor
    ".gc": "marlin",
    ".min": "okuma",
    ".eia": "mazak",     # Mazak EIA/ISO program
    ".i": "heidenhain",  # Heidenhain DIN/ISO program
    # Heidenhain Klartext. .h is NOT claimed in package.json (it would
    # hijack every C header in VS Code) — users opt in with a
    # files.associations setting; this mapping then does the right thing.
    ".h": "klartext",
    # .hnc files are usually Heidenhain Klartext too. If yours are ISO
    # G-code, a magic comment or the setting overrides this.
    ".hnc": "klartext",
}

# Escape hatch for ambiguous extensions: a magic comment near the top of the
# file, e.g.  (DIALECT: siemens)  or  ;DIALECT=marlin
_MAGIC_RE = re.compile(r"(?i)\bDIALECT\s*[:=]\s*([A-Za-z0-9_]+)")
_MAGIC_SCAN_LINES = 5


def resolve_dialect(path=None, text=None, override=None):
    """Pick the dialect for one file.

    Priority (most explicit wins):
      1. `override` — the user's gcode.dialect setting, unless "auto"
      2. magic comment in the first few lines of the file
      3. file extension via EXTENSION_DIALECTS
      4. DEFAULT_DIALECT (fanuc — the safest guess in a machine shop)
    """
    if override and override != "auto" and override in DIALECTS:
        return override

    if text:
        for line in text.splitlines()[:_MAGIC_SCAN_LINES]:
            m = _MAGIC_RE.search(line)
            if m and m.group(1).lower() in DIALECTS:
                return m.group(1).lower()

    if path:
        # Works for plain paths and file:// URIs alike — we only need the
        # extension, and splitext doesn't care about the rest.
        ext = os.path.splitext(str(path))[1].lower()
        if ext in EXTENSION_DIALECTS:
            return EXTENSION_DIALECTS[ext]

    return DEFAULT_DIALECT
