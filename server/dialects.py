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
    # The full canned-cycle family. These must stay in step with
    # CANNED_CYCLES above: a code the engine treats as a cycle but does not
    # document gets flagged "not a known G-code" on a perfectly normal
    # program — G73 peck drilling is about as common as machining gets.
    # tests/test_dialects.py asserts the two lists agree.
    "G73": "**G73 — High-speed peck drilling.** Retracts only by a small break-chip amount (`Q` = peck) instead of clearing the hole, so it's faster but relies on chips breaking.",
    "G74": "**G74 — Left-hand tapping cycle.** Spindle runs CCW; the right-hand counterpart is `G84`.",
    "G76": "**G76 — Fine boring cycle.** Orients the spindle at depth and shifts away by `Q` so the insert does not drag a witness line up the bore on retract.",
    "G81": "**G81 — Drill cycle**: feed to depth, rapid out.",
    "G82": "**G82 — Drill cycle with dwell** at the bottom (spot facing, counterbores).",
    "G83": "**G83 — Peck drilling cycle.** Full retract between pecks (`Q` = peck depth), clearing chips completely — the deep-hole choice.",
    "G84": "**G84 — Tapping cycle.** Feed and speed must match the thread pitch.",
    "G85": "**G85 — Boring cycle**: feed in, FEED back out. Leaves the best finish; also used for reaming.",
    "G86": "**G86 — Boring cycle**: feed in, spindle stops, rapid out. Faster than `G85` but can mark the bore.",
    "G87": "**G87 — Back boring cycle.** The tool enters the hole before it starts cutting, then bores on the way up.",
    "G88": "**G88 — Boring cycle with dwell**, then a manual retract.",
    "G89": "**G89 — Boring cycle with dwell** at depth, feeding back out.",
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
    # M19 is in the shared table because every CNC control below means the
    # same thing by it: stop the spindle at a known angle (for boring-bar
    # retract, back-boring, or an orientation-keyed tool change). Only the
    # 3D-printer dialect has no such concept, and it doesn't use this table.
    "M19": "**M19 — Spindle orientation.** Stops the spindle at a fixed angle. Some controls take the angle as `P`/`R`; others use a parameter.",
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
    # Codes that are real (so hover documents them and the unknown-code lint
    # stays quiet) but that should NOT appear in the completion dropdown.
    # Built for LinuxCNC's user-defined M100–M199 block; see the comment
    # over _LINUXCNC_USER_M_CODES.
    completion_hidden: frozenset = frozenset()
    # Letters that accept Siemens EXTENDED ADDRESSING, `<letter><index>=
    # <value>`, where the number before the '=' selects a spindle rather
    # than being the code itself: `M2=3` is "spindle 2, code M3" and
    # `S1=2000` is "spindle 1 at 2000 rpm". Empty for every control that
    # doesn't have the syntax, which leaves their tokenizing untouched.
    extended_address: frozenset = frozenset()


# Fanuc is also the DEFAULT dialect, which shapes what belongs in its M
# table. Two different kinds of M-code live on a Fanuc-controlled machine:
#
#   * M0–M30ish: defined by Fanuc itself. Identical on every control.
#   * Above that: "M-code macros" the MACHINE BUILDER assigns. Fanuc ships
#     the control, Doosan/Mori/Hyundai-Wia/Fadal decide that M60 changes a
#     pallet. This is why the same number can be a pallet change on one
#     machine and a door opener on the next.
#
# The builder-assigned codes below are the assignments common enough to be
# worth a hover, each labelled as such. They are documented ONLY — none of
# them are wired into coolant_on/spindle_on, because being the default
# dialect means these tables also get applied to files whose real control we
# never identified (Haas posts write plain .nc, for instance), and quietly
# crediting a code that means something else there would suppress a real
# warning. Documenting a code is cheap; letting it satisfy a safety rule is
# not.
FANUC = Dialect(
    name="fanuc",
    title="Fanuc",
    rules=ALL_RULES,
    known_g=dict(_BASE_G),
    known_m={
        **_BASE_M,
        "M10": "**M10 — Rotary/4th-axis clamp** (builder-assigned). Clamp before cutting with the rotary parked.",
        "M11": "**M11 — Rotary/4th-axis unclamp.** Program it before an indexing move, `M10` after.",
        # Deliberately documented but NOT credited as spindle-or-coolant —
        # see the block comment above. The hover says how to opt in.
        "M13": "**M13 — Spindle CW + coolant on** in one code on many Fanuc-based machines (Doosan, Mori Seiki, Hardinge). **Not credited here as spindle or coolant**: on a Haas this number releases the 5th-axis brake, and this dialect is also the fallback for files of unknown origin. If your machine has the combo, add `M13`/`M14` to this dialect's `spindle_on` and `coolant_on` sets.",
        "M14": "**M14 — Spindle CCW + coolant on** (same caveat as `M13`).",
        "M29": "**M29 — Rigid tapping mode.** `M29 S500` on the line before `G84` locks spindle rotation to Z feed so the pitch is held by the control, not by a floating holder. Without it, `G84` is floating-tap tapping.",
        "M41": "**M41 — Low gear range** (builder-assigned, two-speed spindles).",
        "M42": "**M42 — High gear range** (builder-assigned).",
        # The double-negative naming is Fanuc's, not a typo here: M49 is the
        # one that TAKES the knobs away from the operator.
        "M48": "**M48 — Override cancel OFF** — the feed and rapid override knobs work normally. The power-on default.",
        "M49": "**M49 — Override cancel ON** — overrides are ignored and the machine runs at exactly the programmed feed. Usual around tapping cycles.",
        "M60": "**M60 — Automatic pallet change** (builder-assigned; horizontals and pallet-pool machines).",
        "M198": "**M198 — Call a subprogram from an external device** (DNC drip-feed, memory card), `P` = program number. `M98` calls from control memory instead.",
    },
)

