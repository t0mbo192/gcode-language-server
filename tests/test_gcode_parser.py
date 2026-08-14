"""Tests for the engine: tokenizing, modal state, and each lint rule.

Style note: nearly every test asserts on RULE IDS rather than on message
text. Messages are meant to be reworded as the wording gets better on the
shop floor; the rule id is the contract that the settings, the README table
and the Problems panel all agree on.
"""

import unittest

from context import gcode_parser as gp
from context import dialects as d


# tokenize() is a staticmethod on the parser class, not a module function.
tokenize = gp.GCodeParser.tokenize


def rules(text, dialect="fanuc"):
    """Every rule id raised by a program, in order."""
    _, found = gp.check_text(text, dialect=dialect)
    return [issue.rule for _, issue in found]


def rules_at(text, dialect="fanuc"):
    """(0-based line number, rule id) pairs — for asserting WHERE a rule
    fires, which is half of what makes a linter usable."""
    _, found = gp.check_text(text, dialect=dialect)
    return [(n, issue.rule) for n, issue in found]


# A program that does everything right. Individual tests below break exactly
# one thing about it, which keeps each test honest about what it proves.
CLEAN = """\
G21 G17 G90 G54
T1 M6
G0 X0 Y0
G43 H1 Z25.0
M3 S2000 M8
G1 Z-1.0 F250.0
G1 X50.0
G2 X60.0 Y10.0 I0 J10.0
G40 G0 Z100.0 M9
M5
M30
"""


class TestNormalizeCode(unittest.TestCase):
    """Table lookups live or die on this: G01 and G1 must be one key."""

    def test_strips_leading_zeros(self):
        self.assertEqual(gp.normalize_code("G", "01"), "G1")
        self.assertEqual(gp.normalize_code("M", "03"), "M3")
        self.assertEqual(gp.normalize_code("G", "0"), "G0")
        self.assertEqual(gp.normalize_code("G", "000"), "G0")

    def test_keeps_decimals(self):
        self.assertEqual(gp.normalize_code("G", "38.2"), "G38.2")
        self.assertEqual(gp.normalize_code("G", "54.1"), "G54.1")

    def test_bare_decimal_gets_a_zero(self):
        self.assertEqual(gp.normalize_code("G", ".5"), "G0.5")

    def test_signed(self):
        self.assertEqual(gp.normalize_code("M", "-05"), "M-5")
        self.assertEqual(gp.normalize_code("M", "+05"), "M5")

    def test_absurdly_long_number_does_not_raise(self):
        """Regression: int() refuses >4300-digit strings on Python 3.11+,
        and the exception used to unwind all the way out of check_line and
        cost the whole FILE its diagnostics."""
        code = gp.normalize_code("G", "9" * 5000)
        self.assertEqual(code, "G" + "9" * 5000)


class TestTokenize(unittest.TestCase):

    def test_letter_number_pairs_with_columns(self):
        words = tokenize("G1 X-12.5")
        self.assertEqual([(w.letter, w.number) for w in words],
                         [("G", "1"), ("X", "-12.5")])
        # Columns must point into the ORIGINAL line or squiggles land wrong.
        self.assertEqual((words[1].col, words[1].end), (3, 9))

    def test_space_between_letter_and_number_is_legal(self):
        words = tokenize("G 1 X 5.")
        self.assertEqual([w.letter for w in words], ["G", "X"])

    def test_lowercase_is_upper_cased(self):
        self.assertEqual(tokenize("g1 x5")[0].letter, "G")

    def test_paren_comment_is_masked_but_columns_survive(self):
        words = tokenize("G1 (X99 rapid home) Y5.")
        self.assertEqual([w.letter for w in words], ["G", "Y"])
        self.assertEqual(words[1].col, 20)

    def test_semicolon_comment_is_masked(self):
        self.assertEqual([w.letter for w in tokenize("G1 X5 ; Y99")],
                         ["G", "X"])

    def test_block_delete_line_is_still_linted(self):
        # The block-delete switch is usually OFF, so '/' lines do run.
        self.assertEqual([w.letter for w in tokenize("/G1 X5")],
                         ["G", "X"])

    def test_lines_with_no_words(self):
        for line in ("", "   ", "%", "(comment only)", "; comment"):
            self.assertEqual(tokenize(line), [], line)


