// AI Attribution for VS Code and its forks.
//
// NOT an agent hook, and for most agents a hook is the better tool. A hook runs
// inside the agent and knows the author outright; core/adapters.py generates one
// for Cursor, Windsurf, Antigravity, Kiro, Qoder, Codex, Gemini CLI, Qwen Code,
// Copilot, Cline, Devin and Goose. If the agent is on that list, use the hook.
//
// This exists for the two things a hook structurally cannot do:
//
//   1. Pasted code. No agent acted, so no hook fires. A student copying AI
//      output from a browser is invisible to every hook and visible here.
//   2. Agents with no hook surface at all — Trae, Roo Code, Cody.
//
// What it measures is therefore narrower: whether text was typed or arrived
// whole. It cannot see which agent wrote anything and never claims to. `human_line_changes` means keystrokes were observed.
// `ai_line_changes` means lines appeared without them, which covers an agent
// writing a file AND a paste from a browser, and the two are not
// distinguishable from here. Say that plainly anywhere the number is shown.
//
// Three rules, inherited from the Claude Code plugin:
//   1. Never interrupt the editor. Every failure is swallowed.
//   2. Never send source text. Counts, paths and a project name only.
//   3. Never invent activity. A heartbeat is only ever emitted for a moment at
//      which an edit actually landed.

import * as path from "path";
import * as vscode from "vscode";
import {
  classify, isWholeDocumentReplace, touchedLines, type Origin, type RawChange,
} from "./classify";
import {
  CATEGORY, MAX_BATCH, apiKey, apiUrl, languageFor, send, type Heartbeat,
} from "./hackatime";
import { detect, isExcluded, type Project } from "./project";

const EDITOR_FALLBACK = "vscode";
const PLUGIN_UA = () =>
  `${editorName()}/${vscode.version} ai-attribution-wakatime/0.1.0`;

// Matched to Hackatime's own 2-minute heartbeat timeout. Time is the sum of
// gaps between heartbeats, each capped there, so sending more often buys no
// accuracy and only costs requests.
const SEND_INTERVAL_MS = 120_000;

// A bucket holding only typed lines is never sent: the editor's own WakaTime
// extension already reported that work as `coding`, and a second row would be
// the same minutes counted twice. It is held rather than dropped so that an
// agent edit landing on the same file can merge and carry the human count
// along as context.
const HUMAN_ONLY_TTL_MS = 900_000;

const MAX_PENDING = 500;

interface Bucket {
  project: string;
  branch?: string;
  entity: string;
  language?: string;
  // Line number -> who last touched it. A map rather than two counters because
  // the unit being reported is lines: counting change events instead made a
  // 40-character typed line score 41 human lines, since each keystroke is its
  // own event. Last writer wins on a line an agent wrote and a human then
  // edited, matching provenance.retag in the Python plugin.
  //
  // Line numbers shift when text is inserted above them, so a bucket held open
  // across large edits is approximate. It is bounded by the flush interval and
  // is a far smaller error than counting keystrokes.
  origins: Map<number, Origin>;
  lines: number;
  time: number;
}

function countOrigins(b: Bucket): { ai: number; human: number } {
  let ai = 0;
  let human = 0;
  for (const origin of b.origins.values()) {
    if (origin === "appeared") ai++;
    else human++;
  }
  return { ai, human };
}

let out: vscode.OutputChannel;
let pending: Bucket[] = [];
let lastSend = 0;
let timer: NodeJS.Timeout | undefined;

function cfg() {
  return vscode.workspace.getConfiguration("aiAttribution");
}

function log(msg: string) {
  if (cfg().get<boolean>("debug")) out.appendLine(msg);
}

// Cursor and friends report themselves through appName. Hackatime's user-agent
// parser already knows these names, so passing the real one through is what
// makes a Cursor heartbeat say "cursor" rather than pretending to be vscode.
function editorName(): string {
  const name = (vscode.env.appName || "").toLowerCase();
  for (const known of [
    "cursor", "windsurf", "trae", "antigravity", "kiro", "qoder",
    "vscodium", "positron", "code-server",
  ]) {
    if (name.includes(known)) return known;
  }
  return EDITOR_FALLBACK;
}

