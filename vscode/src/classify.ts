// Deciding whether text was typed or simply appeared.
//
// This extension cannot see which agent produced an edit, and it does not try.
// Cursor, Windsurf, Trae, Antigravity, Kiro and Qoder are all VS Code forks
// with closed agent internals; the one thing every one of them shares is that
// text ends up in a TextDocument. So the question asked here is narrower and
// answerable:
//
//     did a human's fingers produce these characters, or did they arrive whole?
//
// That is a genuinely different measurement from the Claude Code plugin's,
// which observes tool calls and therefore knows the author. This knows only
// that nobody typed it. Pasting from a browser and an agent writing a file are
// indistinguishable here, and the wording everywhere downstream has to keep
// saying so.
//
// The signal is the shape of a single change event. Typing arrives one
// character at a time because that is how keyboards work. Anything that
// materialises a paragraph in one event was not typed, whatever produced it.

export type Origin = "typed" | "appeared";

export interface Classified {
  origin: Origin;
  linesAdded: number;
  linesRemoved: number;
  chars: number;
}

// A single event inserting at least this much is not someone typing. Both
// thresholds are deliberately generous: over-calling "appeared" is the error
// that accuses an honest student, so the bar sits well above any plausible
// keystroke, IME commit, or emoji.
//
// 120 characters is roughly two full lines of code. A fast typist produces
// about 10 characters per event at most, and VS Code coalesces at the keystroke
// level rather than batching a burst into one change.
const APPEARED_CHARS = 120;

// ...or spans this many newlines. Length alone misses a short multi-line
// snippet, which is exactly the shape of most generated code.
const APPEARED_NEWLINES = 2;

export function countNewlines(text: string): number {
  let n = 0;
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) === 10) n++;
  }
  return n;
}

// Newlines that brought content with them.
//
// The "appeared" test counts newlines, and counting raw ones made the editor
// itself trip it. Pressing Enter between a brace pair inserts "\n    \n" in a
// single change event: two newlines, no code, and the line scored as
// agent-written. Auto-indent, auto-closing brackets and trailing-newline
// fixups all have this shape, so an honest student writing C or JavaScript
// accumulated AI attribution by typing normally. That is the failure this tool
// least gets to have.
//
// A generated block always carries text on its lines. Whitespace-only segments
// are layout the editor produced around the cursor, so they do not count.
export function countContentNewlines(text: string): number {
  const segments = text.split("\n");
  let n = 0;
  // The first segment continues the line the cursor was already on, and the
  // last is whatever trails the final newline, so neither is a line this change
  // brought into being on its own.
  for (let i = 1; i < segments.length; i++) {
    if (segments[i].trim().length > 0) n++;
  }
  return n;
}

// Which line numbers a change lands on, in the document as it now stands.
//
// Scoring has to be per line, not per event. Typing arrives one character at a
// time, so counting events meant a 40-character line scored 41 human lines and
// a student who typed for an hour reported thousands. Recording WHICH lines
// were touched and counting them once is the only version of this that matches
// what `ai_line_changes` and `human_line_changes` are supposed to mean.
export function touchedLines(startLine: number, text: string): number[] {
  const added = countNewlines(text);
  const out: number[] = [];
  for (let i = 0; i <= added; i++) out.push(startLine + i);
  return out;
}

// One VS Code content change, already narrowed to what matters. Kept as a
// plain shape rather than importing vscode's type so this file stays testable
// without the editor host.
export interface RawChange {
  text: string;
  rangeLength: number;
  rangeLines: number; // lines spanned by the replaced range
}

export function classify(change: RawChange): Classified {
  const newlines = countNewlines(change.text);
  const chars = change.text.trim().length;

  // Both tests run on content, not on raw text. `chars` is trimmed for the
  // same reason the newline count is: a change made entirely of whitespace is
  // the editor laying out around the cursor, and nobody authored it.
  const appeared =
    chars >= APPEARED_CHARS || countContentNewlines(change.text) >= APPEARED_NEWLINES;

  return {
    origin: appeared ? "appeared" : "typed",
    linesAdded: newlines,
    linesRemoved: change.rangeLines,
    chars,
  };
}

// Whole-document replacements are their own case and must never be scored.
//
// They happen when a file is reverted, reloaded from disk after an external
// write, or reformatted wholesale. Counting one as "appeared" would credit the
// entire file to an agent every time a formatter ran, which is the single
// easiest way to make this tool produce a number nobody believes.
export function isWholeDocumentReplace(
  change: RawChange,
  documentLength: number,
): boolean {
  return change.rangeLength >= documentLength && documentLength > 0;
}
