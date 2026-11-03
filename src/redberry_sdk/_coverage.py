"""Coverage report — emitted at init() time and persisted to .redberry/coverage.json."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .instrumentors import InstrumentorStatus

_log = logging.getLogger("redberry.sdk")


@dataclass
class CoverageReport:
    agent_id: str
    generated_at: str
    instrumentors: list[dict]
    tool_gaps: list[str] = field(default_factory=list)  # tools in manifest not wrapped
    notes: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        agent_id: str,
        statuses: list[InstrumentorStatus],
        tool_gaps: list[str] | None = None,
    ) -> CoverageReport:
        return cls(
            agent_id=agent_id,
            generated_at=datetime.now(UTC).isoformat(),
            instrumentors=[asdict(s) for s in statuses],
            tool_gaps=tool_gaps or [],
        )

    def print_summary(self) -> None:
        enabled = [i for i in self.instrumentors if i["enabled"]]
        disabled = [i for i in self.instrumentors if not i["enabled"]]
        print(f"\n[redberry] Coverage report for agent '{self.agent_id}'")
        print(f"  Enabled  ({len(enabled)}): " + ", ".join(i["name"] for i in enabled))
        if disabled:
            print(f"  Skipped  ({len(disabled)}): " + ", ".join(
                f"{i['name']} ({i['reason']})" for i in disabled
            ))
        if self.tool_gaps:
            print(f"  Tool gaps ({len(self.tool_gaps)}): " + ", ".join(self.tool_gaps))
            print("  → Run the manifest extractor to generate wrappers for these tools.")
        print()

    def persist(self, base_dir: Path | None = None) -> Path | None:
        """Write the report to ``<cwd>/.redberry/coverage.json``, else to the temp directory (a read-only
        working directory: AWS Lambda's ``/var/task``). Never raises: the report is a convenience, and
        ``init()`` must not fail the agent over it. Returns where it was written, or None."""
        text = json.dumps(asdict(self), indent=2)
        candidates = [base_dir] if base_dir else [Path(os.getcwd()) / ".redberry",
                                                  Path(tempfile.gettempdir()) / ".redberry"]
        for target_dir in candidates:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                out = target_dir / "coverage.json"
                out.write_text(text)
                return out
            except OSError:
                continue
        _log.info("redberry_sdk: coverage report not written (no writable directory)")
        return None
