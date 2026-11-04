#!/usr/bin/env python3
"""Find a token's earliest buyers and resolve their funding chains.

The script uses Solana JSON-RPC signature history to find the oldest
successful token-address transaction, and Helius Enhanced transaction data
for fee payer, tokenTransfers, and nativeTransfers. It does not parse raw
account indices or pre/post balance arrays.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import config  # noqa: E402
from enrich.wallet import (  # noqa: E402
    SolanaRPC,
    SolanaRPCRateLimitError,
    WalletEnrichmentError,
    WalletFunderResolver,
)
from helius import HeliusClient, HeliusError  # noqa: E402

ENHANCED_PAGE_SIZE = 100
BUYER_LIMIT = 20

# These Enhanced transaction types describe mint or liquidity/pool setup,
# rather than an ordinary buyer receiving tokens. Matching is substring-based
# to cover variants such as INITIALIZE_POOL or ADD_LIQUIDITY_V2.
EXCLUDED_BUY_TYPE_MARKERS = (
    "MINT",
    "POOL",
    "LIQUIDITY",
)


class SeedError(RuntimeError):
    """Raised when a safe seed-buyer list cannot be constructed."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Walk a token mint's history, select its 20 earliest qualifying "
            "buyers, and resolve their SOL funders."
        )
    )
    parser.add_argument("mint", help="Token mint address (CA) to seed from.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30).",
    )
    return parser.parse_args()


