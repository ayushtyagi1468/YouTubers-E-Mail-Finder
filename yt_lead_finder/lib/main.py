#!/usr/bin/env python3
"""yt_lead_finder — YouTube Creator Email Finder & Verifier CLI.

Discovers YouTube channels for a niche, extracts publicly published email
addresses, verifies deliverability (syntax / disposable / MX), and writes a
CSV or JSON report.

Usage:
    python main.py --query "Tech Reviews" --max-results 10 --out leads.csv --verify-mx
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List

from lib.exporter import export_report
from lib.extractor import extract_emails, pick_primary
from lib.scraper import discover_channels, enrich_channel
from lib.verifier import (
    DNS_ERROR,
    INVALID_DOMAIN,
    SKIPPED,
    VERIFIED,
    VerificationResult,
    verify_email,
)

DEFAULT_MAX_RESULTS = 10

# Statuses that make a lead worth reporting (SKIPPED/DNS_ERROR stay visible).
REPORTABLE = {VERIFIED, INVALID_DOMAIN, SKIPPED, DNS_ERROR}

logger = logging.getLogger("yt_lead_finder")


# --- CLI --------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yt_lead_finder",
        description="Find YouTube creators in a niche, extract their public "
        "emails, verify deliverability, and export a CSV report.",
    )
    parser.add_argument(
        "--query",
        required=True,
        help='Search niche, e.g. "Tech Reviews"',
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        metavar="N",
        help=f"Number of creators to scan (default: {DEFAULT_MAX_RESULTS})",
    )
    parser.add_argument(
        "--out",
        default="leads.csv",
        help="Output path (.csv or .json) (default: leads.csv)",
    )
    parser.add_argument(
        "--verify-mx",
        action="store_true",
        help="Run live DNS MX lookups on extracted domains",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def _progress(current: int, total: int, label: str) -> str:
    """Render a plain-text progress bar line."""
    width = 30
    filled = int(width * current / total) if total else width
    bar = "#" * filled + "." * (width - filled)
    return f"[{bar}] {current}/{total} {label}"


def run(args: argparse.Namespace) -> int:
    """Execute the pipeline; returns a process exit code (never raises)."""
    started = time.monotonic()
    print(f"== yt_lead_finder: query=\"{args.query}\" max_results={args.max_results} "
          f"out={args.out} verify_mx={args.verify_mx}")

    # Step 1: discover channels.
    print("[1/4] Discovering channels ...")
    channels = discover_channels(args.query, args.max_results, verbose=args.verbose)
    print(_progress(1, 1, f"discovered {len(channels)} channel(s)"))
    if not channels:
        print("No channels found - nothing to export. "
              "(Check connectivity or try another query.)")
        return 1

    # Step 2: enrich channels with about text, links, video descriptions.
    print("[2/4] Fetching channel metadata ...")
    enriched: List = []
    for index, channel in enumerate(channels, start=1):
        enriched.append(enrich_channel(channel, verbose=args.verbose))
        print(_progress(index, len(channels), channel.name or "unnamed channel"))

    # Step 3: extract + verify emails.
    print("[3/4] Extracting & verifying emails ...")
    leads: List[Dict] = []
    for index, channel in enumerate(enriched, start=1):
        email = pick_primary(extract_emails(channel.combined_text))
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        result = verify_email(email, verify_mx=args.verify_mx) if email else None
        mx_status = result.status if result else ""
        leads.append({
            "Channel Name": channel.name,
            "Channel URL": channel.url,
            "Extracted Email": email or "",
            "MX Status": mx_status,
            "Timestamp": timestamp,
        })
        print(_progress(index, len(enriched), f"{email or 'no email found'}"
                        + (f" ({mx_status})" if mx_status else "")))

    # Step 4: export.
    print("[4/4] Exporting report ...")
    out_path = export_report(leads, args.out)
    print(_progress(1, 1, f"wrote {out_path}"))

    elapsed = time.monotonic() - started
    verified = sum(1 for lead in leads if lead["MX Status"] == VERIFIED)
    skipped = sum(1 for lead in leads if lead["MX Status"] == SKIPPED)
    invalid = sum(1 for lead in leads if lead["MX Status"] == INVALID_DOMAIN)
    dns_errors = sum(1 for lead in leads if lead["MX Status"] == DNS_ERROR)
    print(f"Done in {elapsed:.1f}s: {len(leads)} lead(s) - "
          f"{verified} VERIFIED, {skipped} SKIPPED, {invalid} INVALID_DOMAIN, "
          f"{dns_errors} DNS_ERROR, "
          f"{len(leads) - verified - skipped - invalid - dns_errors} no-email")

    # Fail (exit 2) only when nothing at all could be verified/extracted,
    # so automation can distinguish "ran fine, few leads" from "ran dry".
    if verified == 0 and skipped == 0 and all(not lead["Extracted Email"] for lead in leads):
        return 2
    return 0


def main(argv: List[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted — partial results were not saved.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
