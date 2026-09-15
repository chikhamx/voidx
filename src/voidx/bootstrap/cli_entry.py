"""Static CLI registration; command implementations load only on invocation."""

from __future__ import annotations

import typer

from voidx.bootstrap.cli_registry import cli


@cli.callback(invoke_without_command=True)
def main(
    workspace: str = typer.Option(".", "-w", "--workspace", help="Working directory"),
    model: str = typer.Option(None, "-m", "--model", help="Model name"),
    provider: str = typer.Option(None, "-p", "--provider", help="Provider"),
    resume: str = typer.Option(None, "-r", "--resume", help="Resume a session by ID"),
    new: bool = typer.Option(False, "-n", "--new", help="Force new session"),
    web: bool = typer.Option(False, "--web", help="Start the Web UI gateway"),
    web_headless: bool = typer.Option(
        False,
        "--web-headless",
        help="Run without the terminal UI; requires --web",
    ),
    web_host: str = typer.Option("127.0.0.1", "--web-host", help="Web UI gateway host"),
    web_port: int = typer.Option(0, "--web-port", help="Web UI gateway port"),
    chat: bool = typer.Option(False, "-c", "--chat", help="Start the session in Chat mode (restricted, read-only tools)"),
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    """Start an interactive coding session."""
    from voidx.bootstrap.command_line import main as run

    run(workspace, model, provider, resume, new, web, web_headless, web_host, web_port, chat, version)


@cli.command()
def sessions() -> None:
    """List saved sessions."""
    from voidx.bootstrap.command_line import sessions as run

    run()


@cli.command()
def version() -> None:
    """Show version info."""
    from voidx.bootstrap.command_line import version as run

    run()
