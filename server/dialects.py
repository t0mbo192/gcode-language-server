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

ALL_RULES = frozenset({
    R_FEED_MISSING, R_SPINDLE_OFF, R_G43_NO_H, R_NO_TLO_AFTER_M6,
    R_COMP_AT_END, R_ARC_NO_CENTER, R_UNKNOWN_CODE,
})

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

DIALECTS = {d.name: d for d in (FANUC, LINUXCNC, SIEMENS, MARLIN, OKUMA)}
DEFAULT_DIALECT = "fanuc"

# ---------------------------------------------------------------------------
# Dialect detection
# ---------------------------------------------------------------------------
# The file extension usually tells you the dialect, because each CAM post
# writes a signature extension. Ambiguous ones (.nc could be anything) fall
# through to the default.

EXTENSION_DIALECTS = {
    ".mpf": "siemens",   # Siemens main program
    ".spf": "siemens",   # Siemens subprogram
    ".ngc": "linuxcnc",
    ".gcode": "marlin",  # 3D-printer flavor
    ".gc": "marlin",
    ".min": "okuma",
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
