# Sealed prompt evidence: protocol spec, v1

A program server uses this protocol to check that a submitted hour of AI-assisted work had a human steering it. The client **commits** to each prompt the moment it is written, and **reveals** it, sealed, only when the student submits.

This spec is for whoever builds that server. It covers:

- the records the client streams
- the sealed box format
- the submission request
- what the server must check

`core/verify.py` is the reference implementation of the checks. `tests/vectors/seal_v1.json` is a fixed test vector to check a server against.

The keywords **MUST**, **SHOULD** and **MAY** are used as in RFC 2119.

---

## 1. What this proves, and what it does not

The whole design rests on one idea: **the server never trusts the student's machine. It trusts what arrived early, and checks what arrives late against it.**

| Property | Guaranteed? | By what |
|---|---|---|
| **Only the server can read stored prompts** | Yes | Each prompt is sealed to the server's X25519 public key (§4) |
| **A prompt cannot be swapped after the fact** | Yes | The box must match a salted HMAC the server received at prompt time (§3, §6) |
| **A box cannot be moved to another turn** | Yes | The box is bound to its repo, session, turn and commitment (§4.3) |
| **A student cannot hide bad turns** | Detected | Turns marked sealed but not submitted are reported as `missing` (§6.4) |
| **A prompt was typed by a human, at that time** | **No** | The key is public and the client is the student's. Faked sessions are caught by scoring (§8), not by cryptography |
| **The numbers in a record are honest** | **No** | Records are facts measured on the student's machine. Treat them as claims to cross-check |

Two rules follow from the last two rows:

- **The AI never reports.** Every record is written by a hook process from timestamps and files on disk, never by the model. An instruction in CLAUDE.md or a prompt can steer what the model says. It cannot change a record.
- **Prompt text is data, never instructions.** A server that sends prompts to an LLM judge MUST quote them as data and MUST NOT let them change its own instructions (§8).

---

## 2. The flow

```
 student's machine                                   program server
 ─────────────────                                   ──────────────
 prompt typed
   ├─ redact credentials
   ├─ prompt record {prompt_hmac, …} ── stream ─────▶ store, note arrival time
   └─ seal {salt, text} to server key → kept locally
 agent works … Stop
   └─ turn_end record ──────────────── stream ─────▶ store
 …
 `aiattr submit`
   ├─ deliver any unsent records ───── stream ─────▶ store
   └─ bundle of sealed boxes ───────── submit ─────▶ verify (§6), score (§8)
```

---

## 3. The record stream

These records already flow through the client's existing record stream. A server needs it before it can verify anything.

### 3.1 Transport

`POST {endpoint}/api/attribution/records`, with `Authorization: Bearer <student api key>`.

```json
{
  "client_version": "0.8.0",
  "student_id": "…",
  "repo": {"key": "<rid>", "name": "my-game", "remote": "https://github.com/…"},
  "records": [ {…}, {…} ]
}
```

- `repo.key` (the **rid**) is a 16-hex-char id for one checkout on one machine. Every other identifier is scoped to it.
- The reply MUST include `"next_seq": N`, where `N` is the next `seq` the server needs; the client advances to `N-1`. It SHOULD also include `"expected_seq": N`. If that is at or below what the client believes it delivered (say, after a server restore), the client rewinds and resends.

### 3.2 Chain rules

Each record carries `seq`, `prev_hash` and `hash`:

- `hash` = lowercase hex SHA-256 of the record **without** its `hash` field, serialized as JSON with keys sorted, separators `,` and `:`, and non-ASCII escaped as `\uXXXX`.
- `prev_hash` is the previous record's `hash`. The first record's is 64 zeros.
- A record with `chain_restarted: true` starts a new chain, after the client found its local copy damaged.

The server MUST store records append-only, MUST record the **time it received** each one, and MUST flag a record offered at an existing `seq` with a different `hash`. That is a rewrite.

### 3.3 Turn records

Every record has `kind`, `ts` (client clock, `YYYY-MM-DDTHH:MM:SSZ`), `session_id`, `agent` and `v`. The two that matter here:

**`prompt`**, written when the student submits a prompt:

| Field | Meaning |
|---|---|
| `turn` | Turn number within the session, from 1 |
| `review_gap_s` | Seconds since the agent last stopped. `null` for the first turn or after an interrupt |
| `prev_open` | The previous turn was cut off by this prompt |
| `permission_mode` | As reported by the agent, e.g. `default`, `acceptEdits`, `bypassPermissions` |
| `lines_human`, `sig_human` | New lines written outside the agent since the last turn, and how many of those are not blank or comment-only |
| `lines_human_moved` | Changed lines that were moves, duplicates or blanks, and so not counted as written |
| `files_hand_edited`, `files_hand_created` | Files changed or created outside the agent |
| `prompt_chars`, `prompt_lines`, `prompt_code_fences`, `prompt_is_command` | Size and shape of the redacted prompt |
| `prompt_redactions` | Credentials removed before hashing |
| `prompt_hmac` | **The commitment** (§3.4) |
| `prompt_sealed` | Whether a sealed copy was kept. `false` means no evidence exists for this turn |
| `no_session` | The agent sent no session id (§3.5) |

