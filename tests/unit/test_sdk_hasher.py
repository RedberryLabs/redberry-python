"""redberry_sdk.hasher: the stored document, its bytes, the signed report and the stores.

The golden vectors in ``tests/fixtures/redberry_otel_vectors`` are copied unchanged from
``@redberrylabs/otel`` (``test/vectors`` at 2890614): the Python writer must produce their canonical
JSON byte for byte. Their gzip bytes come from fflate, which zlib does not reproduce; the stored bytes
here are checked for the SPEC's header, determinism and round trip instead (see ``_canonical``).

The VALP side of the report is re-implemented below from ``valp_collector.hashes`` (signature check and
body validation), not imported, so the test pins the wire contract rather than VALP's code.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cryptography")

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from redberry_sdk.hasher import (
    AzureBlobStore,
    ClientAssertionCredential,
    ManagedIdentityCredential,
    PutMeta,
    RedberryStore,
    StoreConflictError,
    build_envelope,
    canonical_json,
    credential_from_env,
    deterministic_gzip,
    encode_trace,
    key_id_of,
    load_signer,
    report_body,
    report_bytes,
    send_report,
    storage_path,
    store_from_env,
    upload_sample,
    verify,
)
from redberry_sdk.hasher._canonical import GZIP_HEADER, _number
from redberry_sdk.hasher._http import Request, Response
from redberry_sdk.hasher._report import Sample

VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "redberry_otel_vectors"
SIG = json.loads((VECTORS / "signature.json").read_text())
AGENT = "AGT-01K5V8QJ3M7XW2ZB4YRNCDEFGH"
TRACE = "45e3cc173b7aae7a6e658021710f8848"
TOKEN = "ab" * 32
NO_SLEEP: tuple[float, ...] = (0, 0)


def _vectors() -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(VECTORS.glob("v1-*.json"))]


def _envelope(v: dict[str, Any]) -> dict[str, Any]:
    i = v["input"]
    return build_envelope(agent_id=i["agentId"], trace_id=i["traceId"], finish=i["finish"], spans=i["spans"],
                          resource=i["resource"], cut_reason=i.get("cutReason"), cut_at_ms=i.get("cutAtMs"),
                          producer=i.get("producer"))


class FakeHTTP:
    """A transport that answers from a list of (method, url prefix, reply) and records every request."""

    def __init__(self, *routes: tuple[str, str, Any]) -> None:
        self.routes = list(routes)
        self.requests: list[Request] = []

    def __call__(self, req: Request) -> Response:
        self.requests.append(req)
        for i, (method, prefix, reply) in enumerate(self.routes):
            if req.method == method and req.url.startswith(prefix):
                if isinstance(reply, list):  # a sequence of replies, the last repeated
                    reply = reply.pop(0) if len(reply) > 1 else reply[0]
                if isinstance(reply, BaseException):
                    raise reply
                return reply(req) if callable(reply) else reply
        raise AssertionError(f"unexpected {req.method} {req.url}")


def _json(status: int, body: Any) -> Response:
    return Response(status, json.dumps(body).encode())


# ── the stored document (SPEC §1-§5) ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_canonical_json_matches_the_golden_vectors_byte_for_byte(vector):
    encoded = encode_trace(_envelope(vector))
    assert encoded.json == vector["expected"]["canonical"].encode("utf-8")


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_span_order_and_key_insertion_order_do_not_change_the_bytes(vector):
    i = vector["input"]
    shuffled = [dict(reversed(list(s.items()))) for s in reversed(i["spans"])]
    flipped = {**vector, "input": {**i, "spans": shuffled, "resource": dict(reversed(list(i["resource"].items())))}}
    assert encode_trace(_envelope(flipped)).json == vector["expected"]["canonical"].encode()


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_stored_bytes_are_gzip_with_the_spec_header_deterministic_and_verifiable(vector):
    encoded = encode_trace(_envelope(vector))
    assert encoded.gz[:10] == GZIP_HEADER == bytes.fromhex("1f8b0800000000000003")
    assert gzip.decompress(encoded.gz) == encoded.json
    assert encode_trace(_envelope(vector)).gz == encoded.gz
    assert encoded.sha256 == hashlib.sha256(encoded.gz).hexdigest() and encoded.size == len(encoded.gz)
    # The reference writer's bytes (fflate) unpack to the same document: a verifier never re-compresses.
    fflate = base64.b64decode(vector["expected"]["gzip_base64"])
    assert gzip.decompress(fflate) == encoded.json
    assert hashlib.sha256(fflate).hexdigest() == vector["expected"]["sha256"]


def test_the_document_carries_the_agent_on_every_span_and_this_writer_as_producer():
    env = build_envelope(agent_id=AGENT, trace_id=TRACE, finish="complete", cut_reason="deadline", spans=[
        _span("00000000000000aa", 2_000_000_000, 3_000_000_000, attributes={"redberry.agent_id": "spoofed"}),
        _span("00000000000000bb", 1_000_000_000, None)])
    assert env["schema"] == "redberry.trace.v1" and env["cut_reason"] is None
    assert [s["spanId"] for s in env["spans"]] == ["00000000000000bb", "00000000000000aa"]
    assert {s["attributes"]["redberry.agent_id"] for s in env["spans"]} == {AGENT}
    assert env["producer"]["name"] == "redberry-sdk"
    assert env["started_at"] == "1970-01-01T00:00:01.000Z" and env["ended_at"] == "1970-01-01T00:00:03.000Z"
    assert storage_path(env) == f"redberry/{AGENT}/1970-01-01/{TRACE}.json.gz"


def test_a_cut_trace_ends_no_earlier_than_the_cut():
    env = build_envelope(agent_id=AGENT, trace_id=TRACE, finish="cut_short", cut_reason="hard_stop",
                         cut_at_ms=5000.7, spans=[_span("00000000000000aa", 1_000_000_000, None)])
    assert env["ended_at"] == "1970-01-01T00:00:05.000Z" and env["cut_reason"] == "hard_stop"


def _span(span_id: str, start: int, end: int | None, **extra: Any) -> dict[str, Any]:
    return {"traceId": TRACE, "spanId": span_id, "parentSpanId": None, "name": "s", "kind": 0,
            "startTimeUnixNano": str(start), "endTimeUnixNano": None if end is None else str(end),
            "attributes": {}, "status": {"code": 0, "message": None}, "events": [], "links": [],
            "scope": {"name": "x", "version": None, "schemaUrl": None},
            "dropped": {"attributes": 0, "events": 0, "links": 0}, **extra}


# ── canonical JSON (SPEC §3) ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("value", "text"), [
    (0.0, "0"), (-0.0, "0"), (1.0, "1"), (-1.5, "-1.5"), (0.1, "0.1"), (1e21, "1e+21"), (1e20, "100000000000000000000"),
    (1e-7, "1e-7"), (1e-6, "0.000001"), (123.456, "123.456"), (1.5e300, "1.5e+300"), (5e-324, "5e-324"),
    (1.7976931348623157e308, "1.7976931348623157e+308"), (2.5e-5, "0.000025"), (1e16, "10000000000000000"),
    (0.30000000000000004, "0.30000000000000004"), (123456789012345680000.0, "123456789012345680000"),
    (1.2345e-7, "1.2345e-7"), (-2.2250738585072014e-308, "-2.2250738585072014e-308"),
])
def test_floats_are_written_as_ecmascript_writes_them(value, text):
    assert _number(value) == text


def test_canonical_json_pins_what_json_cannot_hold():
    value = {"b": [float("nan"), float("inf"), float("-inf"), None, (1, 2)], "a": 2**53, "c": -(2**53 - 1),
             "d": True}
    assert canonical_json(value) == ('{"a":"9007199254740992","b":["NaN","Infinity","-Infinity",null,[1,2]],'
                                     '"c":-9007199254740991,"d":true}')


def test_strings_are_escaped_as_json_stringify_escapes_them():
    s = 'q" b\\ \b\f\n\r\t \x00\x1f \x7f    é 😀 \ud800 😀'
    assert canonical_json(s) == ('"q\\" b\\\\ \\b\\f\\n\\r\\t \\u0000\\u001f \x7f    é 😀 \\ud800 😀"')


def test_keys_sort_by_utf16_code_units():
    # Astral (a surrogate pair, 0xD83D...) sorts before U+E000 by code unit, after it by code point.
    assert canonical_json({"": 1, "😀": 2, "a": 3}) == '{"a":3,"😀":2,"":1}'


def test_binary_values_are_refused():
    with pytest.raises(TypeError):
        canonical_json({"a": b"x"})


def test_gzip_header_and_trailer():
    gz = deterministic_gzip(b"hello")
    assert gz[:10] == GZIP_HEADER and gzip.decompress(gz) == b"hello" and gz[-4:] == (5).to_bytes(4, "little")


# ── signing (SPEC §6) ────────────────────────────────────────────────────────────────────────────


def test_the_signature_matches_valps_python_vector():
    signer = load_signer(SIG["signing_key_pkcs8_b64"])
    assert signer.public_key.hex() == SIG["public_key_hex"] and signer.key_id == SIG["key_id"]
    body = SIG["body"].encode()
    signature = signer.sign(body)
    assert base64.b64encode(signature).decode() == SIG["signature_b64"]  # Ed25519 is deterministic
    assert verify(signer.public_key, body, signature) and not verify(signer.public_key, body + b" ", signature)


def test_the_signing_key_is_read_in_every_form_redberry_connect_or_a_person_might_give_it():
    seed = bytes.fromhex(SIG["seed_hex"])
    pem = Ed25519PrivateKey.from_private_bytes(seed).private_bytes(Encoding.PEM, PrivateFormat.PKCS8,
                                                                   NoEncryption()).decode()
    for value in (SIG["signing_key_pkcs8_b64"], pem, base64.b64encode(seed).decode(),
                  base64.urlsafe_b64encode(seed).decode().rstrip("=")):
        assert load_signer(value).key_id == SIG["key_id"]
    with pytest.raises(ValueError, match="REDBERRY_KEY_ID"):
        load_signer(SIG["signing_key_pkcs8_b64"], "0" * 32)
    with pytest.raises(ValueError, match="not a base64 PKCS8"):
        load_signer("not a key")


def test_key_id_is_valps():
    assert key_id_of(bytes.fromhex(SIG["public_key_hex"])) == SIG["key_id"]


# ── VALP's side of the report, re-implemented from valp_collector.hashes ─────────────────────────

_B64_SIG = re.compile(r"[A-Za-z0-9+/]{86}==")
_FIELDS = {"v", "agent_id", "trace_id", "sha256", "size", "started_at", "ended_at", "stored", "store", "leaf_kind"}
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z")
# VALP feat/azure-blob adds "azure-blob" to hashes.STORES.
_STORES = {"redberry", "vercel-blob", "gcs", "azure-blob", "s3"}


def valp_accepts(agent_id: str, headers: dict[str, str], raw: bytes, public_key: bytes) -> dict[str, Any]:
    """``Hashes.report``'s checks, in its order, for a project holding ``public_key``."""
    auth = headers["authorization"]
    scheme, _, token = auth.partition(" ")
    assert scheme.lower() == "bearer" and re.fullmatch(r"[0-9a-f]{64}", token.strip())
    assert len(raw) <= 4096
    assert headers["x-redberry-key"] == key_id_of(public_key)
    sig_header = headers["x-redberry-signature"]
    assert sig_header.startswith("ed25519=")
    value = sig_header[len("ed25519="):]
    assert _B64_SIG.fullmatch(value)
    sig = base64.b64decode(value, validate=True)
    assert len(sig) == 64 and base64.b64encode(sig).decode() == value
    try:  # keys.verify(public_key, sha256(raw).digest(), sig): Ed25519 over 0x05 || digest
        Ed25519PublicKey.from_public_bytes(public_key).verify(sig, b"\x05" + hashlib.sha256(raw).digest())
    except InvalidSignature:
        raise AssertionError("signature_invalid") from None
    body = json.loads(raw.decode("utf-8"))
    assert set(body) == _FIELDS
    assert type(body["v"]) is int and body["v"] == 1 and body["agent_id"] == agent_id
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", body["trace_id"])
    assert re.fullmatch(r"[0-9a-f]{64}", body["sha256"])
    assert type(body["size"]) is int and 0 < body["size"] <= 64 * 1024 * 1024
    for k in ("started_at", "ended_at"):
        assert _TIME.fullmatch(body[k])
    assert datetime.fromisoformat(body["started_at"]) <= datetime.fromisoformat(body["ended_at"])
    assert type(body["stored"]) is bool and body["store"] in _STORES and body["leaf_kind"] == "stored-bytes"
    return body


