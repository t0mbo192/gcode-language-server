"""
gcode_parser.py — the modal-state engine. This is the heart of the project.

It has ZERO dependencies, so you can prove it works with no editor and no
LSP anywhere in sight:

    python server/gcode_parser.py examples/demo.nc

That separation is the whole architecture in one sentence:

  * THIS file understands G-code (tokenizing, modal state, lint rules).
  * dialects.py holds all dialect knowledge as data tables.
  * server.py only translates between this file and the Language Server
    Protocol — it contains no G-code knowledge at all.

If you ever want to swap in a different parsing engine, this is the file
it replaces or merges into. Keep the `GCodeParser.check_line()` interface
and server.py never changes.

----------------------------------------------------------------------------
Why a linter for G-code must be a state machine, not a regex
----------------------------------------------------------------------------
G-code is *modal*: most words stay in force until something cancels them.
`G1` puts the control in "linear feed" mode, and every later line with only
axis words is ALSO a G1 move. `F200.` sets the feedrate for every feed move
that follows. So whether `G1 X5.` is fine or a wreck depends on what
happened 300 lines earlier — which is exactly the class of mistake a
per-line regex highlighter (like a TextMate grammar) can never catch.
`ModalState` below is that memory; each rule reads it and each line
updates it.
"""

from dataclasses import dataclass, field
import re
import sys

try:
    from dialects import (
        DIALECTS, DEFAULT_DIALECT, resolve_dialect,
        R_FEED_MISSING, R_SPINDLE_OFF, R_G43_NO_H, R_NO_TLO_AFTER_M6,
        R_COMP_AT_END, R_ARC_NO_CENTER, R_UNKNOWN_CODE, R_NO_COOLANT,
    )
except ImportError:  # allow "from server import gcode_parser" style, too
    from server.dialects import (
        DIALECTS, DEFAULT_DIALECT, resolve_dialect,
        R_FEED_MISSING, R_SPINDLE_OFF, R_G43_NO_H, R_NO_TLO_AFTER_M6,
        R_COMP_AT_END, R_ARC_NO_CENTER, R_UNKNOWN_CODE, R_NO_COOLANT,
    )

# LSP severity values (kept as plain ints so this file stays dependency-free;
# lsprotocol's DiagnosticSeverity uses the same numbers).
SEVERITY_ERROR = 1
SEVERITY_WARNING = 2
SEVERITY_INFO = 3


@dataclass
class Issue:
    """One problem found on one line. col/end are 0-based columns into the
    original line text, which is exactly what LSP ranges want."""
    col: int
    end: int
    msg: str
    severity: int = SEVERITY_WARNING
    rule: str = ""


@dataclass
class Word:
    """One G-code word: a letter plus its number, e.g. G1, X-12.5, H04.
    Columns point into the ORIGINAL line so squiggles land precisely."""
    letter: str   # always upper-case
    number: str   # as written, e.g. "01", "-12.5", "38.2"
    col: int
    end: int


def normalize_code(letter, number):
    """Canonical form for table lookups: strip leading zeros, keep decimals.
    G01 -> G1, M03 -> M3, G38.2 -> G38.2."""
    if "." in number:
        whole, frac = number.split(".", 1)
        return f"{letter}{int(whole or '0')}.{frac}"
    return f"{letter}{int(number)}"


@dataclass
class ModalState:
    """Everything the machine 'remembers' between lines that our rules need.
    A real control tracks far more; add fields here as you add rules."""
    motion: str = None        # active motion mode: "G0" "G1" "G2" "G3" or None
    feed: float = None        # active F word, None until one is seen
    spindle_on: bool = False  # M3/M4 set it, M5 clears it
    spindle_speed: float = None
    tool: int = None          # completed by M6 from the staged T word
    staged_tool: int = None   # T word seen but M6 not yet executed
    comp: str = None          # cutter comp: None, "G41" or "G42"
    tlo: bool = False         # tool length offset applied (G43/G44)
    awaiting_tlo: bool = False  # armed by M6, cleared by G43/G44
    coolant_on: bool = False  # M7/M8 set it, M9 clears it
    # The coolant check works like awaiting_tlo, but is armed from line 1:
    # a program that starts cutting with whatever tool is already in the
    # spindle still owes that tool coolant. M6 re-arms it for each new tool.
    awaiting_coolant: bool = True
    units: str = None         # "mm" / "inch" (tracked for future rules)
    plane: str = "G17"        # arc plane (tracked for future rules)
    absolute: bool = True     # G90/G91 (tracked for future rules)


# --- tokenizing ------------------------------------------------------------

