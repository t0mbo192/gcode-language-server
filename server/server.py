"""
server.py — the pygls glue between any LSP editor and gcode_parser.py.

There is intentionally no G-code knowledge in this file. Its whole job is
translation:

    editor speaks LSP  <-- JSON-RPC over stdin/stdout -->  this file
    this file calls    GCodeParser.check_line()            (gcode_parser.py)
    and dialect tables                                     (dialects.py)

Message flow when you edit a file in VS Code:

    1. VS Code activates the extension for language "gcode"
    2. extension.ts spawns `python server.py` (this process)
    3. initialize handshake — pygls answers it for us, advertising the
       features we registered below (diagnostics happen via notifications,
       hover + completion via the @server.feature decorators)
    4. textDocument/didOpen arrives with the full file text
       -> we lint it and push textDocument/publishDiagnostics
    5. every keystroke sends textDocument/didChange
       -> we debounce 300 ms, then re-lint (CAM posts can be enormous;
          re-parsing megabytes on every keystroke would burn a core)
    6. hovering a word sends textDocument/hover -> markdown from dialects.py
"""

import re
import threading

from lsprotocol import types
from pygls.server import LanguageServer

from dialects import DIALECTS, WORD_DOCS, resolve_dialect
from gcode_parser import GCodeParser, normalize_code

server = LanguageServer("gcode-ls", "0.1.0")

# The user's gcode.* settings, pushed to us by the client on startup and on
# every settings change (see `synchronize` in extension.ts).
_settings = {"dialect": "auto"}

# One pending re-lint timer per open file (debouncing didChange).
_DEBOUNCE_SECONDS = 0.3
_timers = {}

# Matches one word (letter + number) — used to find what's under the cursor.
_WORD_RE = re.compile(r"(?i)([A-Z])\s*([+-]?(?:\d+\.\d*|\.\d+|\d+))")


# --- linting ----------------------------------------------------------------

def _dialect_for(doc):
    """Dialect priority: user setting > magic comment > extension > fanuc."""
    return resolve_dialect(path=doc.uri, text=doc.source,
                           override=_settings["dialect"])


def validate(ls, uri):
    """Lint one document and push the results to the editor."""
    try:
        doc = ls.workspace.get_text_document(uri)
        dialect = _dialect_for(doc)
        parser = GCodeParser(dialect)

        diagnostics = []
        for n, line in enumerate(doc.lines):
            for issue in parser.check_line(line):
                diagnostics.append(types.Diagnostic(
                    range=types.Range(
                        start=types.Position(line=n, character=issue.col),
                        end=types.Position(line=n, character=issue.end)),
                    message=issue.msg,
                    severity=types.DiagnosticSeverity(issue.severity),
                    code=issue.rule,
                    # Shows up as the diagnostic's origin in the Problems
                    # panel — including the dialect makes "why is this rule
                    # firing?" self-answering.
                    source=f"gcode-ls ({dialect})"))

        ls.publish_diagnostics(uri, diagnostics)
    except Exception as exc:  # never let a lint bug kill the server
        ls.show_message_log(f"gcode-ls validate error: {exc!r}")


def _schedule_validate(ls, uri):
    """Debounce: restart a 300 ms timer on every keystroke; only when the
    typing pauses does the re-lint actually run."""
    old = _timers.pop(uri, None)
    if old:
        old.cancel()
    timer = threading.Timer(_DEBOUNCE_SECONDS, validate, args=(ls, uri))
    _timers[uri] = timer
    timer.start()


# --- document lifecycle -------------------------------------------------------

@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def on_open(ls, params):
    validate(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def on_change(ls, params):
    _schedule_validate(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def on_close(ls, params):
    # Clear our squiggles for closed files; nobody's watching them anymore.
    ls.publish_diagnostics(params.text_document.uri, [])


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def on_configuration_change(ls, params):
    """The client pushes {"gcode": {...}} here at startup and whenever the
    user edits settings — so changing gcode.dialect re-lints live, no
    window reload needed."""
    settings = getattr(params, "settings", None) or {}
    if isinstance(settings, dict):
        _settings["dialect"] = settings.get("gcode", {}).get("dialect", "auto")
    for uri in list(ls.workspace.text_documents):
        validate(ls, uri)


# --- hover -------------------------------------------------------------------

@server.feature(types.TEXT_DOCUMENT_HOVER)
def on_hover(ls, params):
    doc = ls.workspace.get_text_document(params.text_document.uri)
    try:
        line = doc.lines[params.position.line]
    except IndexError:
        return None

    for m in _WORD_RE.finditer(line):
        if not (m.start() <= params.position.character <= m.end()):
            continue
        letter = m.group(1).upper()
        dialect = DIALECTS[_dialect_for(doc)]

        # G/M codes get their dialect-specific doc; parameter letters
        # (X, F, H, ...) get the generic word doc.
        text = None
        if letter in ("G", "M"):
            code = normalize_code(letter, m.group(2))
            table = dialect.known_g if letter == "G" else dialect.known_m
            text = table.get(code)
            if text:
                text += f"\n\n*dialect: {dialect.title}*"
        if text is None:
            text = WORD_DOCS.get(letter)
        if text is None:
            return None

        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown, value=text),
            range=types.Range(
                start=types.Position(line=params.position.line,
                                     character=m.start()),
                end=types.Position(line=params.position.line,
                                   character=m.end())))
    return None


# --- completion ----------------------------------------------------------------

@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=["G", "M", "g", "m"]))
def on_completion(ls, params):
    """Offer every known code of the file's dialect; VS Code does the
    filtering as you type."""
    doc = ls.workspace.get_text_document(params.text_document.uri)
    dialect = DIALECTS[_dialect_for(doc)]

    items = []
    for table in (dialect.known_g, dialect.known_m):
        for code, doc_md in table.items():
            items.append(types.CompletionItem(
                label=code,
                kind=types.CompletionItemKind.Keyword,
                documentation=types.MarkupContent(
                    kind=types.MarkupKind.Markdown, value=doc_md)))
    return items


if __name__ == "__main__":
    # stdin/stdout belong to the LSP transport from here on — that's why
    # debugging on the Python side is done with show_message_log/stderr,
    # never print().
    server.start_io()
