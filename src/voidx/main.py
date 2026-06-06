"""CLI entry point — `voidx` defaults to interactive chat."""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path

import typer

cli = typer.Typer(
    name="voidx",
    help="A coding agent in your terminal.",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _vconsole():
    from voidx.ui.output.console import VoidConsole
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
        get_session,
    )

    if resume:
        session = await get_session(resume)
        if not session:
            vconsole.error(f"Session not found: {resume}")
            raise typer.Exit(code=1)
        title = session.title[:60] + ("..." if len(session.title) > 60 else "")
        vconsole.print(f"[dim]Resumed {session.id}: {title}[/dim]")
        return session

    return None


async def _run_chat(
    workspace: str = ".",
    model: str | None = None,
    provider: str | None = None,
    resume: str | None = None,
    new_session: bool = False,
    web: bool = False,
    web_headless: bool = False,
    web_host: str = "127.0.0.1",
    web_port: int = 0,
) -> None:
    from voidx.ui.output.dock import set_dock, BottomInputDock
    set_dock(BottomInputDock())

    from voidx.config import Settings
    from voidx.agent.graph import VoidXGraph

    vconsole = _vconsole()
    ws_path = str(Path(workspace).resolve())
    settings = await Settings.create(ws_path)

    # Bind settings to catalog early so list_models() merges custom models
    from voidx.llm.catalog import bind_settings
    bind_settings(settings)

    cfg = await settings.build_config()
    cfg.workspace = ws_path

    if model:
        cfg.model.model = model
    if provider:
        cfg.model.provider = provider

    profile = await settings.resolve_profile()
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
    await graph.run(
        web=web,
        web_headless=web_headless,
        web_host=web_host,
        web_port=web_port,
        web_token=secrets.token_urlsafe(16) if web else "",
    )


# ── default command (no subcommand needed) ──────────────────────────────

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
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    """Start an interactive coding session."""
    if version:
        from voidx import __version__
        print(f"voidx v{__version__}")
        raise typer.Exit()
    if web_headless and not web:
        raise typer.BadParameter("--web-headless requires --web")
    asyncio.run(_run_chat(workspace, model, provider, resume, new, web, web_headless, web_host, web_port))


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