class TestSiemensExtendedAddressing(unittest.TestCase):
    """`M2=3` is "spindle 2, code M3", not a bare M2.

    This one was a cascade, not a single wrong squiggle: M2 is PROGRAM END
    on a SINUMERIK, so reading it that way reset the modal state mid-file
    and every line afterwards inherited a machine with no feed and no
    spindle. A legal counter-spindle program raised six false warnings.
    """

    TURN_MILL = (
        "N10 G17 G54\n"
        "N20 M1=3 S1=2000 M8\n"       # main spindle CW at 2000
        "N30 G1 X10 F250\n"
        "N40 M2=3 S2=1500\n"          # counter-spindle CW at 1500
        "N50 G1 X20\n"
        "N60 G1 X30\n"
        "N70 M30\n"
    )

    def test_legal_turn_mill_program_is_silent(self):
        self.assertEqual(rules(self.TURN_MILL, dialect="siemens"), [])

    def test_value_after_equals_is_the_code(self):
        words = tokenize("M2=3", d.SIEMENS.extended_address)
        self.assertEqual([(w.letter, w.number) for w in words], [("M", "3")])

    def test_span_covers_the_whole_word(self):
        """Squiggles and hovers should cover `M2=3`, not just the `M2`."""
        word = tokenize("M2=3", d.SIEMENS.extended_address)[0]
        self.assertEqual((word.col, word.end), (0, 4))

    def test_speed_is_read_from_the_indexed_form(self):
        words = tokenize("S1=2000", d.SIEMENS.extended_address)
        self.assertEqual([(w.letter, w.number) for w in words],
                         [("S", "2000")])

    def test_indexed_spindle_start_counts_as_spindle_on(self):
        text = "M2=3 S1=1000 M8\nG1 X1 F100.\nM30\n"
        found = rules(text, dialect="siemens")
        self.assertNotIn(d.R_SPINDLE_OFF, found)
        self.assertNotIn(d.R_NO_COOLANT, found)

    def test_indexed_gear_stage_is_a_known_code(self):
        """M1=40 is gear-stage-auto on spindle 1, and M40 is documented."""
        text = "M1=40\nM3 S1000 M8\nG1 X1 F100.\nM30\n"
        self.assertNotIn(d.R_UNKNOWN_CODE, rules(text, dialect="siemens"))

    def test_plain_m2_is_still_program_end(self):
        """The whole point is telling the two apart, so check the other
        side of the distinction too."""
        text = "M3 S1000 M8\nG1 X1 F100.\nM2\nG1 X2\nM30\n"
        # State reset by M2 means the trailing move has no feed again.
        self.assertIn(d.R_FEED_MISSING, rules(text, dialect="siemens"))

    def test_other_dialects_tokenize_exactly_as_before(self):
        """Only dialects that opt in act on the '=' tail. Fanuc reads the
        bare M2 — and, as before the change, no stray word from the tail."""
        words = tokenize("M2=3")
        self.assertEqual([(w.letter, w.number) for w in words], [("M", "2")])

    def test_fanuc_macro_assignments_are_untouched(self):
        """`#100=5` has no letter, so it never was a word and still isn't."""
        self.assertEqual(tokenize("#100=5"), [])

    def test_whitespace_around_equals_is_not_extended_addressing(self):
        """Kept strict on purpose: Heidenhain writes `Q1 = +10` in FN
        parameter assignments, and that must not become Q+10."""
        words = tokenize("Q1 = +10", frozenset({"Q"}))
        self.assertEqual([(w.letter, w.number) for w in words], [("Q", "1")])


class TestModalState(unittest.TestCase):
    """The reason this project isn't a regex: state carries across lines."""

    def test_feed_persists_across_lines(self):
        self.assertNotIn(d.R_FEED_MISSING,
                         rules("M3 S1000 M8\nG1 X1 F100.\nG1 X2\nM30"))

    def test_same_line_feed_counts(self):
        # M and F words execute together with the move on a real control.
        self.assertNotIn(d.R_FEED_MISSING, rules("M3 S1000 M8\nG1 X1 F100.\nM30"))

    def test_motion_mode_is_modal(self):
        """An axis-only line after G1 is still a cutting move — this is the
        classic case a per-line highlighter cannot catch."""
        self.assertIn(d.R_SPINDLE_OFF, rules("G1 X1 F100.\nX2\nM30"))

    def test_g0_axis_only_line_is_not_a_cut(self):
        self.assertEqual(rules("M3 S1\nG0 X1\nX2\nM30"), [])

    def test_m30_resets_state_for_a_second_program(self):
        """Files sometimes hold several programs back to back."""
        text = CLEAN + "T2 M6\nG0 X0\nG43 H2 Z25.\nG1 Z-1. F100.\nM30\n"
        # The second program never starts its spindle or coolant, and the
        # reset is what lets those rules notice.
        found = rules(text)
        self.assertIn(d.R_SPINDLE_OFF, found)
        self.assertIn(d.R_NO_COOLANT, found)


