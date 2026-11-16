"""redberry_sdk._sigv4: AWS Signature Version 4, which the AWS emit path signs every export with. Checked
against AWS's published test vectors and, when installed, botocore."""
from __future__ import annotations

import hashlib

import pytest

from redberry_sdk._sigv4 import Credentials, canonical_request, sign

# ── SigV4: AWS's published test vectors ──────────────────────────────────────────────────────────
# aws-sig-v4-test-suite (service "service", us-east-1, 20150830T123600Z) and the S3 "Signature calculations
# for the Authorization header: single chunk" examples (examplebucket, us-east-1, 20130524T000000Z).

SUITE = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
S3_DOCS = Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
EMPTY = hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(("method", "path", "signature"), [
    ("GET", "/", "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"),                  # get-vanilla
    ("GET", "/?Param2=value2&Param1=value1",                                              # get-vanilla-query-order-key-case
     "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"),
    ("POST", "/", "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b"),                # post-vanilla
])
def test_sigv4_test_suite(method, path, signature):
    h = sign(method, f"https://example.amazonaws.com{path}", {"host": "example.amazonaws.com",
                                                               "x-amz-date": "20150830T123600Z"},
             b"", SUITE, "us-east-1", "service")
    assert h["authorization"] == ("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                                  f"SignedHeaders=host;x-amz-date, Signature={signature}")


def test_sigv4_s3_examples():
    def s3(method, path, headers, body=b""):
        h = {"host": "examplebucket.s3.amazonaws.com", "x-amz-date": "20130524T000000Z",
             "x-amz-content-sha256": hashlib.sha256(body).hexdigest(), **headers}
        auth = sign(method, f"https://examplebucket.s3.amazonaws.com{path}", h, body, S3_DOCS, "us-east-1",
                    "s3")["authorization"]
        return auth.rsplit("Signature=", 1)[1]

    assert s3("GET", "/test.txt", {"range": "bytes=0-9"}) == \
        "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"                     # GET Object
    assert s3("PUT", "/test$file.text", {"date": "Fri, 24 May 2013 00:00:00 GMT",
                                         "x-amz-storage-class": "REDUCED_REDUNDANCY"},
              b"Welcome to Amazon S3.") == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    assert s3("GET", "/?lifecycle", {}) == "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"
    assert s3("GET", "/?max-keys=2&prefix=J", {}) == \
        "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"                     # List objects


def test_sigv4_canonical_uri_s3_keeps_the_path_others_normalise_it():
    s3, _ = canonical_request("GET", "https://b.s3.amazonaws.com/a//b/../c%20d", {"host": "h"}, EMPTY,
                              double_encode=False)
    other, _ = canonical_request("GET", "https://x.amazonaws.com/a//b/../c%20d", {"host": "h"}, EMPTY)
    assert s3.splitlines()[1] == "/a//b/../c%20d" and other.splitlines()[1] == "/a/c%2520d"


def test_sigv4_agrees_with_botocore_with_a_session_token(monkeypatch):
    pytest.importorskip("botocore")
    import datetime as dt

    import botocore.auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials as BotoCredentials

    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: dt.datetime(2026, 10, 26, 10, 15))
    body = b"\x1f\x8b gz"
    url = "https://acme-runs.s3.eu-west-1.amazonaws.com/redberry/AGT-1/2026-10-26/0af7651916cd43dd.json.gz"
    headers = {"content-type": "application/gzip", "if-none-match": "*", "x-amz-meta-sha256": "ab" * 32}
    req = AWSRequest("PUT", url, data=body, headers=dict(headers))
    botocore.auth.S3SigV4Auth(BotoCredentials("AKIA1", "secret/1", "tok+en=="), "s3", "eu-west-1").add_auth(req)
    ours = sign("PUT", url, {**headers, "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
                             "x-amz-date": "20261026T101500Z"}, body, Credentials("AKIA1", "secret/1", "tok+en=="),
                "eu-west-1", "s3")
    assert ours["authorization"] == req.headers["Authorization"]
    assert ours["x-amz-security-token"] == req.headers["X-Amz-Security-Token"] == "tok+en=="
