"""User-level model profile persistence."""

from __future__ import annotations

from dataclasses import dataclass

from voidx.memory.session import _now
from voidx.memory.store import _get_db, _write_lock


@dataclass(frozen=True)
class ModelProfileRow:
    name: str
    provider: str
    model: str
    api_key: str = ""
    base_url: str | None = None
    protocol: str | None = None

def list_model_profiles() -> list[ModelProfileRow]:
    conn = _get_db()
    rows = conn.execute(
        """SELECT name, provider, model, api_key, base_url, protocol
           FROM model_profiles
           ORDER BY updated_at DESC, name ASC"""
    ).fetchall()
    return [_row_to_profile(row) for row in rows]


def get_model_profile(name: str) -> ModelProfileRow | None:
    conn = _get_db()
    row = conn.execute(
        """SELECT name, provider, model, api_key, base_url, protocol
           FROM model_profiles
           WHERE name = ?""",
        (name,),
    ).fetchone()
    return _row_to_profile(row) if row else None


def first_model_profile_for_provider(provider: str) -> ModelProfileRow | None:
    conn = _get_db()
    row = conn.execute(
        """SELECT name, provider, model, api_key, base_url, protocol
           FROM model_profiles
           WHERE provider = ?
           ORDER BY updated_at DESC, name ASC
           LIMIT 1""",
        (provider,),
    ).fetchone()
    return _row_to_profile(row) if row else None


def save_model_profile(profile: ModelProfileRow) -> None:
    now = _now()
    conn = _get_db()
    with _write_lock:
        conn.execute(
            """INSERT INTO model_profiles
               (name, provider, model, api_key, base_url, protocol, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   provider = excluded.provider,
                   model = excluded.model,
                   api_key = excluded.api_key,
                   base_url = excluded.base_url,
                   protocol = excluded.protocol,
                   updated_at = excluded.updated_at""",
            (
                profile.name,
                profile.provider,
                profile.model,
                profile.api_key,
                profile.base_url,
                profile.protocol,
                now,
                now,
            ),
        )
        conn.commit()


def delete_model_profile(name: str) -> None:
    conn = _get_db()
    with _write_lock:
        conn.execute("DELETE FROM model_profiles WHERE name = ?", (name,))
        conn.commit()


def _row_to_profile(row) -> ModelProfileRow:
    return ModelProfileRow(
        name=row["name"],
        provider=row["provider"],
        model=row["model"],
        api_key=row["api_key"],
        base_url=row["base_url"],
        protocol=row["protocol"],
    )
