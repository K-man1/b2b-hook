"""Seal prompt text so only the server can read it.

WHAT SEALING DOES, AND WHAT IT DOES NOT

It gives confidentiality. A sealed prompt can be opened only with the
server's private key, so the plaintext never sits on the student's disk, a
laptop backup, or a shared family computer.

It does NOT give authenticity. The server's key is public by design, so the
student can seal any text they like. What stops a swapped or invented prompt
is the commitment already in the ledger: every prompt record carries
`prompt_hmac`, an HMAC of the text under a random salt, and that record
streams to the server within seconds of the prompt. The salt travels inside
the sealed box. On opening, the server recomputes the HMAC and compares it
with the record it received at prompt time. A text sealed later that does not
match is caught; one that matches is the text that was committed to at the
time. That is commit-then-reveal, and it is the whole integrity story.

THE FORMAT

Standard parts only, so a server in any language opens it with ordinary
library calls (X25519, HKDF-SHA256, ChaCha20-Poly1305 are all in libsodium,
Go's x/crypto, Python's `cryptography`, WebCrypto-adjacent libraries, ...):

    eph_sk, eph_pk  = fresh X25519 key pair, one per sealed message
    shared          = X25519(eph_sk, server_pk)            reject all-zero
    key             = HKDF-SHA256(ikm=shared, salt=eph_pk || server_pk,
                                  info=INFO, length=32)
    ct              = ChaCha20-Poly1305(key, nonce, plaintext, aad)

    {"v": 1, "alg": ALG, "kid": kid(server_pk), "epk": hex,
     "nonce": hex, "ct": base64(ciphertext || tag)}

`aad` is supplied by the caller and is not stored in the box: it binds the box
to its context (repo, session, turn), so a sealed prompt copied onto a
different turn fails to open. The server rebuilds it from the ledger record.

WHY PURE PYTHON

The plugin is stdlib-only, because it has to install on a ten-year-old Windows
laptop with nothing but Python on it, and the stdlib has none of these
primitives. So they are implemented here, from RFC 7748 and RFC 8439, and the
test suite checks them against the `cryptography` package wherever it is
installed. Two consequences, stated plainly:

  - Not constant-time. The only secret on the client is the ephemeral key,
    used once, on the student's own machine; the party who could time it is
    the student, who already knows the plaintext. The server must open boxes
    with a real library, never with `open_box` below, which exists for tests
    and as a readable reference.
  - Slow next to C, but a prompt is a few kilobytes: one X25519 and a
    ChaCha20 pass take milliseconds, once per prompt.
"""

import base64
import hashlib
import hmac
import json
import os
import struct

VERSION = 1
ALG = "X25519-HKDF-SHA256-ChaCha20Poly1305"
INFO = b"ai-attribution prompt seal v1"


class SealError(Exception):
    pass


# --- X25519 (RFC 7748) -----------------------------------------------------

_P = 2 ** 255 - 19
_A24 = 121665


def _clamp(k):
    k = bytearray(k)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(bytes(k), "little")


def x25519(scalar, u):
    """Scalar multiplication on Curve25519, Montgomery ladder per RFC 7748."""
    if len(scalar) != 32 or len(u) != 32:
        raise SealError("x25519 inputs must be 32 bytes")
    k = _clamp(scalar)
    ub = bytearray(u)
    ub[31] &= 127
    x1 = int.from_bytes(bytes(ub), "little") % _P
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) * (da + cb) % _P
        z3 = x1 * (da - cb) * (da - cb) % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, x3, z2, z3 = x3, x2, z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


_BASE = (9).to_bytes(32, "little")


def public_key(private):
    return x25519(private, _BASE)


def generate_keypair():
    """(private, public), 32 bytes each. For the server and for tests."""
    sk = os.urandom(32)
    return sk, public_key(sk)


# --- ChaCha20-Poly1305 (RFC 8439) ------------------------------------------

def _rotl(v, n):
    return ((v << n) & 0xFFFFFFFF) | (v >> (32 - n))


def _chacha_block(key, counter, nonce):
    s = list(struct.unpack("<4I", b"expand 32-byte k")
             + struct.unpack("<8I", key)
             + (counter,)
             + struct.unpack("<3I", nonce))
    w = list(s)

    def qr(a, b, c, d):
        w[a] = (w[a] + w[b]) & 0xFFFFFFFF
        w[d] = _rotl(w[d] ^ w[a], 16)
        w[c] = (w[c] + w[d]) & 0xFFFFFFFF
        w[b] = _rotl(w[b] ^ w[c], 12)
        w[a] = (w[a] + w[b]) & 0xFFFFFFFF
        w[d] = _rotl(w[d] ^ w[a], 8)
        w[c] = (w[c] + w[d]) & 0xFFFFFFFF
        w[b] = _rotl(w[b] ^ w[c], 7)

    for _ in range(10):
        qr(0, 4, 8, 12)
        qr(1, 5, 9, 13)
        qr(2, 6, 10, 14)
        qr(3, 7, 11, 15)
        qr(0, 5, 10, 15)
        qr(1, 6, 11, 12)
        qr(2, 7, 8, 13)
        qr(3, 4, 9, 14)
    return struct.pack("<16I", *((w[i] + s[i]) & 0xFFFFFFFF for i in range(16)))


