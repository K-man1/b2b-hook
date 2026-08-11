// Delivery to Hackatime, mirroring the Claude Code plugin's core/heartbeat.py.
//
// Same schema, same category, same reasoning. If the two producers disagreed
// about any of it, a student running both would have their work counted twice
// or filed under two names.

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export const DEFAULT_API_URL = "https://hackatime.hackclub.com/api/hackatime/v1";

// NOT "coding". Hackatime's stats API takes `no_ai_coding=true`, which drops
// this category from time totals. Heartbeats from here therefore carry the
// line split without ever adding to payable hours — which matters because the
// editor is already sending its own `coding` heartbeats for the same minutes,
// and a second row claiming that time would be double-counting.
export const CATEGORY = "ai coding";

export const MAX_BATCH = 100;

export interface Heartbeat {
  entity: string;
  type: "file";
  project: string;
  time: number;
  is_write: boolean;
  category: string;
  editor: string;
  plugin: string;
  lines?: number;
  language?: string;
  branch?: string;
  ai_line_changes: number;
  human_line_changes: number;
}

// Parsed by hand rather than with an ini library: editor plugins write this
// file with duplicate keys and stray whitespace, and a strict parser throws on
// exactly the files most likely to be in the wild.
export function wakatimeSettings(): Record<string, string> {
  const home = process.env.WAKATIME_HOME || os.homedir();
  const out: Record<string, string> = {};
  let section = "";
  let raw: string;
  try {
    raw = fs.readFileSync(path.join(home, ".wakatime.cfg"), "utf8");
  } catch {
    return out;
  }
  for (const line of raw.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#") || t.startsWith(";")) continue;
    if (t.startsWith("[") && t.endsWith("]")) {
      section = t.slice(1, -1).trim().toLowerCase();
      continue;
    }
    if (section !== "settings") continue;
    const eq = t.indexOf("=");
    if (eq < 0) continue;
    out[t.slice(0, eq).trim().toLowerCase()] = t.slice(eq + 1).trim();
  }
  return out;
}

// The student's own key, read locally and sent only to the service it belongs
// to. It is never forwarded anywhere else, which is what separates using a
// credential from exfiltrating one.
export function apiKey(configured: string): string {
  return (configured || wakatimeSettings()["api_key"] || "").trim();
}

// wakatime.cfg's api_url is written by whichever setup script ran last and
// turns up with and without the version suffix, so normalise rather than
// appending blindly and 404ing half the time.
export function apiUrl(configured: string): string {
  let url = (configured || wakatimeSettings()["api_url"] || DEFAULT_API_URL)
    .trim()
    .replace(/\/+$/, "");
  if (url.endsWith("/heartbeats")) url = url.slice(0, -"/heartbeats".length);
  if (url.endsWith("/users/current")) {
    url = url.slice(0, -"/users/current".length);
  }
  return url;
}

// Rails wraps a top-level JSON array into params[:_json], which is the shape
// their bulk endpoint reads, so the body is a bare array.
export async function send(
  base: string,
  key: string,
  batch: Heartbeat[],
  pluginUa: string,
): Promise<number> {
  const res = await fetch(`${base}/users/current/heartbeats.bulk`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
      "User-Agent": pluginUa,
    },
    body: JSON.stringify(batch),
    signal: AbortSignal.timeout(15000),
  });
  if (!res.ok) throw new Error(`hackatime returned ${res.status}`);
  return res.status;
}

const LANGUAGES: Record<string, string> = {
  ".c": "C", ".cc": "C++", ".cpp": "C++", ".cs": "C#", ".css": "CSS",
  ".go": "Go", ".h": "C", ".hpp": "C++", ".html": "HTML", ".java": "Java",
  ".js": "JavaScript", ".json": "JSON", ".jsx": "JavaScript", ".kt": "Kotlin",
  ".lua": "Lua", ".md": "Markdown", ".php": "PHP", ".py": "Python",
  ".rb": "Ruby", ".rs": "Rust", ".scss": "SCSS", ".sh": "Bash",
  ".sql": "SQL", ".swift": "Swift", ".ts": "TypeScript", ".tsx": "TypeScript",
  ".vue": "Vue", ".yaml": "YAML", ".yml": "YAML",
};

export function languageFor(file: string): string | undefined {
  return LANGUAGES[path.extname(file).toLowerCase()];
}
