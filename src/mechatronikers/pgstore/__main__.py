"""Migrations from the command line, against the store in `MT_PG_DSN`.

python -m mechatronikers.pgstore migrate    # apply pending migrations (idempotent)
python -m mechatronikers.pgstore status     # list migrations and when they were applied
"""

import argparse
import sys

import psycopg

from mechatronikers.pgstore.db import StoreConfigError, connect, scrub
from mechatronikers.pgstore.migrate import MigrationError, migrate, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mechatronikers.pgstore",
        description="Migrations of the central Postgres store (DSN from MT_PG_DSN).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending migrations")
    sub.add_parser("status", help="list migrations and when they were applied")
    args = parser.parse_args(argv)
    try:
        with connect() as conn:
            if args.command == "migrate":
                applied = migrate(conn)
                print(
                    "applied " + ", ".join(f"{v:04d}" for v in applied) if applied else "up to date"
                )
            else:
                for row in status(conn):
                    when = row.applied_at.isoformat() if row.applied_at else "pending"
                    print(f"{row.version:04d}_{row.name}  {when}")
    except (StoreConfigError, MigrationError, psycopg.Error) as exc:
        print(f"error: {scrub(str(exc))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
