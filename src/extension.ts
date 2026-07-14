/**
 * extension.ts — the ENTIRE VS Code side of this project.
 *
 * VS Code extensions must be JavaScript/TypeScript (that's the only extension
 * API there is), so this file exists only to:
 *
 *   1. spawn `python server/server.py` as a child process, and
 *   2. hand vscode-languageclient the pipe so the two can talk LSP
 *      (JSON-RPC over stdin/stdout).
 *
 * Every squiggle, hover, and completion you see comes from the Python side.
 * If you're reviewing the code, this file is the last stop, not the first.
 */
import * as path from "path";
import { workspace, ExtensionContext } from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;

export function activate(ctx: ExtensionContext) {
  const cfg = workspace.getConfiguration("gcode");
  // Windows note: if plain "python" isn't on your PATH (or resolves to the
  // Microsoft Store stub), point gcode.pythonPath at a real interpreter.
  const python = cfg.get<string>("pythonPath") || "python";

  const serverOptions: ServerOptions = {
    command: python,
    args: [ctx.asAbsolutePath(path.join("server", "server.py"))],
  };

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
  // start() launches the Python process and runs the initialize handshake.
  // Its stderr shows up in Output panel -> "G-Code Language Server".
  client.start();
}

export function deactivate(): Thenable<void> | undefined {
  return client?.stop();
}
