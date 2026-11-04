"""SQLite storage for seeded wallets and their funding relationships."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


FundingEdgeRow = tuple[str, str, int, str, int]
WalletProfile = tuple[int, bool]


class WalletDatabase:
    """Create and write the ``wallets`` and ``funding_edges`` tables.

    ``tx_count`` is exact below 500. A value of 500 is the sentinel for
    500-or-more transactions, which is enough to classify an exchange-like
    address without walking its entire history.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _create_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS wallets (
                    address TEXT PRIMARY KEY,
                    tx_count INTEGER NOT NULL,
                    is_exchange INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS funding_edges (
                    wallet_address TEXT NOT NULL,
                    funder_address TEXT NOT NULL,
                    amount_lamports INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    hop INTEGER NOT NULL,
                    PRIMARY KEY (wallet_address, hop),
                    FOREIGN KEY (wallet_address) REFERENCES wallets(address),
                    FOREIGN KEY (funder_address) REFERENCES wallets(address)
                )"""
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_resolution(
        self,
        profiles: dict[str, WalletProfile],
        edges: Iterable[FundingEdgeRow],
    ) -> None:
        """Atomically upsert wallet profiles and funding edges.

        Each edge tuple is ``(wallet, funder, amount_lamports, signature, hop)``.
        Wallet rows are written before edges to satisfy the foreign keys.
        """
        edge_rows = list(edges)
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            for address, (tx_count, is_exchange) in profiles.items():
                connection.execute(
                    """INSERT INTO wallets
                       (address, tx_count, is_exchange, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(address) DO UPDATE SET
                         tx_count = excluded.tx_count,
                         is_exchange = excluded.is_exchange""",
                    (address, tx_count, int(is_exchange), now),
                )

            for wallet, funder, amount_lamports, signature, hop in edge_rows:
                connection.execute(
                    """INSERT INTO funding_edges
                       (wallet_address, funder_address, amount_lamports,
                        signature, hop)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(wallet_address, hop) DO UPDATE SET
                         funder_address = excluded.funder_address,
                         amount_lamports = excluded.amount_lamports,
                         signature = excluded.signature""",
                    (wallet, funder, amount_lamports, signature, hop),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()