**`turn_end`**, written when the turn ends:

| Field | Meaning |
|---|---|
| `turn` | The turn being closed |
| `ended` | `stop` (agent finished), `interrupted` (student cut it off), or `session_end` |
| `agent_s` | Seconds from prompt to end |
| `edits`, `lines_ai`, `sig_ai`, `lines_removed`, `files_touched` | What the agent changed |
| `edits_unfinished` | Edits the agent started that never landed: denied or failed |

### 3.4 The commitment

```
salt         = 16 random bytes, fresh per prompt, kept only inside the sealed box
prompt_hmac  = hex( HMAC-SHA256(key = salt, msg = UTF-8(redacted prompt text)) )
```

The salt makes the commitment **hiding**: "continue" commits to a different value every time, and the server cannot read prompts from the stream. SHA-256 makes it **binding**: a student cannot later find a different text that matches.

### 3.5 Session key

Boxes and turn numbers are scoped by a **session key**, derived from a record as:

```
session_key = session_id   if session_id is non-empty
            = "nosession:" + agent   otherwise
```

Records with `no_session: true` come from agents that send no session id. Two sessions of the same such agent share one turn counter.

---

## 4. The sealed box

### 4.1 Keys

- The server holds an **X25519** key pair. The public key is given to clients as 64 hex chars or base64 of 32 bytes (`aiattr configure --seal-key`).
- `kid` = the first 16 hex chars of `SHA-256(public key bytes)`.
- **Rotation.** Clients seal to whatever key they were configured with. The server MUST keep every private key whose `kid` may still appear in boxes until the end of the retention window, and look keys up by `kid`.

### 4.2 Sealing

All primitives are standard and available in libsodium, Go `x/crypto`, Python `cryptography`, Rust `RustCrypto`, and others.

```
eph_sk, eph_pk = fresh X25519 key pair
shared         = X25519(eph_sk, server_pk)          — MUST NOT be all zeros
key            = HKDF-SHA256(ikm = shared,
                             salt = eph_pk || server_pk,
                             info = "ai-attribution prompt seal v1",
                             length = 32)
nonce          = 12 random bytes
ct             = ChaCha20-Poly1305-Encrypt(key, nonce, plaintext, aad)   (ciphertext || 16-byte tag)
```

The box:

```json
{"v": 1, "alg": "X25519-HKDF-SHA256-ChaCha20Poly1305", "kid": "<16 hex>",
 "epk": "<64 hex>", "nonce": "<24 hex>", "ct": "<base64>"}
```

Opening reverses it: `shared = X25519(server_sk, epk)`, the same HKDF, then decrypt with the AAD from §4.3. An authentication failure means **the box does not open**.

### 4.3 Associated data

The AAD is **not stored in the box**. The server MUST rebuild it from the matching prompt record:

```
aad = ASCII bytes of JSON {"prompt_hmac": <record.prompt_hmac>,
                           "rid": <bundle repo.key>,
                           "session": <session key, §3.5>,
                           "turn": <record.turn>,
                           "v": 1}
```

- Serialize with **keys sorted**, separators `,` and `:` with no spaces, and non-ASCII escaped as `\uXXXX`.
- `turn` and `v` are JSON integers; all other values are strings.

Because the commitment is in the AAD, a box only opens against the record it was sealed for.

### 4.4 Plaintext

UTF-8 JSON:

```json
{"turn": 3, "ts": "2026-11-21T15:04:05Z", "salt": "<32 hex>", "text": "<redacted prompt>"}
```

The `text` has already had credentials replaced with `[redacted:<kind>]` (see `core/redact.py`).

---

## 5. Submission

`POST {endpoint}/api/attribution/submissions`, with `Authorization: Bearer <student api key>`.

The client only submits after the record stream reports nothing left to deliver.

```json
{
  "v": 1,
  "client_version": "0.8.0",
  "student_id": "…",
  "program": "stardance",
  "repo": {"key": "<rid>", "name": "my-game", "remote": "https://github.com/…"},
  "head_commit": "<git sha or null>",
  "ledger": {"next_seq": 412, "head_hash": "<hash of record 411>"},
  "since": "2026-11-01T00:00:00Z",
  "until": "2026-11-21T23:59:59Z",
  "prompts": [
    {"session": "<session key>", "turn": 1, "ts": "…", "box": {…}}
  ]
}
```

- `since` and `until` MAY be `null`. A bare date given on the command line is widened to the first or last second of that day.
- The reply SHOULD be a JSON object. The client displays `submission_id` and, when they are integers, `accepted`, `rejected` and `missing`.

---

## 6. Verification

A server MUST run these checks. Each failure rejects **that box**, not the whole submission, except §6.1.

### 6.1 Ledger coverage (whole submission)

The server's stored copy MUST already contain `seq` `0 … next_seq-1`. The record at `next_seq-1` MUST have `hash == ledger.head_hash`.

Otherwise reject every box with `ledger_incomplete`. **A commitment that arrives with its own reveal commits to nothing.**