class TestCleanProgram(unittest.TestCase):

    def test_no_false_positives(self):
        """The single most important test in the file: a correct program
        must be silent, or nobody keeps the extension installed."""
        self.assertEqual(rules(CLEAN), [])


class TestRules(unittest.TestCase):
    """One test per rule, each breaking exactly one thing in CLEAN."""

    def test_feed_missing(self):
        text = CLEAN.replace("G1 Z-1.0 F250.0", "G1 Z-1.0")
        self.assertIn(d.R_FEED_MISSING, rules(text))

    def test_spindle_off(self):
        text = CLEAN.replace("M3 S2000 M8", "M8")
        self.assertIn(d.R_SPINDLE_OFF, rules(text))

    def test_g43_missing_h(self):
        text = CLEAN.replace("G43 H1 Z25.0", "G43 Z25.0")
        self.assertIn(d.R_G43_NO_H, rules(text))

    def test_no_g43_after_toolchange(self):
        text = CLEAN.replace("G43 H1 Z25.0", "Z25.0")
        self.assertIn(d.R_NO_TLO_AFTER_M6, rules(text))

    def test_no_g43_warns_once_per_tool_change(self):
        text = CLEAN.replace("G43 H1 Z25.0", "Z25.0\nZ20.0\nZ15.0")
        self.assertEqual(rules(text).count(d.R_NO_TLO_AFTER_M6), 1)

    def test_comp_active_at_end(self):
        # Turn comp on AND drop the G40 that CLEAN cancels it with.
        text = (CLEAN.replace("G1 X50.0", "G41 D1 X50.0")
                     .replace("G40 G0 Z100.0 M9", "G0 Z100.0 M9"))
        self.assertIn(d.R_COMP_AT_END, rules(text))

    def test_comp_cancelled_before_end_is_fine(self):
        text = CLEAN.replace("G1 X50.0", "G41 D1 X50.0")
        self.assertIn("G40", text)          # CLEAN already cancels it
        self.assertNotIn(d.R_COMP_AT_END, rules(text))

    def test_arc_missing_center(self):
        text = CLEAN.replace("G2 X60.0 Y10.0 I0 J10.0", "G2 X60.0 Y10.0")
        self.assertIn(d.R_ARC_NO_CENTER, rules(text))

    def test_arc_with_radius_is_fine(self):
        text = CLEAN.replace("G2 X60.0 Y10.0 I0 J10.0", "G2 X60.0 Y10.0 R10.0")
        self.assertNotIn(d.R_ARC_NO_CENTER, rules(text))

    def test_unknown_code_is_info_level(self):
        _, found = gp.check_text(CLEAN.replace("M5", "M5 M123"))
        unknown = [i for _, i in found if i.rule == d.R_UNKNOWN_CODE]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].severity, gp.SEVERITY_INFO)

    def test_squiggle_lands_on_the_offending_word(self):
        """Columns are the difference between a useful squiggle and noise."""
        _, found = gp.check_text("G1 X5. F100.\nM30", dialect="fanuc")
        issue = next(i for _, i in found if i.rule == d.R_SPINDLE_OFF)
        self.assertEqual((issue.col, issue.end), (0, 2))   # the "G1"


