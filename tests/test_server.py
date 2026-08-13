"""Tests for the LSP glue in server.py.

server.py owns no G-code knowledge, so what is worth testing here is the
translation and the lifecycle: are ranges and severities built correctly,
does a bad message from the client take the server down, does a debounce
timer outlive the document it belongs to.

The client is faked, but the PARAMS are real lsprotocol objects — the point
of this layer is that it speaks the protocol, and a hand-rolled stand-in for
the protocol types would let the tests agree with a mistake.
"""

import unittest

from context import dialects as d

try:
    from lsprotocol import types
    import server as gcode_server
    HAVE_PYGLS = True
except ImportError:                                   # pragma: no cover
    HAVE_PYGLS = False


class FakeDocument:
    """Stands in for pygls' TextDocument: the three attributes we read."""

    def __init__(self, uri, source):
        self.uri = uri
        self.source = source

    @property
    def lines(self):
        return self.source.splitlines(keepends=True)


class FakeWorkspace:
    def __init__(self, docs):
        self._docs = {doc.uri: doc for doc in docs}

    @property
    def text_documents(self):
        return self._docs

    def get_text_document(self, uri):
        return self._docs[uri]      # KeyError for unknown uris, like pygls


class FakeServer:
    """Captures what the real client would have received."""

    def __init__(self, *docs):
        self.workspace = FakeWorkspace(docs)
        self.published = {}
        self.logged = []

    def publish_diagnostics(self, uri, diagnostics):
        self.published[uri] = diagnostics

    def show_message_log(self, message):
        self.logged.append(message)


def ident(uri):
    return types.TextDocumentIdentifier(uri=uri)


@unittest.skipUnless(HAVE_PYGLS, "pygls not installed")
class ServerTestCase(unittest.TestCase):
    """Resets the module-level settings/timers between tests — they are
    process-global in a real server too, so leaking them here would make
    tests order-dependent in exactly the way the real bug would be."""

    def setUp(self):
        gcode_server._settings["dialect"] = "auto"
        for timer in list(gcode_server._timers.values()):
            timer.cancel()
        gcode_server._timers.clear()

    tearDown = setUp


class TestValidate(ServerTestCase):

    def test_publishes_diagnostics_with_positions_and_severity(self):
        doc = FakeDocument("file:///part.nc", "G1 X5. F100.\nM30\n")
        ls = FakeServer(doc)
        gcode_server.validate(ls, doc.uri)

        # Two rules fire on that line: the spindle is stopped AND the tool
        # in the spindle never got coolant. Pick the one under test.
        diags = ls.published[doc.uri]
        self.assertEqual({diag.code for diag in diags},
                         {d.R_SPINDLE_OFF, d.R_NO_COOLANT})
        diag = next(x for x in diags if x.code == d.R_SPINDLE_OFF)
        self.assertEqual(diag.severity, types.DiagnosticSeverity.Warning)
        self.assertEqual(diag.range.start.line, 0)
        self.assertEqual(diag.range.start.character, 0)
        self.assertEqual(diag.range.end.character, 2)

    def test_source_names_the_dialect(self):
        """'Why is this rule firing?' should be answerable from the
        Problems panel alone."""
        doc = FakeDocument("file:///part.eia", "G1 X5. F100.\nM30\n")
        ls = FakeServer(doc)
        gcode_server.validate(ls, doc.uri)
        self.assertEqual(ls.published[doc.uri][0].source, "gcode-ls (mazak)")

    def test_clean_program_publishes_an_empty_list(self):
        """Not 'publishes nothing' — an empty list is what clears stale
        squiggles from a previous edit."""
        doc = FakeDocument("file:///ok.nc", "M3 S1000 M8\nG1 X5. F100.\nM30\n")
        ls = FakeServer(doc)
        gcode_server.validate(ls, doc.uri)
        self.assertEqual(ls.published[doc.uri], [])

    def test_unknown_uri_is_logged_not_raised(self):
        """A lint failure must never kill the server process; the editor
        would just silently lose its language features."""
        ls = FakeServer()
        gcode_server.validate(ls, "file:///gone.nc")
        self.assertEqual(ls.published, {})
        self.assertTrue(ls.logged)

    def test_dialect_comes_from_the_magic_comment(self):
        doc = FakeDocument("file:///part.nc",
                           "(DIALECT: haas)\nG1 X5. F100.\nM30\n")
        ls = FakeServer(doc)
        gcode_server.validate(ls, doc.uri)
        self.assertEqual(ls.published[doc.uri][0].source, "gcode-ls (haas)")

    def test_setting_overrides_the_magic_comment(self):
        gcode_server._settings["dialect"] = "siemens"
        doc = FakeDocument("file:///part.nc",
                           "(DIALECT: haas)\nG1 X5. F100.\nM30\n")
        ls = FakeServer(doc)
        gcode_server.validate(ls, doc.uri)
        self.assertEqual(ls.published[doc.uri][0].source, "gcode-ls (siemens)")


