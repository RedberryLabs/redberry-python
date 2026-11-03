"""Canonical JSON and deterministic gzip (``@redberrylabs/otel`` SPEC.md §3-§4).

Any change here changes hashes: the golden vectors (``tests/fixtures/redberry_otel_vectors``) pin the
canonical bytes, and a new byte form needs a new ``schema`` value, never an edit to old vectors.

**Canonical JSON** is RFC 8785 (JCS), as ECMAScript writes it: object keys sorted by UTF-16 code units
at every level, no whitespace, numbers in ECMAScript's shortest round-trip form
(``Number.prototype.toString``), strings escaped as ``JSON.stringify`` escapes them. Beyond JCS, for
values OpenTelemetry can hold and JSON cannot: ``NaN`` / ``Infinity`` / ``-Infinity`` are the strings
``"NaN"``, ``"Infinity"``, ``"-Infinity"``, and ``-0`` is ``0``. Python has two things JavaScript has
not, pinned here: a tuple is an array, and an ``int`` outside ±(2^53 - 1) is its decimal string (the
SPEC's rule for a bigint, since a JavaScript number cannot hold it exactly).

**Gzip.** SPEC §4 pins the reference writer's DEFLATE stream to fflate 0.8.3, which Python's zlib does
not reproduce (no zlib level or memory setting gives fflate's bytes). This writer keeps the SPEC's gzip
header exactly (``1f 8b 08 00 00000000 00 03``: no flags, no name, mtime 0, XFL 0, OS 3) and compresses
with zlib at level 6. The stored bytes are deterministic for a given zlib build and decompress to the
canonical JSON with any gunzip. Nothing re-compresses: the hash report commits to the stored bytes as
they are, and SPEC §9's verifier hashes what is stored, so a run stored by this writer verifies exactly
as one stored by ``@redberrylabs/otel``. Its ``producer`` names this package, so a reader can tell which
compressor made the bytes.
"""
from __future__ import annotations

import math
import struct
import zlib
from typing import Any

__all__ = ["GZIP_HEADER", "canonical_bytes", "canonical_json", "deterministic_gzip"]

#: SPEC §4: deflate, no flags, mtime 0, XFL 0, OS 3 (Unix).
GZIP_HEADER = bytes.fromhex("1f8b0800000000000003")
_MAX_SAFE_INTEGER = 2**53 - 1
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def canonical_json(value: Any) -> str:
    """RFC 8785 canonical JSON of ``value``, as ``@redberrylabs/otel``'s ``canonicalJson`` writes it."""
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def canonical_bytes(value: Any) -> bytes:
    """UTF-8 bytes of the canonical JSON."""
    return canonical_json(value).encode("utf-8")


def deterministic_gzip(data: bytes) -> bytes:
    """gzip (RFC 1952) with SPEC §4's header and a zlib level-6 DEFLATE stream (see the module docstring)."""
    c = zlib.compressobj(6, zlib.DEFLATED, -15, 8, zlib.Z_DEFAULT_STRATEGY)
    body = c.compress(data) + c.flush()
    return GZIP_HEADER + body + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)


def _write(v: Any, out: list[str]) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, str):
        out.append(_string(v))
    elif isinstance(v, int):
        out.append(str(v) if -_MAX_SAFE_INTEGER <= v <= _MAX_SAFE_INTEGER else _string(str(v)))
    elif isinstance(v, float):
        if math.isnan(v):
            out.append('"NaN"')
        elif math.isinf(v):
            out.append('"Infinity"' if v > 0 else '"-Infinity"')
        else:
            out.append(_number(v))
    elif isinstance(v, dict):
        out.append("{")
        first = True
        for k in sorted(v, key=_utf16_key):
            if not isinstance(k, str):
                raise TypeError(f"canonical_json: object keys must be strings, not {type(k).__name__}")
            if not first:
                out.append(",")
            first = False
            out.append(_string(k))
            out.append(":")
            _write(v[k], out)
        out.append("}")
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(v, (bytes, bytearray, memoryview)):
        raise TypeError("canonical_json: binary values are not supported")
    else:
        raise TypeError(f"canonical_json: cannot encode a {type(v).__name__}")


def _utf16_key(k: Any) -> bytes:
    # Big-endian UTF-16 bytes compare in the order of their code units (JCS §3.2.3).
    return k.encode("utf-16-be", "surrogatepass") if isinstance(k, str) else b""


def _string(s: str) -> str:
    """``JSON.stringify`` of a string: short escapes, ``\\u00XX`` for other C0 controls, a lone surrogate
    as ``\\udXXX``, everything else (U+007F, U+2028, U+2029 included) literal."""
    parts = ['"']
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        cp = ord(ch)
        if ch in _SHORT_ESCAPES:
            parts.append(_SHORT_ESCAPES[ch])
        elif cp < 0x20:
            parts.append(f"\\u{cp:04x}")
        elif 0xD800 <= cp <= 0xDBFF and i + 1 < n and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
            # A surrogate pair held as two code points is one character to JavaScript.
            parts.append(chr(0x10000 + ((cp - 0xD800) << 10) + (ord(s[i + 1]) - 0xDC00)))
            i += 1
        elif 0xD800 <= cp <= 0xDFFF:
            parts.append(f"\\u{cp:04x}")
        else:
            parts.append(ch)
        i += 1
    parts.append('"')
    return "".join(parts)


def _number(x: float) -> str:
    """ECMAScript ``Number.prototype.toString`` (radix 10) of a finite float.

    ``repr`` gives the shortest digits that round-trip, which are the digits ECMAScript picks; only the
    layout differs (``1e-07`` / ``1e-7``, ``1e+16`` / ``10000000000000000``, ``1.0`` / ``1``)."""
    if x == 0:
        return "0"  # -0 too
    sign = "-" if x < 0 else ""
    r = repr(abs(x))
    mantissa, _, exp = r.partition("e")
    whole, _, frac = mantissa.partition(".")
    raw = whole + frac
    stripped = raw.lstrip("0")
    n = len(whole) + (int(exp) if exp else 0) - (len(raw) - len(stripped))
    digits = stripped.rstrip("0")
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        body = (digits if k == 1 else digits[0] + "." + digits[1:]) + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body