# A LinuxCNC config can define its own M-codes as executables named M100
# through M199 sitting in the machine's config directory — M101 might fire
# an air blast, M110 might open a door. The numbers are a real, documented
# feature; only their MEANING is per-machine. Generating the whole block
# with one generic doc keeps the unknown-code lint quiet about a legitimate
# feature instead of squiggling a hundred valid numbers, and still gives
# the hover something true to say.
_LINUXCNC_USER_M = {
    f"M{n}": (
        f"**M{n} — User-defined M-code** (LinuxCNC). Runs the executable "
        f"named `M{n}` in this machine's config directory, passing `P` and "
        f"`Q` as arguments. What it does is specific to this machine — "
        f"check the config, not a manual."
    )
    for n in range(100, 200)
}

# ...and they stay OUT of the completion dropdown. Hover and the lint still
# know them, but offering 100 identical "user-defined" items would bury M1,
# M2 and M30 under a wall of numbers this particular machine almost
# certainly does not implement.
_LINUXCNC_USER_M_CODES = frozenset(_LINUXCNC_USER_M)

# LinuxCNC speaks the RS-274/NGC dialect: Fanuc-like, plus some extras.
# Its M-codes are worth more trust than the rest of this file: the
# interpreter implements this list ITSELF, in open source, so unlike the
# builder-assigned numbers on a Fanuc or Mazak there is exactly one correct
# answer for what M64 does.
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
        # --- overrides and adaptive feed --------------------------------
        # LinuxCNC words these the plain way round (M48 ENABLES overrides),
        # where Fanuc names the same behaviour "override cancel off". Same
        # effect, opposite-sounding sentence — worth knowing when you port a
        # program between the two.
        "M48": "**M48 — Enable feed and speed overrides** — the knobs work. The default state.",
        "M49": "**M49 — Disable feed and speed overrides** — the machine runs at exactly the programmed feed and rpm.",
        "M50": "**M50 — Feed override control.** `P1` on, `P0` off (finer-grained than M48/M49).",
        # Collision worth flagging: this number is through-spindle COOLANT
        # on a Mazak. It is the example the coolant-codes comment at the top
        # of this file is built on.
        "M51": "**M51 — Spindle speed override control.** `P1` on, `P0` off. NOT Mazak's through-spindle coolant.",
        "M52": "**M52 — Adaptive feed control.** `P1` on: feed follows an external analog input.",
        "M53": "**M53 — Feed stop control.** `P1` lets the feed-stop switch halt motion; `P0` ignores it.",
        # --- tooling and pallets ----------------------------------------
        "M60": "**M60 — Exchange pallet shuttle and stop** (LinuxCNC's `M30` without ending the program).",
        "M61": "**M61 — Set the current tool number to `Q`** with no tool change. This is how you tell the control what you just put in the spindle by hand.",
        # --- I/O --------------------------------------------------------
        # The synchronized/immediate distinction is the whole point of these
        # four: 62/63 queue with motion so the output flips exactly where
        # the tool is, 64/65 fire the instant the interpreter reads them —
        # which, with lookahead running, can be many moves early.
        "M62": "**M62 — Digital output ON**, synchronized with motion (`P` = output number). Fires at the programmed point in the path.",
        "M63": "**M63 — Digital output OFF**, synchronized with motion (`P`).",
        "M64": "**M64 — Digital output ON immediately** (`P`), without waiting for motion — with lookahead active this can happen several moves early.",
        "M65": "**M65 — Digital output OFF immediately** (`P`).",
        "M66": "**M66 — Wait on an input.** `P` digital / `E` analog input, `L` = wait mode, `Q` = timeout in seconds.",
        "M67": "**M67 — Analog output, synchronized with motion** (`E` = output, `Q` = value).",
        "M68": "**M68 — Analog output, immediate** (`E`, `Q`).",
        # --- modal state stack ------------------------------------------
        "M70": "**M70 — Save modal state** (motion mode, units, offsets, feed…) onto a stack.",
        "M71": "**M71 — Invalidate the saved modal state** — a later `M72` will not restore it.",
        "M72": "**M72 — Restore modal state** saved by `M70`.",
        "M73": "**M73 — Save modal state and auto-restore it** when the current subroutine returns. NOT the Haas through-tool air blast.",
        **_LINUXCNC_USER_M,
    },
    completion_hidden=_LINUXCNC_USER_M_CODES,
)

