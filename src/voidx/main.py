"""CLI entry point — `voidx` defaults to interactive chat."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

cli = typer.Typer(
    name="voidx",
    help="A coding agent in your terminal.",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _vconsole():
    from voidx.ui.console import VoidConsole
    return VoidConsole()


async def _run_chat(
    workspace: str = ".",
    model: str | None = None,
    provider: str | None = None,
    resume: str | None = None,
    new_session: bool = False,
) -> None:
    from voidx.config import Settings
    from voidx.agent.graph import VoidXGraph
    from voidx.memory.session import get_session, list_sessions, create_session

    vconsole = _vconsole()
    settings = Settings()  # type: ignore[call-arg]
    cfg = settings.build_config()
    cfg.workspace = str(Path(workspace).resolve())

    if model:
        cfg.model.model = model
    if provider:
        cfg.model.provider = provider

    api_key = settings.resolve_api_key(cfg.model.provider)
    if not api_key:
        vconsole.error(
            f"No API key found for '{cfg.model.provider}'. "
            f"Set {cfg.model.provider.upper()}_API_KEY in .env"
        )
        raise typer.Exit(code=1)

    if resume:
        session = await get_session(resume)
        if not session:
            vconsole.error(f"Session not found: {resume}")
            raise typer.Exit(code=1)
        title = session.title[:60] + ("..." if len(session.title) > 60 else "")
        vconsole.print(f"[dim]Resumed {session.id}: {title}[/dim]")
    else:
        session = await create_session(
            workspace=cfg.workspace,
            provider=cfg.model.provider,
            model=cfg.model.model,
        )

    graph = VoidXGraph(cfg, api_key, session=session)
    await graph.run()


# ── default command (no subcommand needed) ──────────────────────────────

@cli.callback(invoke_without_command=True)
def main(
    workspace: str = typer.Option(".", "-w", "--workspace", help="Working directory"),
    model: str = typer.Option(None, "-m", "--model", help="Model name"),
    provider: str = typer.Option(None, "-p", "--provider", help="Provider"),
    resume: str = typer.Option(None, "-r", "--resume", help="Resume a session by ID"),
    new: bool = typer.Option(False, "-n", "--new", help="Force new session"),
) -> None:
    """Start an interactive coding session."""
    asyncio.run(_run_chat(workspace, model, provider, resume, new))


# ── subcommands ────────────────────────────────────────────────────────

@cli.command()
def sessions() -> None:
    """List saved sessions."""
    from voidx.memory.session import list_sessions
    vconsole = _vconsole()

    async def _run():
        sessions = await list_sessions()
        if not sessions:
            vconsole.print("No saved sessions.")
            return
        vconsole.print("[bold]Sessions:[/bold]")
        for s in sessions:
            vconsole.print(
                f"  [cyan]{s.id}[/cyan] | {s.title[:60]} | "
                f"{s.message_count} msgs | {s.updated_at[:16]}"
            )

    asyncio.run(_run())


@cli.command()
def version() -> None:
    """Show version info."""
    from voidx import __version__
    vconsole = _vconsole()
    vconsole.print(f"voidx v{__version__}")


if __name__ == "__main__":
    cli()
