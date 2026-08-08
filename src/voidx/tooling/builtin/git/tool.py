"""Git tool — structured Git tool with raw args string and whitelist routing."""

from __future__ import annotations

import shlex

from voidx.tooling.policy.git.policy import git_policy_for_args
from voidx.tooling.domain.context import ToolExecutionContext as ToolContext
from voidx.tooling.domain.result import ToolResult
from voidx.tooling.domain.schema import model_to_json_schema

from voidx.tooling.policy.git.constants import (
    DENIED_SUBCOMMANDS,
    DENIED_SUBCOMMAND_FLAGS,
)
from voidx.tooling.builtin.git.models import GitInput, GitRepo, GitProcessTimeout
from voidx.tooling.policy.git.routing import has_denied_flag
from voidx.tooling.policy.git.access import (
    resolve_path_context,
    external_requested_repo_root_error,
    external_repo_root_error,
    validate_runtime_access_plan,
)
from voidx.tooling.builtin.git.access import runtime_access_plan
from voidx.tooling.builtin.git.process import discover_repo
from voidx.tooling.builtin.git.routing import (
    _is_structured_route,
    _git_raw,
)
from voidx.tooling.builtin.git.handlers import _STRUCTURED_HANDLERS
from voidx.tooling.builtin.git.results import _result, _timeout_result


class GitTool:
    id = "git"
    description = (
        "Run path-scoped git commands. Pass only the git subcommand in args. "
        "Core read-only commands return structured JSON; other allowed commands return raw stdout/stderr inside JSON. "
        "Write commands require approval; destructive commands are denied."
    )

    def parameters_schema(self) -> dict:
        return model_to_json_schema(GitInput)

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            inp = GitInput.model_validate(args)
        except Exception as exc:
            return _result("unknown", ctx, ok=False, error=f"invalid_args: {exc}")
        effective_ctx = resolve_path_context(inp.path, ctx)
        if effective_ctx is None:
            return _result("unknown", ctx, ok=False, error="unsafe_path: path escapes workspace")
        explicit_root_error = external_requested_repo_root_error(inp.path, ctx, effective_ctx)
        if explicit_root_error:
            return _result("unknown", effective_ctx, ok=False, error=explicit_root_error)
        try:
            repo = await discover_repo(effective_ctx)
        except GitProcessTimeout as exc:
            return _timeout_result("discover", effective_ctx, exc.result)
        if repo is None:
            return _result("unknown", ctx, ok=False, error="not_a_git_repository")

        try:
            tokens = shlex.split(inp.args)
        except ValueError as exc:
            return _result("unknown", ctx, repo=repo, ok=False, error=f"invalid_args: {exc}")
        if not tokens:
            return _result("unknown", ctx, repo=repo, ok=False, error="invalid_args: empty command")

        subcommand = tokens[0]
        rest = tokens[1:]

        if subcommand in DENIED_SUBCOMMANDS:
            return _result(subcommand, ctx, repo=repo, ok=False, error=f"command_denied: subcommand '{subcommand}' is destructive and not allowed")

        denied_flags = DENIED_SUBCOMMAND_FLAGS.get(subcommand)
        if denied_flags and has_denied_flag(subcommand, rest, denied_flags):
            return _result(subcommand, ctx, repo=repo, ok=False, error=f"command_denied: destructive flag in '{subcommand}'")

        policy = git_policy_for_args({"args": inp.args})
        if not policy.allowed:
            return _result(policy.subcommand or subcommand, effective_ctx, repo=repo, ok=False, error=f"git_policy_denied: {policy.reason}")
        subcommand = policy.subcommand
        rest = list(policy.rest)

        root_error = external_repo_root_error(inp.path, ctx, effective_ctx, repo.repo_root)
        if root_error:
            return _result(subcommand, effective_ctx, repo=repo, ok=False, error=root_error)
        plan = await runtime_access_plan(repo)
        plan_error = validate_runtime_access_plan(effective_ctx, plan)
        if plan_error:
            return _result(subcommand, effective_ctx, repo=repo, ok=False, error=plan_error)

        try:
            handler = _STRUCTURED_HANDLERS.get(subcommand)
            if handler is not None and _is_structured_route(subcommand, rest):
                return await handler(rest, effective_ctx, repo)
            return await _git_raw(subcommand, rest, effective_ctx, repo)
        except GitProcessTimeout as exc:
            return _timeout_result(subcommand, effective_ctx, exc.result, repo=repo)
        except ValueError as exc:
            from pydantic import ValidationError as _VE
            if isinstance(exc, _VE):
                detail = "; ".join(e.get("msg", str(e)) for e in exc.errors())
                return _result(subcommand, ctx, repo=repo, ok=False, error=f"Invalid argument: {detail}")
            return _result(subcommand, ctx, repo=repo, ok=False, error=str(exc))
        except Exception as exc:
            return _result(subcommand, ctx, repo=repo, ok=False, error=str(exc))