def _signed_report(store: str = "redberry") -> tuple[dict[str, Any], bytes, Any]:
    vector = _vectors()[1]  # v1-minimal
    env = _envelope(vector)
    enc = encode_trace(env)
    body = report_body(agent_id=env["agent_id"], trace_id=env["trace_id"], sha256=enc.sha256, size=enc.size,
                       started_at=env["started_at"], ended_at=env["ended_at"], stored=True, store=store)
    return body, enc.gz, load_signer(SIG["signing_key_pkcs8_b64"])


@pytest.mark.parametrize("store", ["redberry", "azure-blob"])
def test_the_report_round_trips_through_valps_checks(store):
    body, stored, signer = _signed_report(store)
    http = FakeHTTP(("POST", "https://valp.example/v1/agents/", _json(201, {"leaf": "hashes/x", "window_start": "w"})))
    out = send_report(url="https://valp.example/", token=TOKEN, signer=signer, body=body, transport=http,
                      retry_delays=NO_SLEEP)
    assert out.outcome == "recorded" and out.status == 201 and out.leaf == "hashes/x" and out.attempts == 1
    (req,) = http.requests
    assert req.method == "POST" and req.url == f"https://valp.example/v1/agents/{body['agent_id']}/hashes"
    assert req.headers["content-type"] == "application/json" and len(req.headers["x-redberry-signature"]) == 96
    checked = valp_accepts(body["agent_id"], req.headers, req.body, bytes.fromhex(SIG["public_key_hex"]))
    assert req.body == report_bytes(body) == canonical_json(checked).encode()
    # SPEC §9: the leaf's body against the stored bytes.
    assert checked["sha256"] == hashlib.sha256(stored).hexdigest() and checked["size"] == len(stored)
    doc = json.loads(gzip.decompress(stored))
    assert doc["schema"] == "redberry.trace.v1" and doc["trace_id"] == checked["trace_id"]
    assert (doc["agent_id"], doc["started_at"], doc["ended_at"]) == (
        checked["agent_id"], checked["started_at"], checked["ended_at"])