def chacha20(key, counter, nonce, data):
    out = bytearray(len(data))
    for block in range(0, len(data), 64):
        stream = _chacha_block(key, counter + block // 64, nonce)
        chunk = data[block:block + 64]
        for i, byte in enumerate(chunk):
            out[block + i] = byte ^ stream[i]
    return bytes(out)


def poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:32], "little")
    p = (1 << 130) - 5
    acc = 0
    for i in range(0, len(msg), 16):
        n = int.from_bytes(msg[i:i + 16] + b"\x01", "little")
        acc = (acc + n) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(n):
    return b"\x00" * ((16 - n % 16) % 16)


def _mac_data(aad, ct):
    return (aad + _pad16(len(aad)) + ct + _pad16(len(ct))
            + struct.pack("<QQ", len(aad), len(ct)))


def aead_encrypt(key, nonce, plaintext, aad=b""):
    otk = _chacha_block(key, 0, nonce)[:32]
    ct = chacha20(key, 1, nonce, plaintext)
    return ct + poly1305(otk, _mac_data(aad, ct))


def aead_decrypt(key, nonce, sealed, aad=b""):
    if len(sealed) < 16:
        raise SealError("ciphertext too short")
    ct, tag = sealed[:-16], sealed[-16:]
    otk = _chacha_block(key, 0, nonce)[:32]
    if not hmac.compare_digest(poly1305(otk, _mac_data(aad, ct)), tag):
        raise SealError("authentication failed")
    return chacha20(key, 1, nonce, ct)


# --- HKDF-SHA256 (RFC 5869) ------------------------------------------------

def hkdf_sha256(ikm, salt, info, length=32):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block = b"", b""
    for i in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
    return out[:length]


# --- the sealed box ---------------------------------------------------------

def kid(server_pk):
    """Short id for a server key, so a box says which key opens it."""
    return hashlib.sha256(server_pk).hexdigest()[:16]


def parse_public_key(text):
    """A server public key as configured: 64 hex chars or base64 of 32 bytes."""
    text = (text or "").strip()
    try:
        raw = bytes.fromhex(text) if len(text) == 64 else base64.b64decode(
            text, validate=True)
    except ValueError:
        raise SealError("public key is neither hex nor base64")
    if len(raw) != 32:
        raise SealError("public key must be 32 bytes")
    if raw == bytes(32):
        raise SealError("public key is all zeros")
    return raw


def _derive(shared, eph_pk, server_pk):
    if shared == bytes(32):
        # A low-order server key produces an all-zero secret that anyone can
        # compute. Refuse rather than seal to a key that protects nothing.
        raise SealError("degenerate shared secret")
    return hkdf_sha256(shared, eph_pk + server_pk, INFO)


def canonical_aad(context):
    """Associated data from a context dict: sorted, compact, ASCII-only JSON.

    Every byte is specified, because a server in another language has to
    rebuild it exactly: keys sorted, no whitespace, non-ASCII escaped as
    \\uXXXX. See docs/evidence-spec.md.
    """
    return json.dumps(context or {}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def seal(server_pk, plaintext, aad=b"", _eph_sk=None, _nonce=None):
    """Encrypt `plaintext` (bytes) so only the holder of server_sk can open it.

    `_eph_sk` and `_nonce` exist only to produce the fixed test vector in
    tests/vectors/seal_v1.json. Reusing either in real use would be a serious
    mistake, which is why nothing outside the tests passes them.
    """
    eph_sk = _eph_sk or os.urandom(32)
    eph_pk = public_key(eph_sk)
    key = _derive(x25519(eph_sk, server_pk), eph_pk, server_pk)
    nonce = _nonce or os.urandom(12)
    return {
        "v": VERSION,
        "alg": ALG,
        "kid": kid(server_pk),
        "epk": eph_pk.hex(),
        "nonce": nonce.hex(),
        "ct": base64.b64encode(aead_encrypt(key, nonce, plaintext, aad)).decode(),
    }


def open_box(server_sk, box, aad=b""):
    """Reference opener. Tests use it; a server should use a real library."""
    if box.get("v") != VERSION or box.get("alg") != ALG:
        raise SealError("unknown box format")
    server_pk = public_key(server_sk)
    if box.get("kid") != kid(server_pk):
        raise SealError("box was sealed to a different key")
    eph_pk = bytes.fromhex(box["epk"])
    key = _derive(x25519(server_sk, eph_pk), eph_pk, server_pk)
    return aead_decrypt(key, bytes.fromhex(box["nonce"]),
                        base64.b64decode(box["ct"]), aad)
