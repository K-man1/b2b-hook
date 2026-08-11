// Which project a file belongs to, in wakatime-cli's detector order.
//
// Deliberately identical to core/repoutil.py in the Claude Code plugin, and to
// wakatime-cli itself. A student running this extension in Cursor and the
// Claude Code plugin in a terminal is one student doing one project; if the two
// resolved the name differently, Hackatime would file the work under two
// projects and neither total would mean anything.
//
//   1. .wakatime-project   nearest one walking up. Line 1 names the project,
//                          line 2 names the branch. Checked FIRST, before git,
//                          because it exists to override what git would say.
//   2. git work tree       named for its folder.
//   3. the folder itself   last resort.
//
// Step 3 is refused for container directories. That is the one deliberate
// divergence from wakatime-cli, carried over from the Python side: they record
// that a folder existed, this reads file contents, and a home directory is not
// a project anybody meant to track.

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

const PROJECT_FILE = ".wakatime-project";

const CONTAINER_DIRS = new Set([
  "Desktop", "Documents", "Downloads", "Music", "Pictures", "Movies",
  "Public", "Library", "Applications", "Sites", "src", "code", "projects",
  "repos", "dev", "workspace",
]);

export interface Project {
  root: string;
  name: string;
  branch?: string;
}

function isContainer(dir: string): boolean {
  const resolved = path.resolve(dir);
  if (path.dirname(resolved) === resolved) return true; // filesystem root
  const home = path.resolve(os.homedir());
  if (resolved === home) return true;
  const parent = path.dirname(resolved);
  const bases = [home, path.sep, "/Users", "/home", "/tmp"];
  return (
    bases.some((b) => parent === path.resolve(b)) &&
    CONTAINER_DIRS.has(path.basename(resolved))
  );
}

function walkUp(from: string, test: (dir: string) => boolean): string | null {
  let dir = path.resolve(from);
  for (;;) {
    if (test(dir)) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

// Line 1 is the project, line 2 is the branch. Only two are read because only
// two are defined; anything after them is somebody's note to themselves.
function markerLines(dir: string): { name?: string; branch?: string } {
  try {
    const raw = fs.readFileSync(path.join(dir, PROJECT_FILE), "utf8");
    const [first = "", second = ""] = raw.split(/\r?\n/);
    return { name: first.trim() || undefined, branch: second.trim() || undefined };
  } catch {
    return {};
  }
}

export function detect(startDir: string): Project | null {
  const marker = walkUp(startDir, (d) =>
    fs.existsSync(path.join(d, PROJECT_FILE)),
  );
  if (marker) {
    const { name, branch } = markerLines(marker);
    return { root: marker, name: name ?? path.basename(marker), branch };
  }

  const gitRoot = walkUp(startDir, (d) => fs.existsSync(path.join(d, ".git")));
  if (gitRoot) return { root: gitRoot, name: path.basename(gitRoot) };

  const resolved = path.resolve(startDir);
  if (isContainer(resolved)) return null;
  return { root: resolved, name: path.basename(resolved) };
}

// Paths never counted, matching core/counting.py's EXCLUDE_GLOBS in spirit: a
// single generated lockfile write is tens of thousands of lines and would
// swamp every real signal in the project.
const EXCLUDED = [
  /(^|\/)node_modules\//, /(^|\/)vendor\//, /(^|\/)dist\//, /(^|\/)build\//,
  /(^|\/)target\//, /(^|\/)\.venv\//, /(^|\/)venv\//, /(^|\/)__pycache__\//,
  /(^|\/)\.git\//, /(^|\/)\.next\//, /(^|\/)\.claude\//,
  /\.min\.(js|css)$/, /\.map$/, /\.lock$/,
  /(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|go\.sum)$/,
];

export function isExcluded(relPath: string): boolean {
  const p = relPath.replace(/\\/g, "/");
  return EXCLUDED.some((re) => re.test(p));
}
