/**
 * extension.ts — the ENTIRE VS Code side of this project.
 *
 * VS Code extensions must be JavaScript/TypeScript (that's the only extension
 * API there is), so this file exists only to:
 *
 *   1. figure out how to launch the language server (bundled exe or Python),
 *   2. spawn it as a child process, and
 *   3. hand vscode-languageclient the pipe so the two can talk LSP
 *      (JSON-RPC over stdin/stdout).
 *
 * Every squiggle, hover, and completion you see comes from the Python side.
 * If you're reviewing the code, this file is the last stop, not the first.
 */
import * as fs from "fs";
import * as path from "path";
import { commands, window, workspace, ExtensionContext } from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;

/**
 * How to launch the server — three possibilities, in priority order:
 *
 * 1. `gcode.pythonPath` explicitly set (anything other than the default
 *    "python") → run `<that interpreter> server/server.py`. This is the
 *    developer escape hatch: point it at the venv where you're hacking on
 *    the Python side and the bundled exe is deliberately ignored.
 *
 * 2. A bundled standalone executable at server/bin/gcode-ls(.exe), produced
 *    by `npm run bundle-server` (PyInstaller) and shipped inside the
 *    platform-specific .vsix. This is the normal end-user path: no Python
 *    install required at all.
 *
 * 3. Plain `python server/server.py` — the universal .vsix has no exe
 *    inside, so it lands here and needs Python 3 + pygls on the machine.
 *
 * `mode` is carried along so the failure message can say something true
 * about what actually failed instead of guessing.
 */
function pickLaunch(ctx: ExtensionContext): {
  options: ServerOptions;
  mode: "override" | "bundled" | "python";
} {
  const cfg = workspace.getConfiguration("gcode");
  const python = cfg.get<string>("pythonPath") || "python";
  const script = ctx.asAbsolutePath(path.join("server", "server.py"));

  if (python !== "python") {
    return { options: { command: python, args: [script] }, mode: "override" };
  }

  const exeName = process.platform === "win32" ? "gcode-ls.exe" : "gcode-ls";
  const bundled = ctx.asAbsolutePath(path.join("server", "bin", exeName));
  if (fs.existsSync(bundled)) {
    return { options: { command: bundled, args: [] }, mode: "bundled" };
  }

  return { options: { command: python, args: [script] }, mode: "python" };
}

export function activate(ctx: ExtensionContext) {
  const { options: serverOptions, mode } = pickLaunch(ctx);

  const clientOptions: LanguageClientOptions = {
    // Only attach to files VS Code has classified as language "gcode".
    // Which files those are is decided by the "languages" block in
    // package.json — extensions, O-number filenames, first-line '%'.
    documentSelector: [{ scheme: "file", language: "gcode" }],
    // Push the "gcode.*" settings to the server on startup and whenever
    // they change, as workspace/didChangeConfiguration notifications.
    synchronize: { configurationSection: "gcode" },
  };

  client = new LanguageClient(
    "gcodeLS",
    "G-Code Language Server",
    serverOptions,
    clientOptions
  );

  // start() launches the server process and runs the initialize handshake.
  // Its stderr shows up in Output panel -> "G-Code Language Server".
  // If the process can't spawn at all (no Python, Store-stub python.exe,
  // missing pygls), start() rejects — turn that into an actionable
  // notification instead of a silent Output-panel death.
  client.start().catch(() => {
    const openSettings = "Open Settings";
    const message =
      mode === "bundled"
        ? "G-Code Language Server: the bundled server failed to start. " +
          "See Output → \"G-Code Language Server\" for details."
        : "G-Code Language Server failed to start. This build needs " +
          "Python 3 with pygls installed: pip install \"pygls>=1.3,<2.0\". " +
          "If python isn't on PATH (or is the Microsoft Store stub), point " +
          "the gcode.pythonPath setting at a real interpreter.";
    window.showErrorMessage(message, openSettings).then((choice) => {
      if (choice === openSettings) {
        commands.executeCommand(
          "workbench.action.openSettings",
          "gcode.pythonPath"
        );
      }
    });
  });
}

export function deactivate(): Thenable<void> | undefined {
  return client?.stop();
}