def test_report_retries_transient_failures_and_sends_the_same_bytes():
    body, _, signer = _signed_report()
    http = FakeHTTP(("POST", "https://v/", [OSError("reset"), Response(503), _json(200, {"leaf": "l"})]))
    out = send_report(url="https://v", token=TOKEN, signer=signer, body=body, transport=http, retry_delays=NO_SLEEP)
    assert out.outcome == "duplicate" and out.attempts == 3
    assert http.requests[1].body == http.requests[2].body


@pytest.mark.parametrize(("status", "outcome", "attempts"), [(409, "refused", 1), (403, "refused", 1),
                                                             (429, "failed", 3), (500, "failed", 3)])
def test_report_refusals_are_final_and_transient_errors_give_up_after_three(status, outcome, attempts):
    body, _, signer = _signed_report()
    http = FakeHTTP(("POST", "https://v/", Response(status, b'{"error":"x"}')))
    out = send_report(url="https://v", token=TOKEN, signer=signer, body=body, transport=http, retry_delays=NO_SLEEP)
    assert (out.outcome, out.status, out.attempts) == (outcome, status, attempts)
    assert out.error == f'HTTP {status}: {{"error":"x"}}'


def test_a_reply_asking_for_a_sample_gets_the_stored_bytes_with_exactly_its_headers():
    body, stored, signer = _signed_report()
    headers = {"Content-Type": "application/gzip", "x-goog-if-generation-match": "0",
               "x-goog-content-length-range": f"{len(stored)},{len(stored)}", "x-goog-meta-sha256": body["sha256"]}
    sample = {"url": "https://storage.example/sample?sig=1", "method": "PUT", "headers": headers, "object": "o"}
    http = FakeHTTP(("POST", "https://v/", _json(201, {"leaf": "l", "sample": sample})),
                    ("PUT", "https://storage.example/", [Response(200), Response(412)]))
    out = send_report(url="https://v", token=TOKEN, signer=signer, body=body, transport=http, retry_delays=NO_SLEEP)
    assert out.sample is not None and out.sample.headers == headers
    first = upload_sample(out.sample, stored, http, NO_SLEEP)
    again = upload_sample(out.sample, stored, http, NO_SLEEP)
    assert (first.ok, first.existing) == (True, False) and (again.ok, again.existing, again.status) == (True, True, 412)
    put = http.requests[1]
    assert put.method == "PUT" and put.headers == headers and put.body == stored


