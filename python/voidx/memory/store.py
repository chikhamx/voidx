"""Store shim — thin compat layer for transcript.py."""

# _fetch_all and _write_transaction are used by transcript.py
# which does tree-to-transcript conversion.
# These operate on the same SQLite DB but through the Rust store.

def _fetch_all(query: str, params: tuple = ()) -> list[dict]:
    """Execute a query and return all rows as dicts (placeholder)."""
    # Transcript operations use specific queries — handled by Rust store
    return []


def _write_transaction(statements: list[str]) -> None:
    """Execute multiple statements in a transaction (placeholder)."""
    # Write operations go through Rust SessionStore
    pass
