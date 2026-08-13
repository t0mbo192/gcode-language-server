"""Golden tests over examples/.

The demo files are documentation: the README quotes their output, and they
are what someone runs first to decide whether this thing works. So their
diagnostics are pinned here exactly — every deliberate mistake still found,
and, just as importantly, nothing else found. A new rule or a widened table
that starts squiggling the demos will fail this file before it reaches
anyone's screen.

Line numbers are 1-based to match the CLI and the editor gutter.
"""

import contextlib
import io
import os
import unittest

from context import EXAMPLES_DIR
from context import dialects as d
from context import gcode_parser as gp

R = d  # shorthand for the rule-id constants below

# path -> (expected dialect, {(1-based line, rule id), ...})
EXPECTED = {
    "demo.nc": ("fanuc", {
        (8, R.R_NO_TLO_AFTER_M6),
        (9, R.R_G43_NO_H),
        (11, R.R_FEED_MISSING),
        (13, R.R_ARC_NO_CENTER),
        (17, R.R_SPINDLE_OFF),
        (18, R.R_UNKNOWN_CODE),
        (19, R.R_COMP_AT_END),
    }),
    "demo_coolant.nc": ("fanuc", {
        (21, R.R_NO_COOLANT),
        (29, R.R_NO_COOLANT),
    }),
    "demo_haas.nc": ("haas", {
        (40, R.R_NO_COOLANT),
    }),
    "demo_heidenhain.i": ("heidenhain", {
        (27, R.R_NO_COOLANT),
    }),
    # Deliberately mute: Klartext is a different grammar, and no squiggles
    # beats wrong squiggles.
    "demo_klartext.h": ("klartext", set()),
    "demo_linuxcnc.ngc": ("linuxcnc", {
        (40, R.R_NO_COOLANT),
    }),
    "demo_marlin.gcode": ("marlin", {
        (9, R.R_FEED_MISSING),
    }),
    "demo_mazak.eia": ("mazak", {
        (21, R.R_NO_COOLANT),
    }),
    "demo_siemens.nc": ("siemens", {
        (6, R.R_FEED_MISSING),
    }),
}


def lint(filename):
    path = os.path.join(EXAMPLES_DIR, filename)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    dialect, found = gp.check_text(text, path=path)
    return dialect, {(n + 1, issue.rule) for n, issue in found}


class TestExamples(unittest.TestCase):

    def test_every_example_is_covered(self):
        """A new demo file must come with its expected output, or this
        suite quietly stops testing it."""
        on_disk = {f for f in os.listdir(EXAMPLES_DIR)
                   if not f.startswith(".")}
        self.assertEqual(on_disk, set(EXPECTED))

    def test_expected_diagnostics(self):
        for filename, (expected_dialect, expected) in EXPECTED.items():
            with self.subTest(filename):
                dialect, found = lint(filename)
                self.assertEqual(dialect, expected_dialect)
                self.assertEqual(found, expected)

    def test_examples_are_utf8_and_parse_from_disk(self):
        for filename in EXPECTED:
            with self.subTest(filename):
                path = os.path.join(EXAMPLES_DIR, filename)
                with open(path, encoding="utf-8") as f:
                    self.assertTrue(f.read().strip())

    def test_cli_exit_codes(self):
        """The CLI is the no-editor proof path and is scriptable: non-zero
        when it found something, zero when it didn't."""
        clean = os.path.join(EXAMPLES_DIR, "demo_klartext.h")
        dirty = os.path.join(EXAMPLES_DIR, "demo.nc")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gp.main(["gcode_parser.py", clean]), 0)
            self.assertEqual(gp.main(["gcode_parser.py", dirty]), 1)

    def test_cli_prints_one_line_per_problem(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gp.main(["gcode_parser.py", os.path.join(EXAMPLES_DIR, "demo.nc")])
        text = out.getvalue()
        self.assertIn("dialect: fanuc", text)
        self.assertIn("7 problem(s) found", text)
        # Line/column are 1-based in the CLI to match the editor gutter.
        self.assertIn("line 8, col 14:", text)

    def test_cli_usage_without_arguments(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gp.main(["gcode_parser.py"]), 2)


if __name__ == "__main__":
    unittest.main()