def find_enhanced_history_through_launch(
    rpc: SolanaRPC,
    enhanced: HeliusClient,
    mint: str,
    launch_signature: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch the first 100 successful mint transactions without history paging.

    Helius getTransactionsForAddress returns signatures in ascending slot order.
    Those first signatures are then batch-enriched with POST /v0/transactions.
    The returned list is reordered to match the ascending signature list, so
    buyer selection can process it from launch onward.
    """
    options = {
        "limit": ENHANCED_PAGE_SIZE,
        "sortOrder": "asc",
        "filters": {"status": "succeeded"},
    }
    max_rate_limit_retries = 5
    for attempt in range(max_rate_limit_retries + 1):
        try:
            rpc_response = rpc.call(
                "getTransactionsForAddress",
                [mint, options],
            )
            break
        except SolanaRPCRateLimitError:
            if attempt >= max_rate_limit_retries:
                raise SeedError(
                    "Helius RPC continued returning HTTP 429 while fetching "
                    "the first transaction signatures."
                ) from None
            time.sleep(2.0)

    result = rpc_response.get("result")
    if not isinstance(result, dict):
        raise SeedError(
            "getTransactionsForAddress returned an unexpected result shape."
        )
    signature_entries = result.get("data")
    if not isinstance(signature_entries, list):
        raise SeedError(
            "getTransactionsForAddress result did not contain a data list."
        )
    if not signature_entries:
        raise SeedError(f"No successful transactions were found for mint {mint}.")

    signatures: list[str] = []
    for entry in signature_entries:
        if not isinstance(entry, dict):
            raise SeedError("A signature entry was not a JSON object.")
        signature = entry.get("signature")
        if not isinstance(signature, str) or not signature:
            raise SeedError("A signature entry did not contain a signature string.")
        if entry.get("err") is not None:
            raise SeedError(
                "The RPC returned a failed transaction despite the succeeded "
                "filter; refusing to use an uncertain launch history."
            )
        signatures.append(signature)

    if signatures[0] != launch_signature:
        raise SeedError(
            "The oldest signature lookup and ascending first-page query did not "
            "agree; refusing to guess the launch transaction."
        )

    transactions = enhanced.get_transactions_by_signatures(
        signatures,
        commitment="finalized",
    )
    transactions_by_signature: dict[str, dict[str, Any]] = {}
    for transaction in transactions:
        signature = transaction.get("signature")
        if not isinstance(signature, str) or not signature:
            raise SeedError(
                "An Enhanced transaction response did not contain a signature."
            )
        transactions_by_signature[signature] = transaction

    missing = [
        signature
        for signature in signatures
        if signature not in transactions_by_signature
    ]
    if missing:
        raise SeedError(
            f"Enhanced API returned {len(signatures) - len(missing)} of "
            f"{len(signatures)} requested transactions; refusing to omit "
            "possibly earlier buyers."
        )

    transactions_oldest_first = [
        transactions_by_signature[signature] for signature in signatures
    ]
    launch_transaction = transactions_by_signature.get(launch_signature)
    if launch_transaction is None:
        raise SeedError(
            "Enhanced API did not return the launch transaction; refusing to "
            "guess the deployer."
        )

    return transactions_oldest_first, launch_transaction


def _positive_token_amount(transfer: dict[str, Any]) -> bool:
    raw = transfer.get("rawTokenAmount")
    if isinstance(raw, dict):
        value = raw.get("tokenAmount")
    else:
        value = transfer.get("tokenAmount")
    if isinstance(value, bool) or value is None:
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def is_qualifying_buyer_transaction(
    transaction: dict[str, Any], mint: str
) -> bool:
    """Return True for a sourced target-mint transfer to the fee payer.

    Mint and pool/liquidity setup transaction types are excluded. A token
    transfer must identify a non-empty source user account, so a mint-style
    transfer without a source is not mistaken for a purchase.
    """
    fee_payer = transaction.get("feePayer")
    if not isinstance(fee_payer, str) or not fee_payer:
        return False

    transaction_type = transaction.get("type")
    if isinstance(transaction_type, str):
        normalized_type = transaction_type.upper().replace("-", "_").replace(" ", "_")
        if any(marker in normalized_type for marker in EXCLUDED_BUY_TYPE_MARKERS):
            return False

    token_transfers = transaction.get("tokenTransfers")
    if not isinstance(token_transfers, list):
        return False

    for transfer in token_transfers:
        if not isinstance(transfer, dict) or transfer.get("mint") != mint:
            continue
        if transfer.get("toUserAccount") != fee_payer:
            continue

        source = transfer.get("fromUserAccount")
        if not isinstance(source, str) or not source or source == fee_payer:
            continue
        if not _positive_token_amount(transfer):
            continue
        return True

    return False


def select_earliest_buyers(
    transactions_oldest_first: list[dict[str, Any]],
    mint: str,
    deployer: str,
    *,
    keep_deployer: bool = False,
    limit: int = BUYER_LIMIT,
) -> list[str]:
    """Return unique buyer fee payers in earliest-transaction order."""
    buyers: list[str] = []
    seen_buyers: set[str] = set()

    # The RPC query and signature-to-transaction ordering are ascending, so
    # the first qualifying occurrence of each fee payer is their earliest buy.
    for transaction in transactions_oldest_first:
        if not is_qualifying_buyer_transaction(transaction, mint):
            continue
        fee_payer = transaction["feePayer"]
        if not keep_deployer and fee_payer == deployer:
            continue
        if fee_payer in seen_buyers:
            continue
        seen_buyers.add(fee_payer)
        buyers.append(fee_payer)
        if len(buyers) >= limit:
            break

    return buyers


def main() -> int:
    args = parse_args()
    if not args.mint.strip():
        print("Error: mint must be a non-empty address.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("Error: --timeout must be greater than zero.", file=sys.stderr)
        return 2

    resolver: WalletFunderResolver | None = None
    try:
        rpc = SolanaRPC(timeout=args.timeout)
        enhanced = HeliusClient(timeout=args.timeout)
        resolver = WalletFunderResolver(
            database_path=config.DATABASE_PATH,
            rpc=rpc,
            enhanced=enhanced,
        )

        print(f"Walking signature history for mint {args.mint}...", file=sys.stderr)
        launch_signature = rpc.find_oldest_successful_signature(args.mint)
        transactions, launch_transaction = find_enhanced_history_through_launch(
            rpc,
            enhanced,
            args.mint,
            launch_signature,
        )

        deployer = launch_transaction.get("feePayer")
        if not isinstance(deployer, str) or not deployer:
            raise SeedError(
                "The launch transaction did not contain a feePayer address; "
                "cannot safely apply the deployer exclusion."
            )

        buyers = select_earliest_buyers(
            transactions,
            args.mint,
            deployer,
            keep_deployer=bool(config.KEEP_DEPLOYER),
            limit=BUYER_LIMIT,
        )

        resolved: list[dict[str, Any]] = []
        for index, buyer in enumerate(buyers, start=1):
            print(
                f"Resolving buyer {index}/{len(buyers)}: {buyer}",
                file=sys.stderr,
            )
            edges = resolver.resolve_wallet(buyer)
            resolved.append(
                {
                    "buyer": buyer,
                    "funding_edges": [
                        {
                            "funder": edge.funder_address,
                            "amount_sol": format(
                                Decimal(edge.amount_lamports) / Decimal(1_000_000_000),
                                "f",
                            ).rstrip("0").rstrip("."),
                            "signature": edge.signature,
                            "hop": edge.hop,
                        }
                        for edge in edges
                    ],
                }
            )
            if index < len(buyers):
                time.sleep(0.5)

        report = {
            "mint": args.mint,
            "launch_signature": launch_signature,
            "deployer": deployer,
            "keep_deployer": bool(config.KEEP_DEPLOYER),
            "buyers_selected": len(buyers),
            "database": str(config.DATABASE_PATH),
            "buyers": resolved,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    except (HeliusError, WalletEnrichmentError, SeedError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if resolver is not None:
            resolver.close()


if __name__ == "__main__":
    raise SystemExit(main())





















































































