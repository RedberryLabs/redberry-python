"""SDK configuration — loaded from redberry.yaml or passed directly to init()."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from . import _env


class EvaluationTopology(str, Enum):
    REDBERRY_HOSTED = "redberry_hosted"
    CLIENT_HOSTED = "client_hosted"


#: Where redberry-extract writes the manifest (redberry_extractor.cli).
DEFAULT_MANIFEST = ".redberry/manifest.json"

@dataclass
class SDKConfig:
    # Identity
    agent_id: str
    task_class: str | None = None

    # RAP run identifier (per scenario invocation). Set by the RAP outer loop
    # via REDBERRY_RUN_ID, or passed explicitly. Reported as the redberry.run_id
    # resource attribute; optional.
    run_id: str | None = None

    # OTel collector endpoint (defaults to local Docker stack)
    otel_endpoint: str = "http://localhost:4318"  # OTLP HTTP

    # Whether to capture message content (prompts/completions).
    # Setting False records metadata_only tier — content is not sent to collector.
    capture_content: bool = True

    # Topology controls what the Results Export service ships out of the stack.
    topology: EvaluationTopology = EvaluationTopology.REDBERRY_HOSTED

    # Evaluator endpoint (Topology 1 only).
    evaluator_endpoint: str | None = None

    # Declared annual interaction volume — feeds Contract 4.
    declared_annual_volume: int | None = None

    # Extra attributes added to every span as OTel resource attributes.
    extra_resource_attributes: dict[str, Any] = field(default_factory=dict)

    # VALP: the tool catalogue the insured confirmed at binding (tool_catalogue.confirmed.json).
    # When set, execute_tool spans get valp.is_external / valp.is_mutating from it; when unset,
    # they get neither (redberry_sdk.valp: omit rather than guess).
    valp_tool_catalogue: str | None = None

    # VALP: how long a run paused for a person's approval may wait before its interaction is closed
    # as failed (redberry_sdk.integrations.openai_agents, "pending-approval TTL"). Default 7 days.
    valp_pending_approval_ttl_s: float | None = None

    # An agent whose provider library is installed without its instrumentation package would run
    # unobserved (see instrumentors/_expected.py), so init() refuses to start. Set this (or
    # REDBERRY_ALLOW_MISSING_INSTRUMENTATION=1) only where a subset is installed deliberately; init()
    # then warns and logs at ERROR instead.
    allow_missing_instrumentation: bool = False

    # Content capture cannot be switched off silently (RAP docs/valp_single_chain_plan.md, A3): the
    # evidence and pricing both need content. With it off (capture_content=False, or an inherited
    # OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT other than "true") init() refuses to start,
    # unless this is set (or REDBERRY_ALLOW_CONTENT_CAPTURE_OFF=1): a metadata-only run, chosen.
    allow_content_capture_off: bool = False

    # The agent's code version and its manifest, reported on every span as resource attributes
    # (redberry.agent_code_version, redberry.manifest_hash: the SHA-256 of the manifest file's
    # bytes, as VALP's genesis computes it) for VALP spec v2's observed fingerprint. Env:
    # REDBERRY_AGENT_CODE_VERSION, REDBERRY_MANIFEST. The manifest defaults to
    # .redberry/manifest.json when that file exists.
    agent_code_version: str | None = None
    manifest_path: str | None = None

    def fingerprint_attributes(self) -> dict[str, str]:
        """``redberry.agent_code_version`` and ``redberry.manifest_hash``, where known."""
        import hashlib

        attrs: dict[str, str] = {}
        if self.agent_code_version:
            attrs["redberry.agent_code_version"] = self.agent_code_version
        manifest = self.manifest_path or (DEFAULT_MANIFEST if Path(DEFAULT_MANIFEST).is_file() else None)
        if manifest:
            try:
                attrs["redberry.manifest_hash"] = hashlib.sha256(Path(manifest).read_bytes()).hexdigest()
            except OSError as e:
                import logging
                logging.getLogger("redberry.sdk").warning("cannot read the manifest %s: %s", manifest, e)
        return attrs

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SDKConfig":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        with p.open() as fh:
            raw = yaml.safe_load(fh) or {}
        sdk_cfg = raw.get("sdk", {})
        return cls(
            agent_id=sdk_cfg["agent_id"],
            task_class=sdk_cfg.get("task_class"),
            run_id=sdk_cfg.get("run_id"),
            otel_endpoint=sdk_cfg.get("otel_endpoint", "http://localhost:4318"),
            capture_content=sdk_cfg.get("capture_content", True),
            topology=EvaluationTopology(
                sdk_cfg.get("topology", EvaluationTopology.REDBERRY_HOSTED.value)
            ),
            evaluator_endpoint=sdk_cfg.get("evaluator_endpoint"),
            declared_annual_volume=sdk_cfg.get("declared_annual_volume"),
            extra_resource_attributes=sdk_cfg.get("extra_resource_attributes", {}),
            valp_tool_catalogue=sdk_cfg.get("valp_tool_catalogue"),
            valp_pending_approval_ttl_s=sdk_cfg.get("valp_pending_approval_ttl_s"),
            allow_missing_instrumentation=bool(sdk_cfg.get("allow_missing_instrumentation", False)),
            allow_content_capture_off=bool(sdk_cfg.get("allow_content_capture_off", False)),
            agent_code_version=sdk_cfg.get("agent_code_version"),
            manifest_path=sdk_cfg.get("manifest_path"),
        )

    def apply_env_overrides(self) -> "SDKConfig":
        """Fill any missing fields from the REDBERRY_* (or the earlier OTELLINE_*) environment variables.

        Resolution order is: explicit kwargs / YAML > env vars > defaults.
        Called from `init()` after the config is otherwise resolved, so callers
        can opt out by passing values explicitly.

        Env vars:
          REDBERRY_AGENT_ID    — agent_id
          REDBERRY_RUN_ID      — run_id
          REDBERRY_TASK_CLASS  — task_class
          REDBERRY_OTEL_ENDPOINT — otel_endpoint
          VALP_TOOL_CATALOGUE  — valp_tool_catalogue
          VALP_PENDING_APPROVAL_TTL_S — valp_pending_approval_ttl_s
          REDBERRY_ALLOW_MISSING_INSTRUMENTATION=1 — allow_missing_instrumentation
          REDBERRY_ALLOW_CONTENT_CAPTURE_OFF=1 — allow_content_capture_off
          REDBERRY_AGENT_CODE_VERSION — agent_code_version
          REDBERRY_MANIFEST — manifest_path
        """

        def _get_stripped_env(name: str) -> str | None:
            value = _env.get(name)
            if value is None:
                return None
            value = value.strip()
            return value or None

        if not self.agent_id:
            env_id = _get_stripped_env("REDBERRY_AGENT_ID")
            if env_id is not None:
                self.agent_id = env_id
        if self.run_id is None:
            self.run_id = _get_stripped_env("REDBERRY_RUN_ID")
        if self.task_class is None:
            self.task_class = _get_stripped_env("REDBERRY_TASK_CLASS")
        env_endpoint = _get_stripped_env("REDBERRY_OTEL_ENDPOINT")
        if env_endpoint is not None and self.otel_endpoint == "http://localhost:4318":
            self.otel_endpoint = env_endpoint
        if self.valp_tool_catalogue is None:
            self.valp_tool_catalogue = _get_stripped_env("VALP_TOOL_CATALOGUE")
        if self.valp_pending_approval_ttl_s is None and (ttl := _get_stripped_env("VALP_PENDING_APPROVAL_TTL_S")):
            try:
                self.valp_pending_approval_ttl_s = float(ttl)
            except ValueError:
                raise ValueError(f"VALP_PENDING_APPROVAL_TTL_S must be a number of seconds, not {ttl!r}") from None
        if not self.allow_missing_instrumentation and _get_stripped_env(
                "REDBERRY_ALLOW_MISSING_INSTRUMENTATION") in ("1", "true", "yes"):
            self.allow_missing_instrumentation = True
        if not self.allow_content_capture_off and _get_stripped_env(
                "REDBERRY_ALLOW_CONTENT_CAPTURE_OFF") in ("1", "true", "yes"):
            self.allow_content_capture_off = True
        if self.agent_code_version is None:
            self.agent_code_version = _get_stripped_env("REDBERRY_AGENT_CODE_VERSION")
        if self.manifest_path is None:
            self.manifest_path = _get_stripped_env("REDBERRY_MANIFEST")
        return self
