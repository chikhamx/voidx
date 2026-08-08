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
    from voidx.presentation.output.console import VoidConsole
    return VoidConsole()


def _print_version() -> None:
    from voidx import __version__
    _vconsole().print(f"voidx v{__version__}")


async def _select_start_session(
    resume: str | None,
    vconsole,
):
    from voidx.agent.adapters.persistence.session_repository import (
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
    chat: bool = False,
) -> None:
    from voidx.presentation.output.dock import set_dock, BottomInputDock
    set_dock(BottomInputDock())

    from voidx.bootstrap.agent import build_agent_app
    from voidx.bootstrap.application import build_settings

    vconsole = _vconsole()
    ws_path = str(Path(workspace).resolve())
    settings = await build_settings(ws_path)


    profile = await settings.resolve_profile()
    cfg = await settings.build_config(profile=profile)
    cfg.workspace = ws_path

    if model:
        cfg.model.model = model
    if provider:
        cfg.model.provider = provider

    if profile and profile.provider == cfg.model.provider:
        api_key = profile.api_key
    else:
        api_key = await settings.resolve_api_key(cfg.model.provider)

    session = await _select_start_session(
        resume=resume,
        vconsole=vconsole,
    )

    if not session and (chat or new_session):
        from voidx.agent.adapters.persistence.session_repository import create_session
        session = await create_session(
            workspace=ws_path,
            provider=cfg.model.provider,
            model=cfg.model.model,
            profile="chat" if chat else "coding",
            title="Chat session" if chat else "New session",
        )

    agent_app = build_agent_app(cfg, api_key, session=session, settings=settings)

    await agent_app.run(
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
    chat: bool = typer.Option(False, "-c", "--chat", help="Start the session in Chat mode (restricted, read-only tools)"),
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
) -> None:
    """Start an interactive coding session."""
    if version:
        _print_version()
        raise typer.Exit()
    if web_headless and not web:
        raise typer.BadParameter("--web-headless requires --web")
    from voidx.agent.facade import RunLoopStartupError

    try:
        asyncio.run(_run_chat(workspace, model, provider, resume, new, web, web_headless, web_host, web_port, chat))

    except RunLoopStartupError as exc:
        _vconsole().error(str(exc))
        raise typer.Exit(code=1) from None


# ── subcommands ────────────────────────────────────────────────────────

@cli.command()
def sessions() -> None:
    """List saved sessions."""
    from voidx.agent.adapters.persistence.session_repository import list_sessions
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
    _print_version()


if __name__ == "__main__":
    cli()
