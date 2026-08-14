"""Tests for the dialect data: detection, table invariants, and the
M-code coverage each control is supposed to have.

Most of this file is INVARIANTS rather than examples. dialects.py is a pile
of hand-maintained tables, and the realistic failure mode is not "someone
rewrote the engine", it is "someone pasted a code in the wrong format at
2am" — a key of "M08" instead of "M8" silently never matches anything,
because lookups go through normalize_code(). Invariant tests catch that
class of mistake for every future edit, including the ones nobody thought
to write a test for.
"""

import json
import os
import unittest

from context import REPO_ROOT
from context import dialects as d
from context import gcode_parser as gp


class TestDialectResolution(unittest.TestCase):
    """Priority: setting > magic comment > extension > fanuc."""

    def test_default_is_fanuc(self):
        self.assertEqual(d.resolve_dialect(), "fanuc")
        self.assertEqual(d.resolve_dialect(path="part.nc"), "fanuc")

    def test_extension_map(self):
        for path, expected in (("a.mpf", "siemens"), ("a.spf", "siemens"),
                               ("a.ngc", "linuxcnc"), ("a.gcode", "marlin"),
                               ("a.gc", "marlin"), ("a.min", "okuma"),
                               ("a.eia", "mazak"), ("a.i", "heidenhain"),
                               ("a.h", "klartext"), ("a.hnc", "klartext")):
            self.assertEqual(d.resolve_dialect(path=path), expected, path)

    def test_extension_is_case_insensitive(self):
        self.assertEqual(d.resolve_dialect(path="PART.EIA"), "mazak")

    def test_works_on_file_uris(self):
        """The server passes doc.uri straight in, not a filesystem path."""
        uri = "file:///c%3A/Users/tom/parts/bracket.ngc"
        self.assertEqual(d.resolve_dialect(path=uri), "linuxcnc")

    def test_magic_comment_beats_extension(self):
        for text in ("(DIALECT: siemens)", ";DIALECT=siemens",
                     "(dialect:SIEMENS)", "% (DIALECT : siemens)"):
            self.assertEqual(
                d.resolve_dialect(path="a.eia", text=text), "siemens", text)

    def test_magic_comment_only_near_the_top(self):
        text = "\n" * 10 + "(DIALECT: siemens)"
        self.assertEqual(d.resolve_dialect(path="a.eia", text=text), "mazak")

    def test_unknown_magic_name_is_ignored(self):
        self.assertEqual(
            d.resolve_dialect(path="a.eia", text="(DIALECT: brother)"),
            "mazak")

    def test_setting_beats_everything(self):
        self.assertEqual(
            d.resolve_dialect(path="a.eia", text="(DIALECT: siemens)",
                              override="haas"), "haas")

    def test_auto_and_junk_overrides_fall_through(self):
        for override in ("auto", "", None, "not-a-control"):
            self.assertEqual(
                d.resolve_dialect(path="a.eia", override=override), "mazak",
                repr(override))


