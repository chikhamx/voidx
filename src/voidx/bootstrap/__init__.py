"""Application composition roots."""

from typing import Any

__all__ = ["cli"]


def __getattr__(name: str) -> Any:
    if name != "cli":
        raise AttributeError(name)
    from voidx.bootstrap.command_line import cli

    return cli