class TestConfiguration(ServerTestCase):
    """didChangeConfiguration carries whatever the client felt like sending."""

    def _push(self, settings):
        ls = FakeServer(FakeDocument("file:///a.nc", "G1 X1 F10.\nM30\n"))
        params = types.DidChangeConfigurationParams(settings=settings)
        gcode_server.on_configuration_change(ls, params)
        return ls

    def test_applies_the_dialect_and_relints_open_files(self):
        ls = self._push({"gcode": {"dialect": "haas"}})
        self.assertEqual(gcode_server._settings["dialect"], "haas")
        self.assertIn("file:///a.nc", ls.published)

    def test_malformed_payloads_do_not_raise(self):
        """`{"gcode": null}` used to raise AttributeError on the second
        .get(), aborting the handler mid-loop and leaving open files with
        stale diagnostics."""
        for settings in ({"gcode": None}, {"gcode": "haas"}, {"gcode": []},
                         {"gcode": {"dialect": None}},
                         {"gcode": {"dialect": 42}}, {}, None, "nonsense"):
            with self.subTest(settings=settings):
                self._push(settings)
                self.assertEqual(gcode_server._settings["dialect"], "auto")

    def test_unknown_dialect_name_is_harmless(self):
        """resolve_dialect() ignores names it doesn't know, so a junk
        setting degrades to the default instead of raising a KeyError deep
        in the hover handler."""
        ls = self._push({"gcode": {"dialect": "brother"}})
        self.assertEqual(ls.published["file:///a.nc"][0].source,
                         "gcode-ls (fanuc)")


class TestDocumentLifecycle(ServerTestCase):

    def test_did_open_lints_immediately(self):
        doc = FakeDocument("file:///a.nc", "G1 X1 F10.\nM30\n")
        ls = FakeServer(doc)
        gcode_server.on_open(
            ls, types.DidOpenTextDocumentParams(
                text_document=types.TextDocumentItem(
                    uri=doc.uri, language_id="gcode", version=1,
                    text=doc.source)))
        self.assertTrue(ls.published[doc.uri])

    def test_did_change_debounces_instead_of_linting_now(self):
        doc = FakeDocument("file:///a.nc", "G1 X1 F10.\nM30\n")
        ls = FakeServer(doc)
        gcode_server._schedule_validate(ls, doc.uri)
        self.assertIn(doc.uri, gcode_server._timers)
        self.assertEqual(ls.published, {})       # nothing yet — that's the point

    def test_keystrokes_replace_the_pending_timer(self):
        doc = FakeDocument("file:///a.nc", "G1 X1 F10.\nM30\n")
        ls = FakeServer(doc)
        gcode_server._schedule_validate(ls, doc.uri)
        first = gcode_server._timers[doc.uri]
        gcode_server._schedule_validate(ls, doc.uri)
        self.assertIsNot(gcode_server._timers[doc.uri], first)
        self.assertTrue(first.finished.is_set(), "old timer was not cancelled")

    def test_close_cancels_the_pending_timer(self):
        """Otherwise a timer armed by the last keystroke before closing
        fires afterwards and re-publishes the squiggles close just
        cleared — for a document the workspace no longer holds."""
        doc = FakeDocument("file:///a.nc", "G1 X1 F10.\nM30\n")
        ls = FakeServer(doc)
        gcode_server._schedule_validate(ls, doc.uri)
        pending = gcode_server._timers[doc.uri]

        gcode_server.on_close(
            ls, types.DidCloseTextDocumentParams(text_document=ident(doc.uri)))

        self.assertTrue(pending.finished.is_set())
        self.assertNotIn(doc.uri, gcode_server._timers)
        self.assertEqual(ls.published[doc.uri], [])

    def test_timers_do_not_accumulate_after_firing(self):
        doc = FakeDocument("file:///a.nc", "G1 X1 F10.\nM30\n")
        ls = FakeServer(doc)
        gcode_server._fire(ls, doc.uri)
        self.assertNotIn(doc.uri, gcode_server._timers)
        self.assertIn(doc.uri, ls.published)


