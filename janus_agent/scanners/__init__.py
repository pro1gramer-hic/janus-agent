"""Free scanners: plain Python, no model. They build the JSON report the agent works from."""

import json
from pathlib import Path

from janus_agent.scanners import scan_config, scan_docs, scan_secrets

SCANNERS = {
    "config": scan_config.scan,
    "secrets": scan_secrets.scan,
    "docs": scan_docs.scan,
}

SEVERITY_ORDER = ["critical", "high", "medium", "low"]


def run_scan(target: Path) -> dict:
    """Run every scanner on target and return the combined report (nothing is written)."""
    target = Path(target)
    results = {name: scan(target) for name, scan in SCANNERS.items()}
    findings = [f for result in results.values() for f in result["findings"]]
    findings.sort(key=lambda f: SEVERITY_ORDER.index(f["severity"]))
    return {
        "count": len(findings),
        "summaries": {name: r["summary"] for name, r in results.items()},
        "findings": findings,
    }


def write_report(target: Path, report: dict) -> Path:
    out_dir = Path(target) / ".janus"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def is_blocking(report: dict) -> bool:
    """A critical or high finding should block a CI pipeline."""
    return any(f["severity"] in ("critical", "high") for f in report["findings"])
