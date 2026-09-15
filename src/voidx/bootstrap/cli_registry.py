"""Shared CLI instance, independent of command registration and implementations."""

import typer

cli = typer.Typer(
    name="voidx",
    help="A coding agent in your terminal.",
    no_args_is_help=False,
    invoke_without_command=True,
)
