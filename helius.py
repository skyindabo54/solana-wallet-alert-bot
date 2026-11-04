"""Small standard-library client for Helius Enhanced Transactions API."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen


DEFAULT_API_BASE_URL = "https://api.helius.xyz"
DEFAULT_RPC_BASE_URL = "https://mainnet.helius-rpc.com/"
NATIVE_SOL_PSEUDO_MINT = "So11111111111111111111111111111111111111111"


class HeliusError(RuntimeError):
    """Base exception for Helius client errors."""


class HeliusConfigurationError(HeliusError):
    """Raised when the API key or endpoint is not configured."""


class HeliusTransportError(HeliusError):
    """Raised when the HTTP request fails."""


class HeliusResponseError(HeliusError):
    """Raised when the API response is not valid Enhanced transaction data."""


class HeliusClient:
    """Client for GET /v0/addresses/{address}/transactions.

    Configure ``HELIUS_API_KEY`` in the environment. For local tests or a
    compatible proxy, ``HELIUS_API_BASE_URL`` may override the API host.
    Responses are returned as decoded JSON without rewriting transaction data.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("HELIUS_API_KEY")
        if not self.api_key:
            raise HeliusConfigurationError(
                "Set the HELIUS_API_KEY environment variable before running."
            )

        self.base_url = (
            base_url or os.environ.get("HELIUS_API_BASE_URL") or DEFAULT_API_BASE_URL
        ).rstrip("/")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.timeout = timeout
        self.rpc_url = (
            os.environ.get("HELIUS_RPC_URL")
            or f"{DEFAULT_RPC_BASE_URL}?{urlencode({'api-key': self.api_key})}"
        )
        self._rpc_request_id = 0

    def get_transfers_by_address(
        self,
        address: str,
        *,
        direction: str = "any",
        mint: str | None = None,
        amount_gte: int | None = None,
        limit: int = 100,
        pagination_token: str | None = None,
        sort_order: str = "desc",
        sol_mode: str = "merged",
        commitment: str = "finalized",
    ) -> dict[str, Any]:
        """Fetch filtered transfers using Helius' getTransfersByAddress RPC.

        ``amount_gte`` is a raw on-chain amount. For native SOL this is in
        lamports. The result object contains ``data`` and ``paginationToken``.
        """
        if not address or not address.strip():
            raise ValueError("address must be a non-empty string")
        if direction not in ("in", "out", "any"):
            raise ValueError("direction must be 'in', 'out', or 'any'")
        if amount_gte is not None and amount_gte < 0:
            raise ValueError("amount_gte cannot be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if sort_order not in ("asc", "desc"):
            raise ValueError("sort_order must be 'asc' or 'desc'")
        if sol_mode not in ("merged", "separate"):
            raise ValueError("sol_mode must be 'merged' or 'separate'")
        if commitment not in ("confirmed", "finalized"):
            raise ValueError("commitment must be 'confirmed' or 'finalized'")

        options: dict[str, Any] = {
            "direction": direction,
            "limit": limit,
            "sortOrder": sort_order,
            "solMode": sol_mode,
            "commitment": commitment,
        }
        if mint:
            options["mint"] = mint
        if amount_gte is not None:
            options["filters"] = {"amount": {"gte": amount_gte}}
        if pagination_token:
            options["paginationToken"] = pagination_token

        self._rpc_request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_request_id,
            "method": "getTransfersByAddress",
            "params": [address, options],
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

        max_rate_limit_retries = 5
        for attempt in range(max_rate_limit_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                break
            except HTTPError as exc:
                if exc.code == 429 and attempt < max_rate_limit_retries:
                    time.sleep(1.0)
                    continue
                if exc.code == 429:
                    raise HeliusTransportError(
                        "Helius getTransfersByAddress continued returning HTTP 429 "
                        f"after {max_rate_limit_retries} retries."
                    ) from None
                raise HeliusTransportError(
                    f"Helius RPC request failed with HTTP status {exc.code}."
                ) from None
            except URLError as exc:
                reason = getattr(exc, "reason", "unknown network error")
                raise HeliusTransportError(
                    f"Could not reach the Helius RPC endpoint: {reason}"
                ) from None
            except TimeoutError:
                raise HeliusTransportError(
                    f"Helius RPC request timed out after {self.timeout:g} seconds."
                ) from None
            except OSError as exc:
                raise HeliusTransportError(f"Helius RPC request failed: {exc}") from None

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HeliusResponseError(
                "Helius RPC returned a response that was not valid UTF-8 JSON."
            ) from exc
        if not isinstance(decoded, dict):
            raise HeliusResponseError("Helius RPC response was not a JSON object.")
        if "error" in decoded:
            error = decoded["error"]
            if isinstance(error, dict):
                raise HeliusTransportError(
                    f"Helius getTransfersByAddress RPC error "
                    f"{error.get('code')}: {error.get('message', 'unknown error')}"
                )
            raise HeliusResponseError("Helius RPC returned an invalid error object.")

        result = decoded.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise HeliusResponseError(
                "getTransfersByAddress returned an unexpected result shape."
            )
        if any(not isinstance(item, dict) for item in result["data"]):
            raise HeliusResponseError(
                "getTransfersByAddress data contains a non-object item."
            )
        return result

    def get_transactions_by_signatures(
        self,
        signatures: list[str],
        *,
        commitment: str = "finalized",
    ) -> list[dict[str, Any]]:
        """Fetch Enhanced transaction details for a batch of signatures.

        Helius POST /v0/transactions accepts up to 100 signatures per request.
        The returned transaction objects are kept in the response order; callers
        should use each object's signature when matching them to a request.
        """
        if not signatures:
            raise ValueError("signatures must not be empty")
        if len(signatures) > 100:
            raise ValueError("at most 100 signatures can be fetched per request")
        if any(not isinstance(signature, str) or not signature for signature in signatures):
            raise ValueError("every signature must be a non-empty string")
        if commitment not in ("confirmed", "finalized"):
            raise ValueError("commitment must be 'confirmed' or 'finalized'")

        url = f"{self.base_url}/v0/transactions?{urlencode({'api-key': self.api_key})}"
        payload = {"transactions": signatures, "commitment": commitment}
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        max_rate_limit_retries = 5
        for attempt in range(max_rate_limit_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                break
            except HTTPError as exc:
                if exc.code == 429:
                    if attempt < max_rate_limit_retries:
                        time.sleep(1.0)
                        continue
                    raise HeliusTransportError(
                        "Helius Enhanced API continued returning HTTP 429 "
                        f"after {max_rate_limit_retries} retries."
                    ) from None
                raise HeliusTransportError(
                    f"Helius Enhanced API request failed with HTTP status {exc.code}."
                ) from None
            except URLError as exc:
                reason = getattr(exc, "reason", "unknown network error")
                raise HeliusTransportError(
                    f"Could not reach the Helius Enhanced API: {reason}"
                ) from None
            except TimeoutError:
                raise HeliusTransportError(
                    f"Helius Enhanced API request timed out after {self.timeout:g} seconds."
                ) from None
            except OSError as exc:
                raise HeliusTransportError(f"Helius request failed: {exc}") from None

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HeliusResponseError(
                "Helius returned a response that was not valid UTF-8 JSON."
            ) from exc
        if not isinstance(decoded, list):
            raise HeliusResponseError(
                "Expected POST /v0/transactions to return a JSON array."
            )
        if any(not isinstance(item, dict) for item in decoded):
            raise HeliusResponseError(
                "The Enhanced transaction response contains a non-object item."
            )
        return decoded

    def get_address_transactions(
        self,
        address: str,
        *,
        limit: int = 100,
        before: str | None = None,
        transaction_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch Enhanced transactions for an address.

        ``limit`` follows the Enhanced API endpoint's 1-100 limit. The complete
        response objects are returned so callers can inspect or save raw data.
        """
        if not address or not address.strip():
            raise ValueError("address must be a non-empty string")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        query: dict[str, str | int] = {"api-key": self.api_key, "limit": limit}
        if before:
            query["before"] = before
        if transaction_type:
            query["type"] = transaction_type

        url = (
            f"{self.base_url}/v0/addresses/{quote(address, safe='')}/transactions?"
            f"{urlencode(query)}"
        )
        request = Request(
            url,
            headers={"Accept": "application/json"},
            method="GET",
        )

        max_rate_limit_retries = 5
        for attempt in range(max_rate_limit_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                break
            except HTTPError as exc:
                if exc.code == 429:
                    if attempt < max_rate_limit_retries:
                        time.sleep(1.0)
                        continue
                    # Do not include the URL: it contains the API key.
                    raise HeliusTransportError(
                        "Helius Enhanced API continued returning HTTP 429 "
                        f"after {max_rate_limit_retries} retries."
                    ) from None
                # Do not include the URL: it contains the API key query parameter.
                raise HeliusTransportError(
                    f"Helius Enhanced API request failed with HTTP status {exc.code}."
                ) from None
            except URLError as exc:
                reason = getattr(exc, "reason", "unknown network error")
                raise HeliusTransportError(
                    f"Could not reach the Helius Enhanced API: {reason}"
                ) from None
            except TimeoutError:
                raise HeliusTransportError(
                    f"Helius Enhanced API request timed out after {self.timeout:g} seconds."
                ) from None
            except OSError as exc:
                raise HeliusTransportError(f"Helius request failed: {exc}") from None

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HeliusResponseError(
                "Helius returned a response that was not valid UTF-8 JSON."
            ) from exc

        # This endpoint is expected to return an array of Enhanced transaction
        # objects. Do not silently unwrap or reinterpret any other shape.
        if not isinstance(decoded, list):
            raise HeliusResponseError(
                "Expected the Enhanced address-transactions endpoint to return "
                "a JSON array. Inspect the raw API response before adapting "
                "the client."
            )
        if any(not isinstance(item, dict) for item in decoded):
            raise HeliusResponseError(
                "The Enhanced API response array contains a non-object item."
            )

        return decoded
