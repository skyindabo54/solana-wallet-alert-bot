"""Conservatively parse Helius Enhanced transactions into wallet swap records.

The parser uses Enhanced ``tokenTransfers`` to identify the target mint's
wallet-side direction and amount. For the SOL leg it prefers ``events.swap``;
if that is absent or unusable, it accepts exactly one matching wallet-side
entry from ``nativeTransfers``. Ambiguous transactions are skipped rather than
guessing from account indices or balance deltas.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

LAMPORTS_PER_SOL = Decimal("1000000000")


class SwapParseError(ValueError):
    """Raised when parser input arguments are invalid."""


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite():
        return None
    return number


def _amount_string(value: Decimal) -> str:
    """Format a Decimal without scientific notation or insignificant zeros."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _token_amount(transfer: dict[str, Any]) -> Decimal | None:
    """Return a UI token amount, preferring the raw amount plus decimals."""
    raw = transfer.get("rawTokenAmount")
    if isinstance(raw, dict):
        raw_amount = _decimal(raw.get("tokenAmount"))
        decimals = raw.get("decimals")
        if raw_amount is not None and isinstance(decimals, int) and decimals >= 0:
            return raw_amount / (Decimal(10) ** decimals)

    # Enhanced responses commonly also include tokenAmount as a UI amount.
    return _decimal(transfer.get("tokenAmount"))


def _target_token_flow(
    transaction: dict[str, Any], wallet: str, mint: str
) -> tuple[str, Decimal] | None:
    transfers = transaction.get("tokenTransfers")
    if not isinstance(transfers, list):
        return None

    incoming = Decimal(0)
    outgoing = Decimal(0)
    found_incoming = False
    found_outgoing = False

    for transfer in transfers:
        if not isinstance(transfer, dict) or transfer.get("mint") != mint:
            continue

        amount = _token_amount(transfer)
        if amount is None or amount <= 0:
            continue

        from_user = transfer.get("fromUserAccount")
        to_user = transfer.get("toUserAccount")
        if to_user == wallet and from_user != wallet:
            incoming += amount
            found_incoming = True
        elif from_user == wallet and to_user != wallet:
            outgoing += amount
            found_outgoing = True

    # A transaction that both sends and receives the target mint may be a
    # routed/round-trip trade. Do not collapse it into a guessed buy or sell.
    if found_incoming == found_outgoing:
        return None
    if found_incoming:
        return "buy", incoming
    return "sell", outgoing


def _event_sol_amount(
    transaction: dict[str, Any], wallet: str, direction: str
) -> Decimal | None:
    events = transaction.get("events")
    if not isinstance(events, dict):
        return None
    swap = events.get("swap")
    if not isinstance(swap, dict):
        return None

    # A buy spends native SOL; a sell receives native SOL. Helius represents
    # native event amounts in lamports. If an account is supplied, require it
    # to be the wallet being analyzed.
    field = "nativeInput" if direction == "buy" else "nativeOutput"
    native_leg = swap.get(field)
    if not isinstance(native_leg, dict):
        return None
    account = native_leg.get("account")
    if account is not None and account != wallet:
        return None

    amount = _decimal(native_leg.get("amount"))
    if amount is None or amount <= 0:
        return None
    return amount / LAMPORTS_PER_SOL


def _native_transfer_sol_amount(
    transaction: dict[str, Any], wallet: str, direction: str
) -> Decimal | None:
    transfers = transaction.get("nativeTransfers")
    if not isinstance(transfers, list):
        return None

    relevant: list[Decimal] = []
    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue

        from_user = transfer.get("fromUserAccount")
        to_user = transfer.get("toUserAccount")
        matches_direction = (
            from_user == wallet and to_user != wallet
            if direction == "buy"
            else to_user == wallet and from_user != wallet
        )
        if not matches_direction:
            continue

        amount = _decimal(transfer.get("amount"))
        if amount is not None and amount > 0:
            relevant.append(amount)

    # More than one candidate can include rent, a separate transfer, or
    # multiple swap route legs. Without swap-event data, avoid guessing.
    if len(relevant) != 1:
        return None
    return relevant[0] / LAMPORTS_PER_SOL


def parse_swaps(
    transactions: list[dict[str, Any]], wallet: str, mint: str
) -> list[dict[str, Any]]:
    """Parse unambiguous buys and sells for ``mint`` by ``wallet``.

    Returned ``token_amount`` and ``sol_amount`` values are decimal strings
    to avoid floating-point precision loss. Transactions whose direction,
    token amount, or SOL amount cannot be determined conservatively are
    omitted. No account-index or pre/post-balance inference is performed.
    """
    if not isinstance(transactions, list):
        raise SwapParseError("transactions must be a list")
    if not wallet or not wallet.strip():
        raise SwapParseError("wallet must be a non-empty string")
    if not mint or not mint.strip():
        raise SwapParseError("mint must be a non-empty string")

    parsed: list[dict[str, Any]] = []
    for transaction in transactions:
        if not isinstance(transaction, dict):
            continue

        flow = _target_token_flow(transaction, wallet, mint)
        if flow is None:
            continue
        direction, token_amount = flow

        sol_amount = _event_sol_amount(transaction, wallet, direction)
        if sol_amount is None:
            sol_amount = _native_transfer_sol_amount(
                transaction, wallet, direction
            )
        if sol_amount is None or sol_amount <= 0:
            continue

        parsed.append(
            {
                "signature": transaction.get("signature"),
                "timestamp": transaction.get("timestamp"),
                "mint": mint,
                "direction": direction,
                "token_amount": _amount_string(token_amount),
                "sol_amount": _amount_string(sol_amount),
                "source": transaction.get("source"),
                "type": transaction.get("type"),
            }
        )

    return parsed