function enabled(): boolean {
  return !!cfg().get<boolean>("enabled") && !!apiKey(cfg().get<string>("apiKey") ?? "");
}

function projectFor(doc: vscode.TextDocument): Project | null {
  if (doc.uri.scheme !== "file") return null;
  const folder = vscode.workspace.getWorkspaceFolder(doc.uri);
  return detect(folder ? folder.uri.fsPath : path.dirname(doc.uri.fsPath));
}

function record(doc: vscode.TextDocument, touched: Map<number, Origin>) {
  if (!touched.size) return;
  if (!enabled()) return;

  const project = projectFor(doc);
  if (!project) return;

  const rel = path.relative(project.root, doc.uri.fsPath).replace(/\\/g, "/");
  if (!rel || rel.startsWith("..") || isExcluded(rel)) return;

  const now = Date.now();
  let bucket = pending.find(
    (b) => b.project === project.name && b.entity === rel,
  );
  if (bucket) {
    for (const [line, origin] of touched) bucket.origins.set(line, origin);
    bucket.lines = doc.lineCount;
    bucket.time = now;
  } else {
    bucket = {
      project: project.name,
      branch: project.branch,
      entity: rel,
      language: languageFor(rel),
      origins: new Map(touched),
      lines: doc.lineCount,
      time: now,
    };
    pending.push(bucket);
    if (pending.length > MAX_PENDING) pending = pending.slice(-MAX_PENDING);
  }
  // The bucket we just wrote, not a fresh lookup. Re-finding it by `entity`
  // alone matched the first bucket with that relative path in ANY project, so
  // two projects with a src/index.ts logged each other's counts. Keeping the
  // reference also drops a non-null assertion that was load-bearing for no
  // reason.
  const n = countOrigins(bucket);
  log(`record ${rel} appeared=${n.ai} typed=${n.human}`);
}

// Drop any record of the lines an undo or redo moved. Only touches a bucket
// that is already open for this file: if there is none, there is nothing to
// correct.
function forget(
  doc: vscode.TextDocument,
  changes: readonly vscode.TextDocumentContentChangeEvent[],
) {
  const project = projectFor(doc);
  if (!project) return;
  const rel = path.relative(project.root, doc.uri.fsPath).replace(/\\/g, "/");
  const bucket = pending.find(
    (b) => b.project === project.name && b.entity === rel,
  );
  if (!bucket) return;
  for (const change of changes) {
    for (const line of touchedLines(change.range.start.line, change.text)) {
      bucket.origins.delete(line);
    }
  }
}

function build(b: Bucket): Heartbeat {
  const { ai, human } = countOrigins(b);
  const hb: Heartbeat = {
    entity: b.entity,
    type: "file",
    project: b.project,
    time: b.time / 1000,
    is_write: true,
    category: CATEGORY,
    editor: editorName(),
    plugin: PLUGIN_UA(),
    lines: b.lines,
    ai_line_changes: ai,
    human_line_changes: human,
  };
  if (b.language) hb.language = b.language;
  if (b.branch) hb.branch = b.branch;
  return hb;
}

async function flush(force = false) {
  if (!enabled()) return;
  const now = Date.now();
  if (!force && now - lastSend < SEND_INTERVAL_MS) return;

  const ready = pending.filter((b) => countOrigins(b).ai > 0);
  const held = pending.filter(
    (b) => countOrigins(b).ai === 0 && now - b.time < HUMAN_ONLY_TTL_MS,
  );
  if (!ready.length) {
    pending = held;
    return;
  }

  const batch = ready.slice(0, MAX_BATCH);
  pending = ready.slice(MAX_BATCH).concat(held);
  lastSend = now;

  try {
    await send(
      apiUrl(cfg().get<string>("apiUrl") ?? ""),
      apiKey(cfg().get<string>("apiKey") ?? ""),
      batch.map(build),
      PLUGIN_UA(),
    );
    log(`delivered ${batch.length} heartbeat(s)`);
  } catch (err) {
    // Offline, key rotated, Hackatime down: all non-events. Put the batch back
    // and let the next tick try. lastSend deliberately stays where the success
    // path put it, so an offline machine backs off instead of retrying hard.
    pending = batch.concat(pending).slice(-MAX_PENDING);
    log(`delivery failed, ${batch.length} held: ${String(err)}`);
  }
}

