"""Resolve wallet funders using Solana RPC and Helius Enhanced transactions.

The first inbound native-SOL transfer of at least 0.05 SOL is selected by
walking Enhanced address history from newest to oldest and retaining the
oldest qualifying transfer. Burner funders (30 or fewer total signatures) are
followed for at most two additional funding edges. Exchange-like funders
(500 or more signatures) are recorded as endpoints and are not followed.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from db import WalletDatabase
from helius import HeliusClient, NATIVE_SOL_PSEUDO_MINT


LAMPORTS_PER_SOL = 1_000_000_000
MIN_INBOUND_LAMPORTS = 50_000_000  # 0.05 SOL
RPC_SIGNATURE_LIMIT = 1_000
BURNER_MAX_TRANSACTIONS = 30
EXCHANGE_MIN_TRANSACTIONS = 500
MAX_EXTRA_FUNDER_HOPS = 2
DEFAULT_RPC_BASE_URL = "https://mainnet.helius-rpc.com/"


class WalletEnrichmentError(RuntimeError):
    """Raised when wallet history or funder data cannot be safely resolved."""


class SolanaRPCRateLimitError(WalletEnrichmentError):
    """Raised when Solana RPC responds with HTTP 429."""


class SolanaRPC:
    """Small JSON-RPC client for Helius and standard Solana RPC methods."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        rpc_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        key = api_key or os.environ.get("HELIUS_API_KEY")
        configured_url = rpc_url or os.environ.get("HELIUS_RPC_URL")
        if configured_url:
            self.rpc_url = configured_url
        elif key:
            self.rpc_url = (
                f"{DEFAULT_RPC_BASE_URL}?{urlencode({'api-key': key})}"
            )
        else:
            raise WalletEnrichmentError(
                "Set HELIUS_API_KEY or HELIUS_RPC_URL before making RPC calls."
            )

        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = timeout
        self._request_id = 0

    def call(self, method: str, params: list[Any]) -> dict[str, Any]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        request = Request(
            self.rpc_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except HTTPError as exc:
            if exc.code == 429:
                raise SolanaRPCRateLimitError(
                    "Solana RPC returned HTTP 429 (rate limited)."
                ) from None
            raise WalletEnrichmentError(
                f"Solana RPC request failed with HTTP status {exc.code}."
            ) from None
        except URLError as exc:
            reason = getattr(exc, "reason", "unknown network error")
            raise WalletEnrichmentError(
                f"Could not reach the Solana RPC endpoint: {reason}"
            ) from None
        except TimeoutError:
            raise WalletEnrichmentError(
                f"Solana RPC request timed out after {self.timeout:g} seconds."
            ) from None
        except OSError as exc:
            raise WalletEnrichmentError(f"Solana RPC request failed: {exc}") from None

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WalletEnrichmentError(
                "Solana RPC returned a response that was not valid UTF-8 JSON."
            ) from exc

        if not isinstance(decoded, dict):
            raise WalletEnrichmentError("Solana RPC response was not a JSON object.")
        if "error" in decoded:
            error = decoded["error"]
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message", "Unknown JSON-RPC error")
                raise WalletEnrichmentError(
                    f"Solana RPC error {code}: {message}"
                )
            raise WalletEnrichmentError("Solana RPC returned an invalid error object.")
        if "result" not in decoded:
            raise WalletEnrichmentError(
                "Solana RPC response had neither a result nor an error."
            )
        return decoded

    def get_signatures(
        self,
        address: str,
        *,
        limit: int = RPC_SIGNATURE_LIMIT,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        if not address or not address.strip():
            raise ValueError("address must be a non-empty string")
        if not 1 <= limit <= RPC_SIGNATURE_LIMIT:
            raise ValueError("limit must be between 1 and 1000")

        options: dict[str, Any] = {"limit": limit, "commitment": "finalized"}
        if before:
            options["before"] = before
        response = self.call("getSignaturesForAddress", [address, options])
        result = response["result"]
        if not isinstance(result, list) or any(
            not isinstance(item, dict) for item in result
        ):
            raise WalletEnrichmentError(
                "getSignaturesForAddress returned an unexpected result shape."
            )
        for item in result:
            if not isinstance(item.get("signature"), str):
                raise WalletEnrichmentError(
                    "A signature-history entry did not contain a signature string."
                )
        return result

    def find_oldest_successful_signature(self, address: str) -> str:
        """Return the oldest successful signature in one ascending Helius query.

        Helius' getTransactionsForAddress method supports ascending sort order
        and a succeeded-status filter. Requesting one result avoids paging
        through every signature for high-activity mints.
        """
        if not address or not address.strip():
            raise ValueError("address must be a non-empty string")

        options = {
            "limit": 1,
            "sortOrder": "asc",
            "filters": {"status": "succeeded"},
        }
        max_rate_limit_retries = 5

        for attempt in range(max_rate_limit_retries + 1):
            try:
                response = self.call(
                    "getTransactionsForAddress",
                    [address, options],
                )
                break
            except SolanaRPCRateLimitError:
                if attempt >= max_rate_limit_retries:
                    raise WalletEnrichmentError(
                        "Solana RPC continued returning HTTP 429 after "
                        f"{max_rate_limit_retries} retries."
                    ) from None
                time.sleep(2.0)

        result = response.get("result")
        if not isinstance(result, dict):
            raise WalletEnrichmentError(
                "getTransactionsForAddress returned an unexpected result shape."
            )
        data = result.get("data")
        if not isinstance(data, list):
            raise WalletEnrichmentError(
                "getTransactionsForAddress result did not contain a data list."
            )
        if not data:
            raise WalletEnrichmentError(
                f"No successful transaction signatures were found for {address}."
            )

        oldest = data[0]
        if not isinstance(oldest, dict):
            raise WalletEnrichmentError(
                "The oldest transaction entry was not a JSON object."
            )
        signature = oldest.get("signature")
        if not isinstance(signature, str) or not signature:
            raise WalletEnrichmentError(
                "The oldest transaction entry did not contain a signature string."
            )
        if oldest.get("err") is not None:
            raise WalletEnrichmentError(
                "The ascending query returned a failed transaction despite the "
                "succeeded-status filter; refusing to guess the launch."
            )
        return signature

    def transaction_count_for_classification(self, address: str) -> tuple[int, bool]:
        """Return exact counts below 500; return (500, True) for 500+.

        A single 1000-signature page is enough to establish either an exact
        count below 500 or that the address is exchange-like. The sentinel 500
        in the database therefore means "500 or more", avoiding an expensive
        full history walk for high-volume funders.
        """
        page = self.get_signatures(address, limit=RPC_SIGNATURE_LIMIT)
        count = len(page)
        if count >= EXCHANGE_MIN_TRANSACTIONS:
            return EXCHANGE_MIN_TRANSACTIONS, True
        return count, False


@dataclass(frozen=True)
class FundingEdge:
    wallet_address: str
    funder_address: str
    amount_lamports: int
    signature: str
    hop: int


class WalletFunderResolver:
    """Resolve, classify, and persist wallet funding relationships."""

    def __init__(
        self,
        *,
        database_path: str | Path,
        rpc: SolanaRPC,
        enhanced: HeliusClient,
        max_extra_hops: int = MAX_EXTRA_FUNDER_HOPS,
    ) -> None:
        if max_extra_hops < 0:
            raise ValueError("max_extra_hops cannot be negative")
        self.rpc = rpc
        self.enhanced = enhanced
        self.max_extra_hops = max_extra_hops
        self.database = WalletDatabase(database_path)
        self._profile_cache: dict[str, tuple[int, bool]] = {}
        self._first_inbound_cache: dict[str, tuple[str, int, str] | None] = {}

    def _profile(self, address: str) -> tuple[int, bool]:
        if address not in self._profile_cache:
            self._profile_cache[address] = (
                self.rpc.transaction_count_for_classification(address)
            )
        return self._profile_cache[address]

    def _first_inbound_sol_transfer(
        self, address: str
    ) -> tuple[str, int, str] | None:
        """Find the earliest inbound native-SOL transfer meeting the threshold.

        Helius filters by the native-SOL pseudo-mint, incoming direction, and
        raw lamport amount, then sorts ascending. This avoids scanning the
        wallet's full transaction history. Results are cached per address.
        """
        if address in self._first_inbound_cache:
            return self._first_inbound_cache[address]

        pagination_token: str | None = None
        previous_token: str | None = None

        while True:
            response = self.enhanced.get_transfers_by_address(
                address,
                direction="in",
                mint=NATIVE_SOL_PSEUDO_MINT,
                amount_gte=MIN_INBOUND_LAMPORTS,
                limit=100,
                pagination_token=pagination_token,
                sort_order="asc",
                sol_mode="separate",
                commitment="finalized",
            )
            transfers = response.get("data")
            if not isinstance(transfers, list):
                raise WalletEnrichmentError(
                    "getTransfersByAddress response did not contain a data list."
                )

            for transfer in transfers:
                if not isinstance(transfer, dict):
                    raise WalletEnrichmentError(
                        "getTransfersByAddress returned a non-object transfer."
                    )
                # The native-SOL pseudo-mint excludes WSOL. Also require an
                # ordinary transfer so unwraps or other lamport changes are
                # not classified as funding.
                if transfer.get("mint") != NATIVE_SOL_PSEUDO_MINT:
                    continue
                if transfer.get("type") != "transfer":
                    continue
                if transfer.get("toUserAccount") != address:
                    continue

                funder = transfer.get("fromUserAccount")
                if not isinstance(funder, str) or not funder or funder == address:
                    continue

                amount_value = transfer.get("amount")
                if isinstance(amount_value, bool):
                    continue
                try:
                    amount_lamports = int(str(amount_value))
                except (TypeError, ValueError):
                    continue
                if amount_lamports < MIN_INBOUND_LAMPORTS:
                    continue

                signature = transfer.get("signature")
                if not isinstance(signature, str) or not signature:
                    raise WalletEnrichmentError(
                        "A qualifying native-SOL transfer did not contain a signature."
                    )

                result = (funder, amount_lamports, signature)
                self._first_inbound_cache[address] = result
                return result

            next_token = response.get("paginationToken")
            if next_token is None:
                self._first_inbound_cache[address] = None
                return None
            if not isinstance(next_token, str) or not next_token:
                raise WalletEnrichmentError(
                    "getTransfersByAddress returned an invalid pagination token."
                )
            if next_token == previous_token:
                raise WalletEnrichmentError(
                    "getTransfersByAddress pagination did not advance."
                )

            previous_token = next_token
            pagination_token = next_token
            time.sleep(0.5)

    def _persist(
        self,
        profiles: dict[str, tuple[int, bool]],
        edges: list[FundingEdge],
    ) -> None:
        self.database.save_resolution(
            profiles,
            (
                (
                    edge.wallet_address,
                    edge.funder_address,
                    edge.amount_lamports,
                    edge.signature,
                    edge.hop,
                )
                for edge in edges
            ),
        )

    def resolve_wallet(self, buyer_address: str) -> list[FundingEdge]:
        """Resolve a buyer's funding chain and persist wallets and edges.

        Hop zero is the buyer's direct funder. A burner funder may be followed
        for up to two additional edges (hops one and two). Exchange-like
        funders are persisted as the edge endpoint but are never followed.
        """
        if not buyer_address or not buyer_address.strip():
            raise ValueError("buyer_address must be a non-empty string")

        profiles: dict[str, tuple[int, bool]] = {}
        edges: list[FundingEdge] = []
        seen = {buyer_address}
        profiles[buyer_address] = self._profile(buyer_address)
        current_wallet = buyer_address
        hop = 0
        extra_hops_followed = 0

        while True:
            first_transfer = self._first_inbound_sol_transfer(current_wallet)
            if first_transfer is None:
                break

            funder, amount_lamports, signature = first_transfer
            if funder in seen:
                break
            seen.add(funder)

            funder_profile = self._profile(funder)
            profiles[funder] = funder_profile
            edges.append(
                FundingEdge(
                    wallet_address=current_wallet,
                    funder_address=funder,
                    amount_lamports=amount_lamports,
                    signature=signature,
                    hop=hop,
                )
            )

            funder_tx_count, is_exchange = funder_profile
            if is_exchange or funder_tx_count > BURNER_MAX_TRANSACTIONS:
                break
            if extra_hops_followed >= self.max_extra_hops:
                break

            # The qualifying funder is a burner; trace its own earliest inbound
            # funding transfer. This adds at most two edges after the buyer.
            current_wallet = funder
            hop += 1
            extra_hops_followed += 1

        self._persist(profiles, edges)
        return edges

    def close(self) -> None:
        """Compatibility hook; SQLite connections are opened per operation."""
        return None