class TestCoolantRule(unittest.TestCase):
    """The shop-floor rule, and the one with the most dialect data behind
    it — worth its own class."""

    def test_dry_tool_is_flagged_once(self):
        text = CLEAN.replace("M3 S2000 M8", "M3 S2000")
        self.assertEqual(rules(text).count(d.R_NO_COOLANT), 1)

    def test_same_line_coolant_counts(self):
        text = CLEAN.replace("M3 S2000 M8", "M3 S2000").replace(
            "G1 Z-1.0 F250.0", "G1 Z-1.0 F250.0 M8")
        self.assertNotIn(d.R_NO_COOLANT, rules(text))

    def test_rearms_for_each_tool(self):
        text = CLEAN.replace("M30", "T2 M6\nG0 X0\nG43 H2 Z5.\n"
                                    "M3 S1000\nG1 Z-1. F100.\nM30")
        self.assertEqual(rules(text).count(d.R_NO_COOLANT), 1)

    def test_canned_cycle_counts_as_a_cut(self):
        text = ("T1 M6\nG0 X0 Y0\nG43 H1 Z25.\nM3 S1000\n"
                "G81 Z-10. R2. F100.\nG80\nM30\n")
        self.assertIn(d.R_NO_COOLANT, rules(text))

    def test_program_starting_with_a_tool_already_in_the_spindle(self):
        """No M6 anywhere: the tool in the spindle still owes us coolant."""
        self.assertIn(d.R_NO_COOLANT,
                      rules("G54\nM3 S1000\nG1 X1 F100.\nM30"))

    def test_message_names_this_dialects_codes(self):
        """A Haas message must offer M88, not just M7/M8, or the fix it
        suggests is wrong for the machine."""
        text = "T1 M6\nG0 X0\nG43 H1 Z5.\nM3 S1000\nG1 Z-1. F100.\nM30"
        _, found = gp.check_text(text, dialect="haas")
        msg = next(i.msg for _, i in found if i.rule == d.R_NO_COOLANT)
        self.assertIn("M88", msg)

    def test_haas_tsc_only_tool_passes(self):
        """A coolant-through drill programming M88 and never M8 is correct
        — the false positive this dialect data exists to prevent."""
        text = "T1 M6\nG0 X0\nG43 H1 Z5.\nM3 S1000 M88\nG1 Z-1. F100.\nM30"
        self.assertNotIn(d.R_NO_COOLANT, rules(text, dialect="haas"))

    def test_mazak_through_spindle_passes(self):
        text = "T1 M6\nG0 X0\nG43 H1 Z5.\nM3 S1000 M51\nG1 Z-1. F100.\nM30"
        self.assertNotIn(d.R_NO_COOLANT, rules(text, dialect="mazak"))

    def test_fanuc_does_not_credit_m13(self):
        """M13 is a spindle+coolant combo on many Fanuc-BASED machines, but
        on a Haas it releases the 5th-axis brake — and this dialect is also
        the fallback for files whose control was never identified. So it is
        documented for hover but must NOT satisfy the rule."""
        self.assertIn("M13", d.FANUC.known_m)
        self.assertNotIn("M13", d.FANUC.coolant_on)
        self.assertNotIn("M13", d.FANUC.spindle_on)
        text = "T1 M6\nG0 X0\nG43 H1 Z5.\nM13 S1000\nG1 Z-1. F100.\nM30"
        found = rules(text)
        self.assertIn(d.R_NO_COOLANT, found)
        self.assertNotIn(d.R_UNKNOWN_CODE, found)   # still hover-documented


class TestHeidenhainToolCall(unittest.TestCase):
    """The T word IS the tool change on a TNC — no M6 to hang the rule on."""

    def test_t_word_rearms_the_coolant_check(self):
        text = ("%test G71 *\n"
                "N10 T1 G17 S2000 M13\n"
                "N20 G0 X0 Y0 Z10\n"
                "N30 G1 Z-1 F250\n"
                "N40 T2 G17 S3000 M3\n"     # new tool, no coolant this time
                "N50 G1 Z-2 F250\n"
                "N60 M30\n")
        self.assertEqual(rules(text, dialect="heidenhain").count(
            d.R_NO_COOLANT), 1)

    def test_m13_is_both_spindle_and_coolant(self):
        text = ("N10 T1 G17 S2000 M13\n"
                "N20 G1 Z-1 F250\n"
                "N30 M30\n")
        found = rules(text, dialect="heidenhain")
        self.assertNotIn(d.R_NO_COOLANT, found)
        self.assertNotIn(d.R_SPINDLE_OFF, found)

    def test_tool_definition_line_is_not_a_tool_change(self):
        """G99 defines a tool; its T is a table entry, not a change."""
        text = ("N10 G99 T5 L+50 R+3\n"
                "N20 T1 G17 S2000 M13\n"
                "N30 G1 Z-1 F250\n"
                "N40 M30\n")
        self.assertNotIn(d.R_NO_COOLANT, rules(text, dialect="heidenhain"))

    def test_cycle_definition_does_not_count_as_a_cut(self):
        """G200 only STORES a cycle; G79 is the line that cuts."""
        text = ("N10 T1 G17 S2000 M3\n"      # spindle on, no coolant
                "N20 G200 Q200=2 Q201=-20\n"  # definition only
                "N30 M30\n")
        self.assertNotIn(d.R_NO_COOLANT, rules(text, dialect="heidenhain"))


