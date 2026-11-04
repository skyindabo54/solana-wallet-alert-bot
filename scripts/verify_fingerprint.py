#!/usr/bin/env python3
"""Fetch Enhanced transactions and print conservative parsed swaps.

This verifier does not compute fingerprints. Configure HELIUS_API_KEY in the
process environment; no API key should be passed as a command-line argument.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from helius import HeliusClient, HeliusError  # noqa: E402
from swaps import SwapParseError, parse_swaps  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Helius Enhanced address transactions and print parsed "
            "buy/sell swaps for one mint. Does not compute fingerprints."
        )
    )
    parser.add_argument("address", help="Wallet address to query.")
    parser.add_argument("mint", help="Token mint address to identify swaps for.")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum Enhanced transactions to fetch (1-100; default: 100).",
    )
    parser.add_argument(
        "--before",
        help="Optional signature to paginate backward from.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--raw-output",
        type=Path,
        help="Optional path at which to save the raw Enhanced response array.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.limit <= 100:
        print("Error: --limit must be between 1 and 100.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("Error: --timeout must be greater than zero.", file=sys.stderr)
        return 2

    try:
        client = HeliusClient(timeout=args.timeout)
        transactions = client.get_address_transactions(
            args.address,
            limit=args.limit,
            before=args.before,
        )
        swaps = parse_swaps(transactions, args.address, args.mint)
    except (HeliusError, SwapParseError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.raw_output is not None:
        try:
            args.raw_output.parent.mkdir(parents=True, exist_ok=True)
            args.raw_output.write_text(
                json.dumps(transactions, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"Could not write raw response file: {exc}", file=sys.stderr)
            return 1

    # Print only the clean parsed output to stdout; any optional raw response
    # is written separately so it cannot be confused with parsed swap records.
    print(json.dumps(swaps, indent=2, ensure_ascii=False))
    print(
        f"\nParsed {len(swaps)} swap(s) from {len(transactions)} transaction(s).",
        file=sys.stderr,
    )
    if args.raw_output is not None:
        print(f"Raw Enhanced responses saved to {args.raw_output}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
