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


async def _select_start_session(
    workspace: str,
    provider: str,
    model: str,
    resume: str | None,
    new_session: bool,
    vconsole,
):
    from voidx.memory.session import (
        create_session,
        get_session,
        latest_session_for_workspace,
    )

    if resume:
        session = await get_session(resume)
        if not session:
            vconsole.error(f"Session not found: {resume}")
            raise typer.Exit(code=1)
        title = session.title[:60] + ("..." if len(session.title) > 60 else "")
        vconsole.print(f"[dim]Resumed {session.id}: {title}[/dim]")
        return session

    if not new_session:
        session = await latest_session_for_workspace(workspace)
        if session:
            title = session.title[:60] + ("..." if len(session.title) > 60 else "")
            vconsole.print(f"[dim]Resumed {session.id}: {title}[/dim]")
            return session

    return await create_session(
        workspace=workspace,
        provider=provider,
        model=model,
    )


async def _run_chat(
    workspace: str = ".",
    model: str | None = None,
    provider: str | None = None,
    resume: str | None = None,
    new_session: bool = False,
) -> None:
    from voidx.ui.dock import set_dock, BottomInputDock
    set_dock(BottomInputDock())

    from voidx.config import Settings
    from voidx.agent.graph import VoidXGraph

    vconsole = _vconsole()
    ws_path = str(Path(workspace).resolve())
    settings = Settings(ws_path)
    cfg = settings.build_config()
    cfg.workspace = ws_path

    if model:
        cfg.model.model = model
    if provider:
        cfg.model.provider = provider

    profile = settings.resolve_profile()
    if profile:
        api_key = profile.api_key
    else:
        api_key = settings.resolve_api_key(cfg.model.provider)

    session = await _select_start_session(
        workspace=cfg.workspace,
        provider=cfg.model.provider,
        model=cfg.model.model,
        resume=resume,
        new_session=new_session,
        vconsole=vconsole,
    )

    graph = VoidXGraph(cfg, api_key, session=session, settings=settings)
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