function onChange(e: vscode.TextDocumentChangeEvent) {
  if (!enabled()) return;
  if (e.document.uri.scheme !== "file") return;
  // Undo and redo move text the author already accounted for, so neither is
  // scored. But returning outright left the undone lines sitting in the open
  // bucket with their original origin: an agent wrote a block, the student
  // undid it, and the lines were still reported as agent-written because the
  // bucket was never told. Forget them instead of ignoring the event, so the
  // undone lines are attributed to whoever writes them next.
  if (
    e.reason === vscode.TextDocumentChangeReason.Undo ||
    e.reason === vscode.TextDocumentChangeReason.Redo
  ) {
    forget(e.document, e.contentChanges);
    return;
  }

  const docLength = e.document.getText().length;
  const touched = new Map<number, Origin>();

  for (const change of e.contentChanges) {
    const raw: RawChange = {
      text: change.text,
      rangeLength: change.rangeLength,
      rangeLines: change.range.end.line - change.range.start.line,
    };
    // A reload from disk or a full reformat replaces everything. Counting one
    // would credit the whole file to an agent every time a formatter ran.
    if (isWholeDocumentReplace(raw, docLength)) continue;

    const c = classify(raw);
    if (!c.chars && !c.linesRemoved) continue;
    for (const line of touchedLines(change.range.start.line, change.text)) {
      touched.set(line, c.origin);
    }
  }

  record(e.document, touched);
}

export function activate(context: vscode.ExtensionContext) {
  out = vscode.window.createOutputChannel("AI Attribution");
  context.subscriptions.push(out);

  context.subscriptions.push(
    vscode.workspace.onDidChangeTextDocument((e) => {
      try {
        onChange(e);
      } catch (err) {
        log(`onChange failed: ${String(err)}`);
      }
    }),
  );

  // Saving is the moment a student would expect their work to count, so it is
  // worth a flush attempt; the interval gate still applies.
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(() => void flush()),
  );

  timer = setInterval(() => void flush(), SEND_INTERVAL_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  context.subscriptions.push(
    vscode.commands.registerCommand("aiAttribution.showStatus", () => {
      const key = apiKey(cfg().get<string>("apiKey") ?? "");
      const doc = vscode.window.activeTextEditor?.document;
      const project = doc ? projectFor(doc) : null;
      out.show(true);
      out.appendLine("--- AI Attribution ---");
      out.appendLine(`editor      : ${editorName()} (${vscode.env.appName})`);
      out.appendLine(`enabled     : ${cfg().get<boolean>("enabled")}`);
      out.appendLine(
        `hackatime   : ${key ? `key ending ${key.slice(-4)}` : "NO KEY (checked settings and ~/.wakatime.cfg)"}`,
      );
      out.appendLine(`endpoint    : ${apiUrl(cfg().get<string>("apiUrl") ?? "")}`);
      out.appendLine(
        `project     : ${project ? `${project.name} (${project.root})` : "none for the active file"}`,
      );
      out.appendLine(`pending     : ${pending.length} bucket(s)`);
      for (const b of pending.slice(0, 10)) {
        const n = countOrigins(b);
        out.appendLine(`   ${b.entity}  appeared=${n.ai} typed=${n.human}`);
      }
    }),
  );

  log(`activated in ${vscode.env.appName} as "${editorName()}"`);
}

export function deactivate() {
  // Best effort: the host does not wait on this, but an in-flight flush that
  // completes is a heartbeat that would otherwise be lost.
  void flush(true);
}