class TestTableInvariants(unittest.TestCase):
    """Rules that must hold for every dialect, now and after every edit."""

    def test_keys_are_in_normalized_form(self):
        """A key of "M08" or "G01" would never match a lookup, and would
        fail silently — no hover, plus a bogus unknown-code squiggle."""
        for name, dialect in d.DIALECTS.items():
            for table in (dialect.known_g, dialect.known_m):
                for code in table:
                    with self.subTest(dialect=name, code=code):
                        self.assertEqual(
                            gp.normalize_code(code[0], code[1:]), code)

    def test_g_table_holds_only_g_codes_and_m_table_only_m(self):
        for name, dialect in d.DIALECTS.items():
            for code in dialect.known_g:
                self.assertTrue(code.startswith("G"), f"{name}: {code}")
            for code in dialect.known_m:
                self.assertTrue(code.startswith("M"), f"{name}: {code}")

    def test_docs_are_non_empty_markdown(self):
        for name, dialect in d.DIALECTS.items():
            for table in (dialect.known_g, dialect.known_m):
                for code, doc in table.items():
                    with self.subTest(dialect=name, code=code):
                        self.assertTrue(doc.strip(), "empty doc")
                        # Every hover opens with the bolded code itself.
                        self.assertTrue(doc.startswith(f"**{code} "),
                                        f"{code}: {doc[:40]!r}")

    def test_state_codes_are_documented(self):
        """Any code the engine treats as coolant/spindle must also be in the
        hover table, or the lint calls its own state code 'unknown'."""
        for name, dialect in d.DIALECTS.items():
            if not dialect.known_m:
                continue          # klartext is deliberately empty
            for label, codes in (("coolant_on", dialect.coolant_on),
                                 ("coolant_off", dialect.coolant_off),
                                 ("spindle_on", dialect.spindle_on)):
                for code in codes:
                    with self.subTest(dialect=name, set=label, code=code):
                        self.assertIn(code, dialect.known_m)

    def test_cycle_codes_are_documented(self):
        for name, dialect in d.DIALECTS.items():
            if not dialect.known_g:
                continue
            for code in dialect.cycle_codes:
                with self.subTest(dialect=name, code=code):
                    self.assertIn(code, dialect.known_g)

    def test_coolant_on_and_off_do_not_overlap(self):
        """A code in both sets would make the engine's branch order decide
        the meaning — always a bug, never intent."""
        for name, dialect in d.DIALECTS.items():
            self.assertFalse(dialect.coolant_on & dialect.coolant_off, name)

    def test_rules_are_known_rule_ids(self):
        for name, dialect in d.DIALECTS.items():
            self.assertTrue(dialect.rules <= d.ALL_RULES,
                            f"{name} enables an unknown rule")

    def test_hidden_completions_are_real_codes(self):
        for name, dialect in d.DIALECTS.items():
            known = set(dialect.known_g) | set(dialect.known_m)
            self.assertTrue(dialect.completion_hidden <= known, name)

    def test_dialect_names_match_their_keys(self):
        for key, dialect in d.DIALECTS.items():
            self.assertEqual(key, dialect.name)

    def test_extended_address_is_opt_in(self):
        """Only SINUMERIK has the `M2=3` form. Every other dialect must
        leave the set empty, or its tokenizing changes silently."""
        self.assertEqual(d.SIEMENS.extended_address, frozenset({"M", "S"}))
        for name, dialect in d.DIALECTS.items():
            if name != "siemens":
                self.assertEqual(dialect.extended_address, frozenset(), name)

    def test_extended_address_letters_are_single_upper_case(self):
        for name, dialect in d.DIALECTS.items():
            for letter in dialect.extended_address:
                with self.subTest(dialect=name, letter=letter):
                    self.assertTrue(letter.isupper() and len(letter) == 1)

    def test_tool_def_codes_only_where_they_apply(self):
        """tool_def_codes is only meaningful when the T word is the tool
        change; setting one without the other is a silent no-op."""
        for name, dialect in d.DIALECTS.items():
            if dialect.tool_def_codes:
                self.assertTrue(dialect.tool_change_on_t, name)


class TestPackageJsonAgreement(unittest.TestCase):
    """dialects.py and package.json describe the same product to two
    different audiences. They drift, and a user notices before we do."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "package.json"),
                  encoding="utf-8") as f:
            cls.pkg = json.load(f)

    def _setting(self, key):
        return self.pkg["contributes"]["configuration"]["properties"][key]

    def test_dialect_setting_lists_every_dialect(self):
        enum = set(self._setting("gcode.dialect")["enum"])
        self.assertEqual(enum, set(d.DIALECTS) | {"auto"})

    def test_claimed_extensions_cover_the_detection_map(self):
        """Every extension the server maps to a dialect should also be an
        extension VS Code hands us — except '.h', which is deliberately
        left unclaimed so we don't hijack every C header."""
        claimed = set(self.pkg["contributes"]["languages"][0]["extensions"])
        mapped = set(d.EXTENSION_DIALECTS)
        self.assertEqual(mapped - claimed, {".h"})

    def test_extension_map_points_at_real_dialects(self):
        for ext, name in d.EXTENSION_DIALECTS.items():
            self.assertIn(name, d.DIALECTS, ext)

    def test_python_path_setting_is_machine_scoped(self):
        """Security: this value is spawned as a process. Machine scope is
        what stops a workspace's .vscode/settings.json from pointing it at
        an arbitrary executable that runs when the folder is opened."""
        self.assertEqual(self._setting("gcode.pythonPath").get("scope"),
                         "machine")


