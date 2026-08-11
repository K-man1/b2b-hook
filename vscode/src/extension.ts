// AI Attribution for VS Code and its forks.
//
// One extension covers Cursor, Windsurf, Trae, Antigravity, Kiro, Qoder,
// VSCodium and code-server, because every one of them runs VS Code extensions.
// That is the whole reason this exists rather than a per-agent integration:
// chasing agents is a losing race (there were 29 of them in Hackatime's parser
// the day this was written), while chasing editors is a fixed, small list.
//
// What it measures, precisely: whether text was typed or arrived whole. It
// cannot see which agent wrote anything — these editors keep that internal —
// so it never claims to. `human_line_changes` means keystrokes were observed.
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
import { classify, isWholeDocumentReplace, type RawChange } from "./classify";
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
  ai: number;
  human: number;
  lines: number;
  time: number;
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

function record(doc: vscode.TextDocument, ai: number, human: number) {
  if (!ai && !human) return;
  if (!enabled()) return;

  const project = projectFor(doc);
  if (!project) return;

  const rel = path.relative(project.root, doc.uri.fsPath).replace(/\\/g, "/");
  if (!rel || rel.startsWith("..") || isExcluded(rel)) return;

  const now = Date.now();
  const existing = pending.find(
    (b) => b.project === project.name && b.entity === rel,
  );
  if (existing) {
    existing.ai += ai;
    existing.human += human;
    existing.lines = doc.lineCount;
    existing.time = now;
  } else {
    pending.push({
      project: project.name,
      branch: project.branch,
      entity: rel,
      language: languageFor(rel),
      ai,
      human,
      lines: doc.lineCount,
      time: now,
    });
    if (pending.length > MAX_PENDING) pending = pending.slice(-MAX_PENDING);
  }
  log(`record ${rel} ai=${ai} human=${human}`);
}

function build(b: Bucket): Heartbeat {
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
    ai_line_changes: b.ai,
    human_line_changes: b.human,
  };
  if (b.language) hb.language = b.language;
  if (b.branch) hb.branch = b.branch;
  return hb;
}

async function flush(force = false) {
  if (!enabled()) return;
  const now = Date.now();
  if (!force && now - lastSend < SEND_INTERVAL_MS) return;

  const ready = pending.filter((b) => b.ai > 0);
  const held = pending.filter((b) => !b.ai && now - b.time < HUMAN_ONLY_TTL_MS);
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
  // Undo and redo move text the author already accounted for. Scoring them
  // would let a student inflate either bucket by mashing ctrl-Z.
  if (e.reason === vscode.TextDocumentChangeReason.Undo) return;
  if (e.reason === vscode.TextDocumentChangeReason.Redo) return;

  const docLength = e.document.getText().length;
  let ai = 0;
  let human = 0;

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
    if (!c.linesAdded && !c.chars) continue;
    // Lines, not characters: a heartbeat's unit is lines, and one long line is
    // still one line however it arrived.
    const scored = Math.max(c.linesAdded, c.chars > 0 ? 1 : 0);
    if (c.origin === "appeared") ai += scored;
    else human += scored;
  }

  record(e.document, ai, human);
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
        out.appendLine(`   ${b.entity}  appeared=${b.ai} typed=${b.human}`);
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
