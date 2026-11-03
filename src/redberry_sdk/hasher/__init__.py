"""The Python hasher: hash, store and sign each AI agent run, as ``@redberrylabs/otel`` does on Vercel.

For Python agents on Azure Functions and AWS Lambda (and anywhere else a process handles one run per
invocation). Each run's spans become one stored document (``@redberrylabs/otel`` SPEC.md, ``schema:
redberry.trace.v1``): canonical JSON, gzip, SHA-256, written to the store (``redberry``, ``azure-blob`` or
``s3``), and a signed hash report sent to VALP (``POST /v1/agents/{agent_id}/hashes``), which keeps it as a
leaf in its ledger.

::

    import azure.functions as func
    from redberry_sdk.hasher import register, with_redberry

    register()
    app = func.FunctionApp()

    @app.route(route="refund")
    @with_redberry
    def refund(req: func.HttpRequest) -> func.HttpResponse:
        ...

Install with ``pip install "redberry-agent-sdk[hasher]"`` (plus ``[providers]`` for the LLM
instrumentation). Configuration is by environment variable: see ``redberry_sdk.hasher._config``.
Nothing here needs Docker or the bundle.
"""
from ._azure import DEFAULT_TIMEOUT, get_processor, register, with_redberry
from ._canonical import canonical_bytes, canonical_json, deterministic_gzip
from ._config import HasherConfig, resolve_config
from ._crypto import Signer, key_id_of, load_signer, signing_message, verify
from ._envelope import (
    AGENT_ATTRIBUTE,
    PRODUCER,
    SCHEMA,
    EncodedTrace,
    build_envelope,
    encode_trace,
    storage_path,
)
from ._processor import RedberryProcessor, TraceResult
from ._report import (
    ReportOutcome,
    Sample,
    SampleOutcome,
    report_body,
    report_bytes,
    send_report,
    upload_sample,
)
from ._span import is_ai_span, sort_spans, span_to_json
from .stores import (
    AzureBlobStore,
    ClientAssertionCredential,
    EnvAwsCredentials,
    ManagedIdentityCredential,
    PutMeta,
    PutResult,
    RedberryStore,
    S3Store,
    Store,
    StoreConflictError,
    WebIdentityCredentials,
    aws_credentials_from_env,
    credential_from_env,
    store_from_env,
)

__all__ = [
    "AGENT_ATTRIBUTE",
    "DEFAULT_TIMEOUT",
    "PRODUCER",
    "SCHEMA",
    "AzureBlobStore",
    "ClientAssertionCredential",
    "EncodedTrace",
    "EnvAwsCredentials",
    "HasherConfig",
    "ManagedIdentityCredential",
    "PutMeta",
    "PutResult",
    "RedberryProcessor",
    "RedberryStore",
    "ReportOutcome",
    "S3Store",
    "Sample",
    "SampleOutcome",
    "Signer",
    "Store",
    "StoreConflictError",
    "TraceResult",
    "WebIdentityCredentials",
    "aws_credentials_from_env",
    "build_envelope",
    "canonical_bytes",
    "canonical_json",
    "credential_from_env",
    "deterministic_gzip",
    "encode_trace",
    "get_processor",
    "is_ai_span",
    "key_id_of",
    "load_signer",
    "register",
    "report_body",
    "report_bytes",
    "resolve_config",
    "send_report",
    "signing_message",
    "sort_spans",
    "span_to_json",
    "storage_path",
    "store_from_env",
    "upload_sample",
    "verify",
    "with_redberry",
]