def test_a_sample_upload_failure_is_reported_not_raised():
    http = FakeHTTP(("PUT", "https://s/", Response(403, b"denied")))
    out = upload_sample(Sample(url="https://s/x"), b"gz", http, NO_SLEEP)
    assert (out.ok, out.status, out.error) == (False, 403, "HTTP 403: denied")


# ── stores ───────────────────────────────────────────────────────────────────────────────────────

META = PutMeta(AGENT, TRACE, "c" * 64, 3)


def test_redberry_store_asks_valp_for_a_signed_url_then_puts_the_bytes_there():
    signed = {"url": "https://storage.example/obj?sig", "method": "PUT", "object": "agent=x/y",
              "headers": {"content-type": "application/gzip", "x-goog-if-generation-match": "0"}}
    http = FakeHTTP(("POST", "https://v/v1/agents/", _json(200, signed)), ("PUT", "https://storage.example/", Response(200)))
    out = RedberryStore("https://v/", TOKEN, transport=http, retry_delays=NO_SLEEP).put("p", b"abc", META)
    assert out.url == "agent=x/y" and not out.existing
    ask, put = http.requests
    assert ask.url == f"https://v/v1/agents/{AGENT}/stored-logs/upload-url"
    assert ask.headers == {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
    assert json.loads(ask.body) == {"run_id": TRACE, "sha256": "c" * 64, "size": 3}
    assert put.headers == signed["headers"] and put.body == b"abc"


def test_redberry_store_412_means_our_bytes_are_already_there():
    http = FakeHTTP(("POST", "https://v/", _json(200, {"url": "https://s/o", "object": "o"})),
                    ("PUT", "https://s/", Response(412)))
    assert RedberryStore("https://v", TOKEN, transport=http, retry_delays=NO_SLEEP).put("p", b"x", META).existing


def test_redberry_store_refused_raises():
    http = FakeHTTP(("POST", "https://v/", Response(403, b"no")))
    with pytest.raises(RuntimeError, match="refused: HTTP 403"):
        RedberryStore("https://v", TOKEN, transport=http, retry_delays=NO_SLEEP).put("p", b"x", META)


def _mi_env(**extra: str) -> dict[str, str]:
    return {"IDENTITY_ENDPOINT": "http://localhost:8081/msi/token", "IDENTITY_HEADER": "secret-header",
            "AZURE_CLIENT_ID": "11111111-2222-3333-4444-555555555555", **extra}


def test_azure_blob_puts_create_only_with_a_managed_identity_token():
    http = FakeHTTP(("GET", "http://localhost:8081/msi/token", _json(200, {"access_token": "tok", "expires_on": "4102444800"})),
                    ("PUT", "https://acct.blob.core.windows.net/runs/", Response(201)))
    env = _mi_env(REDBERRY_STORE="azure-blob", REDBERRY_AZURE_CONTAINER_URL="https://acct.blob.core.windows.net/runs/")
    store = store_from_env(env, transport=http, retry_delays=NO_SLEEP)
    assert store.kind == "azure-blob"
    path = f"redberry/{AGENT}/2026-10-21/{TRACE}.json.gz"
    store.put(path, b"gz", META)
    store.put(path, b"gz", META)  # the token is cached
    token_req, put, _ = http.requests
    assert token_req.headers == {"X-IDENTITY-HEADER": "secret-header"}
    assert "resource=https%3A%2F%2Fstorage.azure.com%2F" in token_req.url and "api-version=2019-08-01" in token_req.url
    assert "client_id=11111111-2222-3333-4444-555555555555" in token_req.url
    assert put.url == f"https://acct.blob.core.windows.net/runs/{path}" and put.body == b"gz"
    h = put.headers
    assert h["authorization"] == "Bearer tok" and h["x-ms-version"] == "2023-11-03"
    assert h["x-ms-blob-type"] == "BlockBlob" and h["if-none-match"] == "*"
    assert h["content-type"] == "application/gzip" and h["x-ms-meta-sha256"] == "c" * 64 and h["x-ms-date"]


def test_azure_blob_system_assigned_identity_sends_no_client_id():
    http = FakeHTTP(("GET", "http://mi/", _json(200, {"access_token": "t", "expires_on": "4102444800"})))
    ManagedIdentityCredential(endpoint="http://mi/token", header="h", transport=http, retry_delays=NO_SLEEP).token()
    assert "client_id" not in http.requests[0].url


def test_azure_blob_conflict_is_a_store_conflict():
    http = FakeHTTP(("PUT", "https://a.blob.core.windows.net/c/", Response(409, b"BlobAlreadyExists")))
    store = AzureBlobStore("https://a.blob.core.windows.net/c", _StaticToken(), transport=http, retry_delays=NO_SLEEP)
    with pytest.raises(StoreConflictError):
        store.put("p", b"x", META)


def test_azure_blob_unauthorised_drops_the_cached_token():
    cred = _StaticToken()
    http = FakeHTTP(("PUT", "https://a.blob.core.windows.net/c/", Response(401)))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        AzureBlobStore("https://a.blob.core.windows.net/c", cred, transport=http, retry_delays=NO_SLEEP).put("p", b"x", META)
    assert cred.invalidated


@pytest.mark.parametrize("url", ["http://a.blob.core.windows.net/c", "https://a.blob.core.windows.net/c?sv=1&sig=x",
                                 "https://a.blob.core.windows.net", ""])
def test_azure_blob_container_url_is_https_without_a_sas(url):
    with pytest.raises(ValueError, match="REDBERRY_AZURE_CONTAINER_URL"):
        AzureBlobStore(url, _StaticToken())


def test_client_assertion_mode_exchanges_a_federated_token(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("federated.jwt\n")
    http = FakeHTTP(("POST", "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token",
                     _json(200, {"access_token": "aad", "expires_in": 3599})))
    cred = credential_from_env({"AZURE_TENANT_ID": "tenant-1", "AZURE_CLIENT_ID": "client-1",
                                "AZURE_FEDERATED_TOKEN_FILE": str(token_file)}, transport=http, retry_delays=NO_SLEEP)
    assert isinstance(cred, ClientAssertionCredential) and cred.token() == "aad"
    form = dict(p.split("=", 1) for p in http.requests[0].body.decode().split("&"))
    assert form["grant_type"] == "client_credentials" and form["client_id"] == "client-1"
    assert form["scope"] == "https%3A%2F%2Fstorage.azure.com%2F.default"
    assert form["client_assertion_type"] == "urn%3Aietf%3Aparams%3Aoauth%3Aclient-assertion-type%3Ajwt-bearer"
    assert form["client_assertion"] == "federated.jwt"


def test_no_azure_credential_is_a_clear_error():
    with pytest.raises(ValueError, match="IDENTITY_ENDPOINT"):
        credential_from_env({})


@pytest.mark.parametrize(("kind", "match"), [("gcs", "Vercel"), ("vercel-blob", "Vercel"), ("dynamodb", "must be")])
def test_stores_this_hasher_does_not_write_are_refused(kind, match):
    with pytest.raises(ValueError, match=match):
        store_from_env({"REDBERRY_STORE": kind})


class _StaticToken:
    def __init__(self) -> None:
        self.invalidated = False

    def token(self) -> str:
        return "t"

    def invalidate(self) -> None:
        self.invalidated = True