_COMMENT_PAREN = re.compile(r"\([^)]*\)")
_COMMENT_SEMI = re.compile(r";.*")
# A word = letter + signed number. Whitespace between them is legal G-code.
_WORD_RE = re.compile(r"(?i)([A-Z])\s*([+-]?(?:\d+\.\d*|\.\d+|\d+))")

MOTION_CODES = {"G0", "G1", "G2", "G3"}
CUTTING_CODES = {"G1", "G2", "G3"}          # moves that actually cut
# Which G words start cutting the moment they execute is per-dialect data
# now (Dialect.cycle_codes): Fanuc's G81 family drills its first hole on the
# definition line itself, while Heidenhain only DEFINES with G200+ and cuts
# at the call (G79). The coolant rule treats these as a first cut; extending
# the feed and spindle rules to them is a possible follow-up.
# E is the extruder axis on 3D printers — a G1 E5 line is still a move.
AXIS_LETTERS = set("XYZABCUVWE")
ARC_LETTERS = ("I", "J", "K", "R")
# Letters we understand but don't lint (N line numbers, O program numbers,
# P/Q/L cycle parameters...). Anything else is quietly ignored too —
# dialects vary far too much for an unknown-letter rule to be trustworthy.
_SEV_NAME = {1: "error", 2: "warning", 3: "info"}


