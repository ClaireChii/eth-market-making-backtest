"""Command-line entry point for the market-data audit."""

import argparse
import json
from pathlib import Path
from typing import Any

from src.audit import audit_fundings, audit_orderbook, audit_trades
from src.data_files import dataset_files


def _audit_dataset(
    paths: list[Path],
    audit_function: Any,
) -> list[dict[str, Any]]:
    """Apply one dataset-specific audit function to each daily file."""
    return [audit_function(path) for path in paths]


def _build_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    """Build cross-dataset findings that require the assembled report."""
    funding_gaps = [
        item["gap_seconds"]["0.5"]
        for item in report["datasets"]["fundings"]
    ]
    return [
        {
            "severity": "warning",
            "code": "TIMEZONE_UNSPECIFIED",
            "message": (
                "Timestamps have nanosecond precision but no timezone metadata; "
                "UTC is a documented assumption, not a verified fact."
            ),
        },
        {
            "severity": "warning",
            "code": "FUNDING_IS_ROLLING",
            "message": (
                f"Funding observations arrive about every {funding_gaps} seconds. "
                "Treat them as a rolling slow signal, not as payment events."
            ),
        },
    ]


def build_audit_report() -> dict[str, Any]:
    """Run every daily audit and assemble the in-memory report."""
    files = dataset_files()
    report: dict[str, Any] = {
        "audit_version": 1,
        "reader": "polars",
        "timezone_assumption": (
            "naive timestamps treated as UTC pending venue metadata"
        ),
        "datasets": {
            "orderbook": _audit_dataset(files["orderbook"], audit_orderbook),
            "trades": _audit_dataset(files["trades"], audit_trades),
            "fundings": _audit_dataset(files["fundings"], audit_fundings),
        },
        "findings": [],
    }
    report["findings"] = _build_findings(report)
    return report


def print_summary(report: dict[str, Any]) -> None:
    """Print dataset totals, quality failures, and audit findings."""
    for dataset, daily_results in report["datasets"].items():
        total_rows = sum(item["rows"] for item in daily_results)
        quality_failures = sum(
            value
            for item in daily_results
            for value in item["quality"].values()
        )
        print(
            f"{dataset}: rows={total_rows:,}, "
            f"quality_failures={quality_failures:,}"
        )

    for finding in report["findings"]:
        print(
            f"[{finding['severity'].upper()}] "
            f"{finding['code']}: {finding['message']}"
        )


def main(argv: list[str] | None = None) -> None:
    """Run the audit command-line workflow."""
    parser = argparse.ArgumentParser(description="Audit the market data.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the complete structured audit report",
    )
    args = parser.parse_args(argv)
    report = build_audit_report()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_summary(report)


if __name__ == "__main__":
    main()