class TestHover(ServerTestCase):

    def _hover(self, source, line, character, uri="file:///a.nc"):
        doc = FakeDocument(uri, source)
        ls = FakeServer(doc)
        return gcode_server.on_hover(
            ls, types.HoverParams(
                text_document=ident(uri),
                position=types.Position(line=line, character=character)))

    def test_g_code_hover_is_dialect_specific(self):
        hover = self._hover("G1 X5.\n", 0, 1)
        self.assertIn("Linear feed move", hover.contents.value)
        self.assertIn("Fanuc", hover.contents.value)

    def test_same_number_gives_different_answers_per_dialect(self):
        """M30 is program end on a mill and 'delete the SD file' on a
        printer — the whole reason for dialect tables."""
        mill = self._hover("M30\n", 0, 1, uri="file:///a.nc")
        printer = self._hover("M30\n", 0, 1, uri="file:///a.gcode")
        self.assertIn("rewind", mill.contents.value)
        self.assertIn("SD card", printer.contents.value)

    def test_parameter_letters_get_the_generic_doc(self):
        hover = self._hover("G1 X5. F250.\n", 0, 8)
        self.assertIn("Feedrate", hover.contents.value)

    def test_hover_range_covers_the_whole_word(self):
        hover = self._hover("G1 X-12.5\n", 0, 5)
        self.assertEqual(hover.range.start.character, 3)
        self.assertEqual(hover.range.end.character, 9)

    def test_unknown_code_has_no_hover(self):
        self.assertIsNone(self._hover("M123\n", 0, 1))

    def test_hovering_whitespace_returns_none(self):
        self.assertIsNone(self._hover("G1   X5.\n", 0, 3))

    def test_position_past_the_end_of_file_returns_none(self):
        self.assertIsNone(self._hover("G1 X5.\n", 99, 0))


class TestCompletion(ServerTestCase):

    def _complete(self, uri):
        doc = FakeDocument(uri, "")
        ls = FakeServer(doc)
        items = gcode_server.on_completion(
            ls, types.CompletionParams(
                text_document=ident(uri),
                position=types.Position(line=0, character=0)))
        return {item.label for item in items}

    def test_offers_the_dialects_codes(self):
        labels = self._complete("file:///a.nc")
        self.assertIn("G0", labels)
        self.assertIn("M6", labels)

    def test_completion_is_dialect_specific(self):
        self.assertIn("M104", self._complete("file:///a.gcode"))   # marlin
        self.assertNotIn("M104", self._complete("file:///a.nc"))   # fanuc

    def test_hidden_codes_are_documented_but_not_offered(self):
        """LinuxCNC's 100 user-defined M1xx slots would bury the real
        codes in the dropdown."""
        labels = self._complete("file:///a.ngc")
        self.assertNotIn("M150", labels)
        self.assertIn("M150", d.LINUXCNC.known_m)   # still hovers and lints
        self.assertIn("M64", labels)

    def test_every_item_carries_markdown_documentation(self):
        doc = FakeDocument("file:///a.nc", "")
        ls = FakeServer(doc)
        items = gcode_server.on_completion(
            ls, types.CompletionParams(
                text_document=ident(doc.uri),
                position=types.Position(line=0, character=0)))
        for item in items:
            self.assertEqual(item.documentation.kind, types.MarkupKind.Markdown)
            self.assertTrue(item.documentation.value.strip())


if __name__ == "__main__":
    unittest.main()