class TestMCodeCoverage(unittest.TestCase):
    """Coverage the dialects are documented as having. These are the
    regression tests for the M-code expansion — a future table cleanup that
    drops one of these is a user-visible loss."""

    def test_shared_codes_everywhere(self):
        """The Fanuc-family controls all share the basic set."""
        for name in ("fanuc", "linuxcnc", "siemens", "okuma", "mazak", "haas"):
            table = d.DIALECTS[name].known_m
            for code in ("M0", "M1", "M3", "M4", "M5", "M6", "M19", "M30"):
                self.assertIn(code, table, f"{name} is missing {code}")

    def test_fanuc_builder_assigned_codes(self):
        for code in ("M10", "M11", "M29", "M41", "M42", "M48", "M49", "M60",
                     "M198"):
            self.assertIn(code, d.FANUC.known_m, code)

    def test_linuxcnc_rs274ngc_set(self):
        for code in ("M48", "M49", "M50", "M51", "M52", "M53", "M60", "M61",
                     "M62", "M63", "M64", "M65", "M66", "M67", "M68",
                     "M70", "M71", "M72", "M73"):
            self.assertIn(code, d.LINUXCNC.known_m, code)

    def test_linuxcnc_user_defined_block(self):
        """M100–M199 are documented (so the lint stays quiet) but hidden
        from completion (so they don't bury M1, M2 and M30)."""
        for code in ("M100", "M101", "M150", "M199"):
            self.assertIn(code, d.LINUXCNC.known_m, code)
            self.assertIn(code, d.LINUXCNC.completion_hidden, code)
        self.assertNotIn("M200", d.LINUXCNC.known_m)
        self.assertEqual(len(d.LINUXCNC.completion_hidden), 100)
        # The real codes stay offerable.
        for code in ("M2", "M30", "M64"):
            self.assertNotIn(code, d.LINUXCNC.completion_hidden, code)

    def test_linuxcnc_m51_is_not_coolant(self):
        """The collision the per-dialect coolant sets exist for: M51 is a
        spindle-override switch here, through-spindle coolant on a Mazak."""
        self.assertNotIn("M51", d.LINUXCNC.coolant_on)
        self.assertIn("M51", d.MAZAK.coolant_on)

    def test_siemens_predefined_set(self):
        for code in ("M17", "M19", "M40", "M41", "M42", "M43", "M44", "M45",
                     "M70"):
            self.assertIn(code, d.SIEMENS.known_m, code)

    def test_siemens_has_no_fanuc_subprogram_codes(self):
        """Siemens calls subprograms by name and returns with M17/RET, so
        an M98 in a Siemens file is a mis-posted program — worth the
        info-level note rather than a silent pass."""
        self.assertNotIn("M98", d.SIEMENS.known_m)
        self.assertNotIn("M99", d.SIEMENS.known_m)

    def test_marlin_covers_the_printer_families(self):
        for code in ("M17", "M20", "M24", "M73", "M80", "M92", "M105",
                     "M112", "M115", "M117", "M119", "M155", "M201", "M204",
                     "M205", "M220", "M221", "M290", "M301", "M400", "M420",
                     "M500", "M851", "M900", "M906", "M999"):
            self.assertIn(code, d.MARLIN.known_m, code)

    def test_marlin_laser_and_router_codes(self):
        """The same firmware runs engravers and small routers; these were
        already in Marlin's inherited state sets, so leaving them out of the
        table made the lint call its own state codes unknown."""
        for code in ("M3", "M4", "M5", "M7", "M8", "M9"):
            self.assertIn(code, d.MARLIN.known_m, code)

    def test_dialect_traps_are_documented_as_such(self):
        """Same number, different planet. If these hovers ever stop
        disagreeing with each other, a table got copy-pasted."""
        self.assertIn("SD card", d.MARLIN.known_m["M30"])
        self.assertIn("rewind", d.FANUC.known_m["M30"])
        self.assertIn("rigid", d.FANUC.known_m["M29"].lower())
        self.assertIn("SD", d.MARLIN.known_m["M29"])
        self.assertIn("brake", d.FANUC.known_m["M13"])
        self.assertIn("coolant", d.HEIDENHAIN.known_m["M13"].lower())

    def test_okuma_stays_conservative(self):
        """Deliberately the thinnest table in the file — OSP M-codes are
        per-machine and published lists disagree. If this ever grows, it
        should be from a real machine's list, not from guessing."""
        self.assertIn("CALL", d.OKUMA.known_m["M98"])

    def test_klartext_is_empty_on_purpose(self):
        self.assertEqual(d.KLARTEXT.known_g, {})
        self.assertEqual(d.KLARTEXT.known_m, {})
        self.assertEqual(d.KLARTEXT.rules, frozenset())


if __name__ == "__main__":
    unittest.main()
