"""Configuration defaults for the wallet seeding tools."""

from pathlib import Path

# Keep the deployer out of the seed buyer list unless explicitly enabled.
KEEP_DEPLOYER = False

# SQLite database is created at the repository root by default.
DATABASE_PATH = Path(__file__).resolve().parent / "wallets.db"