# Siemens SINUMERIK: tool length comp comes from the tool edge (D word on the
# tool, not a G43 H call), so the two G43-related rules are dropped. Watch
# out: G70/G71 mean inch/metric INPUT here — on a Fanuc lathe they are
# finishing/roughing cycles. Same code, different planet.
#
# EXTENDED ADDRESSING is the other thing that makes this control different
# to tokenize. On multi-spindle machines (turn-mills with a counter-spindle)
# Siemens aims an M-code at one spindle by writing `M2=3` — "spindle 2, code
# M3" — and sets its speed with `S1=2000`. Read as plain letter+number that
# is a bare M2, which on this control is PROGRAM END: the modal state used
# to reset mid-file and every line after it inherited a machine with no feed
# and no spindle, burying the rest of a legal program in false warnings.
# `extended_address` below tells the tokenizer that M and S carry an index
# here, so the value after the '=' is the code. The index itself is dropped:
# ModalState tracks a single spindle, so `M1=3` and `M2=3` both just mean
# "a spindle is turning". Per-spindle state is a modelling change, not a
# tokenizer one, and no rule needs it yet.
SIEMENS = Dialect(
    name="siemens",
    title="Siemens SINUMERIK",
    rules=ALL_RULES - {R_G43_NO_H, R_NO_TLO_AFTER_M6},
    known_g={
        code: doc for code, doc in _BASE_G.items()
        # The whole Fanuc canned-cycle family goes: on a SINUMERIK you drill
        # with a named cycle call (CYCLE81, CYCLE83, POCKET3), not a G word.
        if code not in ("G43", "G44", "G49", "G80", "G98", "G99", "G20",
                        "G21", "G73", "G74", "G76", "G81", "G82", "G83",
                        "G84", "G85", "G86", "G87", "G88", "G89")
    } | {
        "G70": "**G70 — Inch input** (Siemens). NOT the Fanuc finishing cycle.",
        "G71": "**G71 — Metric input** (Siemens). NOT the Fanuc roughing cycle.",
        "G64": "**G64 — Continuous-path (blending) mode** (Siemens).",
    },
    # Siemens splits M-codes cleanly into two groups, and the split is the
    # opposite of Fanuc's: M0–M5, M17, M19, M30, M40–M45 and M70 are
    # PREDEFINED by Siemens and mean the same thing on every 840D. Almost
    # everything else — including the coolant trio M7/M8/M9 that the base
    # table contributes — is assigned by the machine builder. The coolant
    # numbers happen to follow the industry convention on virtually every
    # machine, which is why they stay in coolant_on/coolant_off here.
    known_m={
        # M98/M99 are filtered out rather than inherited: Siemens has no
        # such codes. A subprogram is called by NAME (`DRILL_PATTERN` or
        # `CALL "DRILL_PATTERN"`) and returns with M17 or RET, so an M98 in
        # a Siemens file is a post-processor set to the wrong control — the
        # info-level unknown-code note is exactly the right response.
        code: doc for code, doc in _BASE_M.items()
        if code not in ("M98", "M99")
    } | {
        "M17": "**M17 — End of subprogram** (Siemens): return to the caller. `RET` does the same job without breaking continuous-path mode (`G64`), which is why finish-pass subprograms usually end with `RET`.",
        "M19": "**M19 — Position the spindle** to the angle held in setting data. Newer Siemens programs use `SPOS=45` directly instead.",
        "M40": "**M40 — Automatic gear-stage selection** — the control picks the range for the programmed `S`.",
        "M41": "**M41 — Gear stage 1** (lowest range, highest torque).",
        "M42": "**M42 — Gear stage 2.**",
        "M43": "**M43 — Gear stage 3.**",
        "M44": "**M44 — Gear stage 4.**",
        "M45": "**M45 — Gear stage 5.**",
        "M70": "**M70 — Switch the spindle into axis mode** — it stops behaving as a spindle and becomes a positioning (C) axis.",
    },
    # No G-word canned cycles here, so nothing for the coolant rule to treat
    # as a first cut. Siemens drilling is `CYCLE81(...)` — a named call this
    # letter+number tokenizer cannot see at all, which is a limitation worth
    # stating plainly rather than papering over with Fanuc codes that would
    # never appear in a real Siemens program.
    cycle_codes=frozenset(),
    # `M2=3` (spindle 2, code M3) and `S1=2000` — see the block comment above.
    extended_address=frozenset({"M", "S"}),
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
    # Unlike every CNC table in this file, this one can be taken almost at
    # face value: Marlin is open source and one firmware, so an M-code means
    # what the source says it means. The caveat is VERSION, not builder —
    # older forks and vendor firmware (Prusa, Klipper's Marlin emulation)
    # implement subsets, and a few codes need a compile-time option enabled.
    known_m={
        # --- job / machine control ---------------------------------------
        "M0": "**M0 — Unconditional stop**, wait for user.",
        "M1": "**M1 — Stop and wait for user**, same as `M0` in Marlin.",
        # Marlin does not only drive printers: the same firmware runs laser
        # engravers and small routers, and there it really does implement
        # M3/M4/M5 and the coolant trio (air assist). They were already in
        # this dialect's inherited spindle_on/coolant_on sets, so leaving
        # them undocumented meant the lint called its own state codes
        # unknown. The spindle and coolant RULES stay off for Marlin — a
        # print file has no spindle and shouldn't be nagged about one.
        "M3": "**M3 — Spindle/laser on, CW** (`S` = power or rpm). Needs the spindle/laser feature compiled in.",
        "M4": "**M4 — Spindle/laser on, CCW.** On lasers this is dynamic power mode, scaled with feedrate.",
        "M5": "**M5 — Spindle/laser off.**",
        "M7": "**M7 — Mist coolant / air assist on** (needs `COOLANT_CONTROL`).",
        "M8": "**M8 — Flood coolant on** (needs `COOLANT_CONTROL`).",
        "M9": "**M9 — Coolant off.**",
        "M17": "**M17 — Enable steppers** (energize the motors).",
        "M18": "**M18 — Disable steppers**, identical to `M84`.",
        "M80": "**M80 — Power supply ON** (ATX or relay-controlled PSU).",
        "M81": "**M81 — Power supply OFF.**",
        "M82": "**M82 — Extruder absolute mode.**",
        "M83": "**M83 — Extruder relative mode.**",
        "M84": "**M84 — Disable steppers.** `S` sets an idle timeout instead.",
        "M85": "**M85 — Inactivity shutdown timer** (`S` seconds; `S0` disables it).",
        "M108": "**M108 — Break out of a heating wait** (`M109`/`M190`) or continue past `M0`.",
        "M110": "**M110 — Set the line number** used by checksum-verified serial comms.",
        "M111": "**M111 — Set the debug output level** (`S`).",
        "M112": "**M112 — EMERGENCY STOP.** Kills heaters and motion; the board needs `M999` or a reset afterwards.",
        "M115": "**M115 — Report firmware name, version and capabilities.**",
        "M117": "**M117 — Show a message on the LCD.**",
        "M118": "**M118 — Echo a string back over serial** (host-script signalling).",
        "M125": "**M125 — Park the head** and wait — the mechanism behind runout and pause handling.",
        "M226": "**M226 — Wait for a pin to reach a state** (`P` = pin, `S` = state).",
        "M300": "**M300 — Play a tone** (`S` = Hz, `P` = milliseconds).",
        "M400": "**M400 — Wait for all queued moves to finish** before the next command. The planner-flush you need before probing or measuring.",
        "M997": "**M997 — Begin a firmware update** (boards that support it).",
        "M999": "**M999 — Restart after an emergency stop or halt.**",
        # --- temperature ---------------------------------------------------
        "M104": "**M104 — Set hotend temperature** and continue (no wait).",
        "M105": "**M105 — Report current temperatures.**",
        "M106": "**M106 — Part-cooling fan on** (`S0–255`).",
        "M107": "**M107 — Part-cooling fan off.**",
        "M109": "**M109 — Set hotend temperature and WAIT** until reached. `R` waits for cooling too, `S` only for heating.",
        "M140": "**M140 — Set bed temperature** and continue (no wait).",
        "M141": "**M141 — Set chamber temperature** and continue.",
        "M149": "**M149 — Set temperature units** (`C`, `K` or `F`) for reports.",
        "M155": "**M155 — Auto-report temperatures** every `S` seconds.",
        "M190": "**M190 — Set bed temperature and WAIT** until reached.",
        "M191": "**M191 — Set chamber temperature and WAIT.**",
        "M301": "**M301 — Set hotend PID values** (`P`/`I`/`D`).",
        "M302": "**M302 — Allow cold extrusion** (`S` = minimum extrude temperature, `P1` = allow). How you unload filament cold.",
        "M303": "**M303 — PID autotune** (`E` heater, `S` target, `C` cycles).",
        "M304": "**M304 — Set bed PID values.**",
        # --- SD card -------------------------------------------------------
        # M30 is the trap that justifies dialect tables all by itself, and
        # M29 is a quieter one: rigid tapping on a Fanuc, stop-writing here.
        "M20": "**M20 — List SD card contents.**",
        "M21": "**M21 — Initialize the SD card.**",
        "M22": "**M22 — Release the SD card.**",
        "M23": "**M23 — Select an SD file** by name.",
        "M24": "**M24 — Start or resume the SD print.**",
        "M25": "**M25 — Pause the SD print.**",
        "M26": "**M26 — Set the SD read position** (`S` = byte offset).",
        "M27": "**M27 — Report SD print status.**",
        "M28": "**M28 — Begin writing a file to SD.**",
        "M29": "**M29 — Stop writing to SD** and close the file. On a Fanuc mill this same number is rigid-tapping mode.",
        "M30": "**M30 — Delete file from SD card** (Marlin!). On CNC controls this is program end.",
        "M31": "**M31 — Report how long the current print has been running.**",
        "M32": "**M32 — Select an SD file and start printing it.**",
        "M524": "**M524 — Abort the current SD print.**",
        "M928": "**M928 — Start logging serial output to an SD file.**",
        # --- print job progress --------------------------------------------
        "M73": "**M73 — Set print progress** (`P` = percent, `R` = minutes remaining) for the display. Slicers emit these.",
        "M75": "**M75 — Start the print job timer.**",
        "M76": "**M76 — Pause the print job timer.**",
        "M77": "**M77 — Stop the print job timer.**",
        "M78": "**M78 — Report print job statistics.**",
        # --- motion tuning and limits ---------------------------------------
        "M92": "**M92 — Set axis steps per unit** (`X`/`Y`/`Z`/`E`).",
        "M114": "**M114 — Report current position.**",
        "M119": "**M119 — Report endstop states** — the fastest way to check wiring.",
        "M120": "**M120 — Enable endstops.**",
        "M121": "**M121 — Disable endstops.**",
        "M201": "**M201 — Set max print acceleration** per axis (mm/s²).",
        "M203": "**M203 — Set max feedrate** per axis (mm/s).",
        "M204": "**M204 — Set acceleration**: `P` printing, `R` retract, `T` travel.",
        "M205": "**M205 — Advanced settings**: jerk (`X`/`Y`/`Z`/`E`) or junction deviation (`J`), min feedrate (`S`), min segment time (`B`).",
        "M206": "**M206 — Set home offset** — shifts where the machine thinks zero is after homing.",
        "M207": "**M207 — Set firmware retraction** (`S` length, `F` feedrate, `Z` hop).",
        "M208": "**M208 — Set firmware recover (unretract)** length and feedrate.",
        "M209": "**M209 — Enable/disable automatic firmware retract** (`S1`/`S0`) — turns every `G10`/`G11` on.",
        "M211": "**M211 — Enable/disable software endstops** (`S1`/`S0`).",
        "M220": "**M220 — Set feedrate percentage** (`S`) — the speed knob, in G-code.",
        "M221": "**M221 — Set flow percentage** (`S`) — extrusion multiplier.",
        "M290": "**M290 — Babystep** (`Z`) — nudge the nozzle while printing without changing offsets.",
        "M900": "**M900 — Linear Advance factor** (`K`) — pressure compensation for extrusion.",
        # --- probing and bed leveling ---------------------------------------
        "M401": "**M401 — Deploy the Z probe.**",
        "M402": "**M402 — Stow the Z probe.**",
        "M420": "**M420 — Bed leveling state** (`S1`/`S0`), `Z` fade height, `L` loads a stored mesh.",
        "M421": "**M421 — Set a single mesh point** (`I`/`J` index, `Z` value).",
        "M425": "**M425 — Backlash compensation** settings.",
        "M851": "**M851 — Probe offsets** (`X`/`Y`/`Z`). The `Z` value is the classic first-layer adjustment.",
        # --- hardware / drivers ----------------------------------------------
        "M42": "**M42 — Set a pin state** (`P` = pin, `S` = value). Direct I/O; easy to damage things with.",
        "M150": "**M150 — Set RGB LED colour.** `R` red, `U` GREEN (`G` was already taken by G-codes), `B` blue, `P` brightness.",
        "M250": "**M250 — Set LCD contrast** (`C`).",
        "M280": "**M280 — Set servo position** (`P` = index, `S` = angle). Deploys BLTouch-style probes.",
        "M350": "**M350 — Set microstepping mode.**",
        "M569": "**M569 — Set TMC stepper driver mode**: `S0` spreadCycle, `S1` stealthChop.",
        "M710": "**M710 — Controller fan settings.**",
        "M906": "**M906 — Set TMC driver current** (mA).",
        "M907": "**M907 — Set motor current** (digipot boards).",
        "M914": "**M914 — Set TMC StallGuard homing sensitivity** (sensorless homing).",
        # --- filament ---------------------------------------------------------
        "M600": "**M600 — Filament change.** Parks, unloads and waits for the user.",
        "M603": "**M603 — Configure the filament change** load/unload lengths used by `M600`.",
        "M701": "**M701 — Load filament** (`L` = length).",
        "M702": "**M702 — Unload filament.**",
        # --- settings storage --------------------------------------------------
        "M500": "**M500 — Save settings to EEPROM.**",
        "M501": "**M501 — Load settings from EEPROM**, discarding unsaved changes.",
        "M502": "**M502 — Reset settings to firmware defaults** (not saved until `M500`).",
        "M503": "**M503 — Report the current settings.**",
        "M504": "**M504 — Validate the EEPROM contents.**",
    },
    # A printer has no canned cycles. This was inert anyway (the coolant
    # rule is off for Marlin), but leaving the Fanuc drilling family in
    # here implied a G81 would mean something on a 3D printer.
    cycle_codes=frozenset(),
)

