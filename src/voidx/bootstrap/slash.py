"""Slash command composition."""

from __future__ import annotations

from voidx.agent.adapters.slash_host import build_slash_ports
from voidx.presentation.slash import SlashHandler


def build_slash_handler(host, *, session_repository=None, session_cleanup=None):
    ports = build_slash_ports(host)
    return SlashHandler(
        *ports,
        session_repository=session_repository,
        session_cleanup=session_cleanup,
    )