class GCodeParser:
    """Feed it lines top-to-bottom; it accumulates modal state and returns
    the Issues each line raises. One instance per file, per pass."""

    def __init__(self, dialect: str = DEFAULT_DIALECT):
        self.dialect_name = dialect
        self.dialect = DIALECTS.get(dialect, DIALECTS[DEFAULT_DIALECT])
        self.state = ModalState()

    # -- helpers ------------------------------------------------------------

    def _on(self, rule):
        return rule in self.dialect.rules

    @staticmethod
    def _mask_comments(line):
        """Blank out comments with spaces instead of deleting them, so every
        surviving word keeps its original column for exact squiggles."""
        blank = lambda m: " " * (m.end() - m.start())
        line = _COMMENT_PAREN.sub(blank, line)
        line = _COMMENT_SEMI.sub(blank, line)
        return line

    @staticmethod
    def tokenize(line):
        code = GCodeParser._mask_comments(line)
        # Block delete: a leading '/' means "skip this line when the switch
        # is on". The switch is usually off, so we lint the line anyway.
        stripped = code.lstrip()
        if stripped.startswith("/"):
            i = code.index("/")
            code = code[:i] + " " + code[i + 1:]
        return [Word(m.group(1).upper(), m.group(2), m.start(1), m.end(2))
                for m in _WORD_RE.finditer(code)]

    # -- the engine ----------------------------------------------------------

    def check_line(self, line):
        """Update modal state with one line and return its list of Issues.
        This is the interface server.py (and your own parser) builds on."""
        issues = []
        st = self.state
        words = self.tokenize(line)
        if not words:
            return issues  # blank line, pure comment, or a lone '%'

        by_letter = {}
        for w in words:
            by_letter.setdefault(w.letter, []).append(w)
        g_words = by_letter.get("G", [])
        m_words = by_letter.get("M", [])

        # --- 1. words that set state, BEFORE any rule runs -----------------
        # G-code semantics: words on the same line take effect together, so
        # "G1 X10. F150." is legal — the F counts for that same move.
        if "F" in by_letter:
            st.feed = float(by_letter["F"][-1].number)
        if "S" in by_letter:
            st.spindle_speed = float(by_letter["S"][-1].number)
        if "T" in by_letter:
            st.staged_tool = int(float(by_letter["T"][-1].number))
            # On tool_change_on_t controls (Heidenhain) the T word IS the
            # tool change — unless this line is a tool DEFINITION or
            # preselect (G99/G51 there), whose T is just a table entry.
            line_g_codes = {normalize_code(w.letter, w.number)
                            for w in g_words}
            if (self.dialect.tool_change_on_t
                    and not (line_g_codes & self.dialect.tool_def_codes)):
                st.tool = st.staged_tool
                # Same bookkeeping as M6 below: the new tool starts dry and
                # owes us a coolant-on before its first cut. No awaiting_tlo
                # though — these controls apply length comp at the call.
                st.coolant_on = False
                st.awaiting_coolant = True

        # --- 2. G words: state effects + G-specific rules -------------------
        line_motion = None      # explicit G0/G1/G2/G3 written on THIS line
        g43_on_line = False
        cycle_word = None       # canned cycle (G81, G83...) started on THIS line
        for w in g_words:
            code = normalize_code(w.letter, w.number)

            if code in MOTION_CODES:
                st.motion = code
                line_motion = w
            elif code in ("G43", "G44"):
                g43_on_line = True
                st.tlo = True
                st.awaiting_tlo = False
                # RULE: G43 without H applies offset 0 or whatever H is
                # lingering — on a real machine that's a crash generator.
                if self._on(R_G43_NO_H) and "H" not in by_letter:
                    issues.append(Issue(
                        w.col, w.end,
                        f"{code} without an H word — no length offset selected",
                        SEVERITY_ERROR, R_G43_NO_H))
            elif code == "G49":
                st.tlo = False
            elif code in ("G41", "G42"):
                st.comp = code
            elif code == "G40":
                st.comp = None
            elif code == "G80":
                st.motion = None       # canned cycle cancel
            elif code == "G20":
                st.units = "inch"
            elif code == "G21":
                st.units = "mm"
            elif code in ("G17", "G18", "G19"):
                st.plane = code
            elif code == "G90":
                st.absolute = True
            elif code == "G91":
                st.absolute = False
            elif code in self.dialect.cycle_codes:
                cycle_word = cycle_word or w

            # RULE: unknown code for this dialect. Severity is only "info"
            # because the tables in dialects.py are starting points, not
            # gospel — expand them as your posts prove codes are real.
            if self._on(R_UNKNOWN_CODE) and code not in self.dialect.known_g:
                issues.append(Issue(
                    w.col, w.end,
                    f"{code} is not a known {self.dialect.title} G-code",
                    SEVERITY_INFO, R_UNKNOWN_CODE))

        # --- 3. M words: state effects + M-specific rules -------------------
        for w in m_words:
            code = normalize_code(w.letter, w.number)

            # Spindle and coolant are read from the dialect and checked
            # INDEPENDENTLY (two ifs, not one elif chain), because combo
            # codes exist: Heidenhain's M13 is spindle-CW-plus-coolant in a
            # single number, and must land in both branches.
            if code in self.dialect.spindle_on:
                st.spindle_on = True
            elif code == "M5":
                st.spindle_on = False

            # Coolant codes come from the dialect (M51 is through-spindle
            # coolant on a Mazak, a spindle-override switch on LinuxCNC).
            if code in self.dialect.coolant_on:
                st.coolant_on = True
                st.awaiting_coolant = False   # this tool's check is satisfied
            elif code in self.dialect.coolant_off:
                st.coolant_on = False

            if code == "M6":
                st.tool = st.staged_tool
                st.tlo = False
                # Arm the "you changed tools but never applied G43" check.
                st.awaiting_tlo = True
                # Most controls kill coolant during a tool change (ATC
                # safety), and well-posted programs re-issue M8 per tool —
                # so the new tool starts dry and owes us a coolant-on.
                st.coolant_on = False
                st.awaiting_coolant = True
            elif code in ("M2", "M30"):
                # RULE: ending the program with cutter comp still active.
                # The next program (or a restart) inherits a sideways offset.
                if self._on(R_COMP_AT_END) and st.comp:
                    issues.append(Issue(
                        w.col, w.end,
                        f"Program ends with cutter compensation ({st.comp}) "
                        f"still active — add G40 before {code}",
                        SEVERITY_WARNING, R_COMP_AT_END))
                # Files sometimes hold several programs; start fresh.
                self.state = st = ModalState()

            if self._on(R_UNKNOWN_CODE) and code not in self.dialect.known_m:
                issues.append(Issue(
                    w.col, w.end,
                    f"{code} is not a known {self.dialect.title} M-code",
                    SEVERITY_INFO, R_UNKNOWN_CODE))

        # --- 4. motion rules -------------------------------------------------
        axis_words = [w for w in words if w.letter in AXIS_LETTERS]
        # A line "moves" if it writes a motion G word, or if it has axis
        # words while a motion mode is modally active (the classic
        # "X5. on its own line is still a G1" situation).
        moves = line_motion is not None or (
            bool(axis_words) and st.motion in MOTION_CODES)
        cutting_anchor = None   # set when THIS line makes a cutting move

        if moves:
            active = line_motion and normalize_code("G", line_motion.number) \
                or st.motion
            # Where to draw the squiggle: the G word if written, else the
            # first axis word that implies the move.
            anchor = line_motion or axis_words[0]

            if active in CUTTING_CODES:
                cutting_anchor = anchor
                # RULE: feed move with no feedrate ever set. Most controls
                # alarm out; the ones that don't will cut at whatever was
                # left in the register. Both are wrong.
                if self._on(R_FEED_MISSING) and st.feed is None:
                    issues.append(Issue(
                        anchor.col, anchor.end,
                        f"{active} cutting move but no feedrate is active — "
                        f"add an F word", SEVERITY_WARNING, R_FEED_MISSING))

                # RULE: cutting with the spindle stopped. Snapped tool, or a
                # push-broach finish you didn't order.
                if self._on(R_SPINDLE_OFF) and not st.spindle_on:
                    issues.append(Issue(
                        anchor.col, anchor.end,
                        f"{active} cutting move while the spindle is stopped "
                        f"(no M3/M4 active)", SEVERITY_WARNING, R_SPINDLE_OFF))

            # RULE: arc without geometry. Only checked when G2/G3 is written
            # on the line itself — modal arc continuation is control-specific
            # enough that flagging it produces false positives.
            if line_motion is not None and active in ("G2", "G3"):
                if self._on(R_ARC_NO_CENTER) and not any(
                        l in by_letter for l in ARC_LETTERS):
                    issues.append(Issue(
                        line_motion.col, line_motion.end,
                        f"{active} arc without I/J/K or R — no center or "
                        f"radius given", SEVERITY_ERROR, R_ARC_NO_CENTER))

            # RULE: Z motion after a tool change with no G43 in between.
            # The new tool's length differs from the old one's; without
            # length comp the control still thinks in old-tool Z.
            if (self._on(R_NO_TLO_AFTER_M6) and st.awaiting_tlo
                    and "Z" in by_letter and not g43_on_line):
                zw = by_letter["Z"][0]
                issues.append(Issue(
                    zw.col, zw.end,
                    "Z move after a tool change (M6) but no G43 tool length "
                    "offset has been applied", SEVERITY_WARNING,
                    R_NO_TLO_AFTER_M6))
                # Warn once per tool change, not on every following line.
                st.awaiting_tlo = False

        # --- 5. the coolant check (per tool, not per line) -------------------
        # RULE: every tool must turn coolant on — any code in the dialect's
        # coolant_on set (M7/M8 on most, plus each control's extras: Mazak
        # M50/M51, Haas M73/M83/M88, Heidenhain's combo M13/M14) — before
        # its first cut.
        # This is a shop-floor rule: dry cutting is occasionally intended
        # (cast iron, graphite), which is why it's a warning and fires only
        # ONCE per tool — on the first cutting move or canned-cycle start
        # after the tool change — instead of nagging on every line. Coolant
        # on the same line as the cut counts, just like a same-line F word,
        # because M words execute with the move on real controls.
        first_cut = cycle_word or cutting_anchor
        if (self._on(R_NO_COOLANT) and st.awaiting_coolant
                and first_cut is not None and not st.coolant_on):
            who = f"T{st.tool}" if st.tool is not None else "the active tool"
            # Name the codes THIS dialect accepts (M7/M8 on a Fanuc, up to
            # M7/M8/M50/M51 on a Mazak) so the fix is right in the message.
            cool = "/".join(sorted(self.dialect.coolant_on,
                                   key=lambda c: float(c[1:])))
            issues.append(Issue(
                first_cut.col, first_cut.end,
                f"First cut with {who} but coolant is off — no coolant code "
                f"({cool}) since the tool change",
                SEVERITY_WARNING, R_NO_COOLANT))
            st.awaiting_coolant = False   # said it once; that's enough

        return issues


# --- file-level convenience (used by the CLI and handy for tests) ----------

def check_text(text, dialect=None, path=None):
    """Lint a whole program. Returns (dialect_used, [(line_no, Issue), ...])
    with 0-based line numbers."""
    dialect = dialect or resolve_dialect(path=path, text=text)
    parser = GCodeParser(dialect)
    found = []
    for n, line in enumerate(text.splitlines()):
        for issue in parser.check_line(line):
            found.append((n, issue))
    return dialect, found


def main(argv):
    """CLI entry point — the 'prove the engine works' path:

        python server/gcode_parser.py examples/demo.nc
    """
    if len(argv) < 2:
        print("usage: python gcode_parser.py FILE [FILE ...]")
        return 2
    exit_code = 0
    for path in argv[1:]:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        dialect, found = check_text(text, path=path)
        print(f"{path} — dialect: {dialect}")
        for n, issue in found:
            print(f"  line {n + 1}, col {issue.col + 1}: "
                  f"{_SEV_NAME[issue.severity]} [{issue.rule}] {issue.msg}")
        print(f"  {len(found)} problem(s) found")
        if found:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