# Okuma OSP. This M table is deliberately the thinnest in the file, and
# that is a statement rather than an oversight: OSP machines carry a large
# M-code list, but it is assigned per machine and per option package, and
# the published lists disagree with each other. Guessing here would be
# worse than useless — a wrong hover on a machinist's screen outranks a
# missing one — so this ships the codes that are common to every OSP
# control and leaves the rest for you to paste in from YOUR machine's
# M-code list, which is the one document that is actually authoritative.
OKUMA = Dialect(
    name="okuma",
    title="Okuma OSP",
    rules=ALL_RULES,
    known_g={
        **_BASE_G,
        "G15": "**G15 — Select work coordinate system** by number (`H` word) (Okuma).",
        "G16": "**G16 — Rotary axis coordinate designation** (Okuma).",
    },
    known_m={
        **_BASE_M,
        # Kept (they arrive from the base table) but re-documented, because
        # a post writing Okuma's native subprogram syntax will never emit
        # them and a programmer coming from Fanuc needs to know why.
        "M98": "**M98 — Call subprogram** (`P` = program number), Fanuc-style. Okuma's own form is `CALL O1000`, returning with `RTS` — if your post writes `CALL`, you will never see this code.",
        "M99": "**M99 — Return from subprogram**, Fanuc-style. Okuma's native equivalent is `RTS`.",
    },
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
