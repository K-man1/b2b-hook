// Plain assertions, run with `node out/test.js`. No test framework, matching
// the Python side: this has to run anywhere the extension does.
//
// Every case here is a shape a real editor actually produces. The classifier is
// the only place this extension can be wrong in a way that accuses somebody, so
// the cases that matter most are the ones that must NOT read as "appeared".

import {
  classify, isWholeDocumentReplace, touchedLines, type RawChange,
} from "./classify";
import { detect, isExcluded } from "./project";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

let pass = 0;
const fail: string[] = [];

function check(name: string, cond: boolean, detail = "") {
  if (cond) {
    pass++;
    console.log(`  ok   ${name}`);
  } else {
    fail.push(name);
    console.log(`  FAIL ${name}${detail ? "  " + detail : ""}`);
  }
}

function ch(text: string, rangeLength = 0, rangeLines = 0): RawChange {
  return { text, rangeLength, rangeLines };
}

console.log("classifying typed input (must never read as appeared)");
check("a single character is typed", classify(ch("a")).origin === "typed");
check("pressing Enter is typed", classify(ch("\n")).origin === "typed");
check("Enter plus auto-indent is typed", classify(ch("\n    ")).origin === "typed");
check("a bracket pair is typed", classify(ch("()")).origin === "typed");
check(
  "an autocompleted identifier is typed",
  classify(ch("getUserAccountById")).origin === "typed",
);
check(
  "an emoji is typed",
  classify(ch("\u{1F600}")).origin === "typed",
);
check(
  "a whole typed line is still typed",
  classify(ch("const total = items.reduce((a, b) => a + b, 0);")).origin === "typed",
);

console.log("classifying text that appeared");
const snippet = "function solve() {\n  const h = [];\n  return h;\n}";
check("a short multi-line snippet appeared", classify(ch(snippet)).origin === "appeared");
check(
  "a long single line appeared",
  classify(ch("x".repeat(200))).origin === "appeared",
);
const generated = Array.from({ length: 40 }, (_, i) => `line${i} = ${i}`).join("\n");
const g = classify(ch(generated));
check("a generated block appeared", g.origin === "appeared");
check("and its added lines are counted", g.linesAdded === 39, `got ${g.linesAdded}`);

console.log("the boundary");
check(
  "one newline alone is not enough to call it appeared",
  classify(ch("if (x) {\n")).origin === "typed",
);
check(
  "two newlines is",
  classify(ch("if (x) {\n  y();\n")).origin === "appeared",
);

console.log("changes that must not be scored at all");
check(
  "a full-document replace is recognised",
  isWholeDocumentReplace(ch("new content", 500), 500),
);
check(
  "a normal edit is not",
  !isWholeDocumentReplace(ch("hi", 2), 500),
);
check(
  "an empty document is not treated as a replace",
  !isWholeDocumentReplace(ch("", 0), 0),
);

console.log("deletions");
const del = classify(ch("", 200, 8));
check("a deletion adds no lines", del.linesAdded === 0);
check("and reports what it removed", del.linesRemoved === 8);

console.log("project detection matches wakatime-cli order");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "aiattr-"));
const plain = path.join(tmp, "just-a-folder");
fs.mkdirSync(plain);
check(
  "a bare folder is a project named for itself",
  detect(plain)?.name === "just-a-folder",
);

const marked = path.join(tmp, "marked");
fs.mkdirSync(marked);
fs.writeFileSync(path.join(marked, ".wakatime-project"), "green-monkeys\nmy-branch\n");
const m = detect(marked);
check("a .wakatime-project names the project", m?.name === "green-monkeys");
check("line 2 sets the branch", m?.branch === "my-branch");

const deep = path.join(marked, "src", "deep");
fs.mkdirSync(deep, { recursive: true });
check("the marker is found walking up", detect(deep)?.root === marked);

const repo = path.join(tmp, "my-repo");
fs.mkdirSync(path.join(repo, ".git"), { recursive: true });
check("a git repo is named for its folder", detect(repo)?.name === "my-repo");
fs.writeFileSync(path.join(repo, ".wakatime-project"), "chosen\n");
check("the marker beats git", detect(repo)?.name === "chosen");

check("a home directory is not a project", detect(os.homedir()) === null);
check(
  "nor is a container inside it",
  detect(path.join(os.homedir(), "Downloads")) === null,
);

console.log("exclusions");
check("node_modules is excluded", isExcluded("node_modules/react/index.js"));
check("nested node_modules too", isExcluded("web/node_modules/x.js"));
check("lockfiles are excluded", isExcluded("package-lock.json"));
check("minified output is excluded", isExcluded("dist/app.min.js"));
check("real source is not", !isExcluded("src/index.ts"));

// --- per-line scoring -------------------------------------------------------
//
// Regression: scoring counted change EVENTS. Typing arrives one character per
// event, so a 40-character line reported 41 human lines and an hour of typing
// reported thousands. What follows simulates the real event stream.

console.log("scoring counts lines, not keystrokes");

function typeOut(text: string, startLine = 0): Map<number, string> {
  const touched = new Map<number, string>();
  let line = startLine;
  for (const chunk of text) {
    const c = classify(ch(chunk));
    for (const l of touchedLines(line, chunk)) touched.set(l, c.origin);
    if (chunk === "\n") line++;
  }
  return touched;
}

function tally(m: Map<number, string>) {
  let ai = 0, human = 0;
  for (const o of m.values()) o === "appeared" ? ai++ : human++;
  return { ai, human };
}

const sdlkfh = tally(typeOut("sdlkfh"));
check(
  'typing "sdlkfh" is 1 human line, not 6',
  sdlkfh.human === 1 && sdlkfh.ai === 0,
  JSON.stringify(sdlkfh),
);

const oneLine = tally(typeOut("const total = items.reduce((a, b) => a + b, 0);"));
check(
  "typing a 40-char line is 1 human line, not 41",
  oneLine.human === 1,
  JSON.stringify(oneLine),
);

const threeLines = tally(typeOut("function f() {\n  return 1;\n}"));
check(
  "typing three lines is 3 human lines",
  threeLines.human === 3 && threeLines.ai === 0,
  JSON.stringify(threeLines),
);

// An agent writing a block, then the student fixing one line inside it.
const mixed = new Map<number, string>();
const block = classify(ch("a();\nb();\nc();\nd();"));
for (const l of touchedLines(10, "a();\nb();\nc();\nd();")) mixed.set(l, block.origin);
for (const [l, o] of typeOut("x", 11)) mixed.set(l, o);
const mixedTally = tally(mixed);
check(
  "editing one line of an agent block moves only that line",
  mixedTally.ai === 3 && mixedTally.human === 1,
  JSON.stringify(mixedTally),
);

fs.rmSync(tmp, { recursive: true, force: true });

console.log(`\n${pass} passed, ${fail.length} failed`);
if (fail.length) process.exit(1);