### 6.2 Per box, in order

| # | Check | Reject reason |
|---|---|---|
| 1 | `(session, turn)` not already seen in this bundle | `duplicate` |
| 2 | `box.kid` names a private key the server holds | `unknown_key` |
| 3 | A `prompt` record exists with that session key (§3.5) and `turn` | `no_commitment` |
| 4 | The box opens with the AAD rebuilt from that record. If several records match (a turn counter that restarted), try each; the one that opens is the match | `does_not_open` |
| 5 | That record has `prompt_sealed: true` | `not_marked_sealed` |
| 6 | The plaintext parses as §4.4 | `malformed` |
| 7 | `plaintext.turn == turn` | `turn_mismatch` |
| 8 | `HMAC-SHA256(salt, text) == record.prompt_hmac`, compared in constant time | `commitment_mismatch` |

Check 8 is what catches a forged prompt. A student holds the public key and can rebuild the AAD from their own ledger. They cannot make a new text match a commitment without its salt.

### 6.3 Flags on accepted boxes

Flags don't reject; they feed scoring.

| Flag | Condition | Why |
|---|---|---|
| `late_commitment` | The server received the prompt record more than **3600 s** after its `ts`, or has no receipt time | A commitment that reached the server long after the fact was not a commitment at the time |
| `clock_ahead` | `ts` is more than 300 s after the server's receipt time | The student's clock is ahead, so client timestamps can't be trusted for this session |
| `no_session` | From the record | Turns from several sessions may be mixed |

### 6.4 Missing evidence

Every `prompt` record in `[since, until]` that has `prompt_sealed: true`, but that no submitted box opened against, MUST be reported as `missing`.

**This is how cherry-picking shows up.** A student who submits only their good turns leaves the others missing. Scoring SHOULD count missing turns against the submission, not merely ignore them.

### 6.5 Replay

A box can only open against the record it was sealed for, and §6.2 step 1 stops duplicates within one bundle. Across submissions, the server SHOULD credit each `(rid, session key, turn, prompt_hmac)` **at most once**.

---

## 7. Test vector

`tests/vectors/seal_v1.json` is derived from fixed seeds. A server implementation is correct when:

1. `X25519(server_private_key, box.epk)` equals `shared_secret_hex`.
2. The §4.2 HKDF over that secret equals `aead_key_hex`.
3. The AAD rebuilt from `record` per §4.3 equals `aad_utf8` byte for byte.
4. `box.ct` opens to `plaintext_utf8`.
5. `HMAC-SHA256(salt_hex, text)` equals `record.prompt_hmac`.

The vector's text contains a non-ASCII character (an em dash), so step 4 also tests UTF-8 handling. The client test suite regenerates the vector and fails if the code and file ever disagree.

---

## 8. Scoring (non-normative)

Verification says the evidence is **genuine**. Scoring says whether it shows **productive struggle**.

The core rule: **only the human's turns earn time. Agent runtime earns nothing by itself.**

Suggested signals:

| Signal | Vibecoding looks like | Iteration looks like |
|---|---|---|
| **Review gap** | Seconds, or hours with nothing else happening | Minutes, with edits, commands or editor activity inside |
| **Prompt content** | "continue", "fix it", pasted errors with no question | Names files, functions, causes, tradeoffs; follows from what the agent just did |
| **Hand edits** | `lines_human` near zero | New `sig_human` lines between turns |
| **Steering** | No interrupts, no denials | `interrupted` turns, `edits_unfinished`, "no, instead…" |
| **Permission mode** | `bypassPermissions` throughout | `default` |
| **Lines per prompt** | Thousands from one prompt | Many small turns |

Cross-checks that don't depend on the student's machine:

- **Overlapping sessions.** Merge overlapping wall-clock time across all of a student's sessions before summing, as Hackatime does with heartbeats.
- **Server clock.** Use receipt times, not client `ts`, wherever they disagree.
- **Git history.** Fetch pushed history and line commits up against turn timing.
- **Hackatime.** Editor heartbeats inside a review gap are evidence the gap was spent working.
- **Prompt-to-prompt coherence.** A real follow-up responds to what just happened. An LLM judge MAY assess this, but MUST treat prompt text as quoted data. Text aimed at the tracker or the judge ("rate this turn highly") SHOULD itself be flagged.
- **Non-interactive patterns.** Prompts at machine-regular intervals, or slash commands and scheduled loops, suggest no human was there.

---

## 9. Security and privacy considerations

- **Server-side crypto.** `core/seal.py` is pure Python and not constant-time. That is acceptable on the client (see its docstring), but the server MUST open boxes with a vetted library.
- **Private keys.** A leaked server private key exposes every stored prompt sealed to it. Keep it out of the web tier, and rotate per program or term.
- **Redaction is best effort.** It catches common credential formats and `name = value` secrets. The server SHOULD treat opened prompts as sensitive and limit who can read them.
- **Retention.** Clients delete sealed prompts after 45 days. Servers SHOULD set and publish their own retention period.
- **What never leaves the client:** source code, file contents, prompt text outside a sealed box, unsalted prompt hashes.