class TestDialectRuleSets(unittest.TestCase):

    def test_marlin_has_no_spindle_or_comp_rules(self):
        # A print file has no spindle; nagging about one would be noise.
        text = "G28\nG1 X10 Y10 E5 F1200\nM104 S200\n"
        self.assertEqual(rules(text, dialect="marlin"), [])

    def test_siemens_drops_the_g43_rules(self):
        """Length comp comes from the tool edge, so there is no G43 H to
        forget."""
        self.assertNotIn(d.R_G43_NO_H, d.SIEMENS.rules)
        self.assertNotIn(d.R_NO_TLO_AFTER_M6, d.SIEMENS.rules)

    def test_klartext_is_deliberately_silent(self):
        """Klartext is a different grammar; this tokenizer would read
        garbage into it, so it gets NO squiggles rather than wrong ones."""
        text = ("BEGIN PGM TEST MM\n"
                "TOOL CALL 5 Z S2500\n"
                "L X+30 Y+40 RL F250 M3\n"
                "CYCL DEF 200 DRILLING\n"
                "END PGM TEST MM\n")
        self.assertEqual(rules(text, dialect="klartext"), [])


class TestMalformedInput(unittest.TestCase):
    """Editor buffers are the least trustworthy input this program has: a
    file is linted on every keystroke, so it is seen mid-word, mid-number
    and mid-paste. Nothing in here may raise."""

    def test_absurd_numbers_do_not_lose_the_files_diagnostics(self):
        text = ("T1 M6\n"
                "G" + "9" * 5000 + "\n"        # was ValueError
                "T" + "9" * 400 + "\n"         # was OverflowError
                "G1 X1 F100.\n"
                "M30\n")
        _, found = gp.check_text(text, dialect="fanuc")
        # The real point: the lines AFTER the junk still get checked.
        self.assertIn(d.R_SPINDLE_OFF, [i.rule for _, i in found])

    def test_unreadable_tool_number_falls_back_to_a_readable_message(self):
        text = "T" + "9" * 400 + " M6\nG0 X0\nG43 H1 Z5.\nM3 S1\nG1 Z-1. F10.\nM30"
        _, found = gp.check_text(text, dialect="fanuc")
        msg = next(i.msg for _, i in found if i.rule == d.R_NO_COOLANT)
        self.assertIn("the active tool", msg)
        self.assertNotIn("None", msg)

    def test_junk_lines_never_raise(self):
        junk = [
            "", "   ", "%", "\x00\x01\x02", "G", "X", "....", "G1 X",
            "(unclosed comment", "G1 X5.5.5", "-", "+", "G-", "M",
            "éè 中文 \U0001f600",
            "G1 " + "X" * 10000,
            "(" + "a" * 50000 + ")",
        ]
        parser = gp.GCodeParser("fanuc")
        for line in junk:
            with self.subTest(line=line[:30]):
                parser.check_line(line)   # must not raise

    def test_every_dialect_survives_the_same_junk(self):
        for name in d.DIALECTS:
            parser = gp.GCodeParser(name)
            with self.subTest(dialect=name):
                for line in ("G1 X5", "M6 T1", "\x00", "G" + "1" * 5000):
                    parser.check_line(line)

    def test_unknown_dialect_name_falls_back_to_default(self):
        parser = gp.GCodeParser("no-such-control")
        self.assertEqual(parser.dialect.name, d.DEFAULT_DIALECT)

    def test_pathological_line_is_not_a_regex_bomb(self):
        """The tokenizer runs on every line of multi-megabyte CAM output, so
        its regexes must stay linear. A quadratic one would hang the server
        on a file the user cannot even see is hostile."""
        import time
        parser = gp.GCodeParser("fanuc")
        for line in ("G1 " + "X1." * 20000,
                     "(" + "(" * 20000,
                     "9" * 100000,
                     ";" + "G1 X1 " * 20000):
            start = time.monotonic()
            parser.check_line(line)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 5.0, f"slow on {line[:20]!r}")


if __name__ == "__main__":
    unittest.main()
