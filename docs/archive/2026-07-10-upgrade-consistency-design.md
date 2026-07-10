# Upgrade Consistency Design

> **Status: Done** — Archived on 2026-07-11.

## Goal

Prevent supported upgrade paths from reporting success or caching an installed
version while `voidx` and `voidx-cli` are missing, mismatched, or unusable.

The supported paths covered by this design are:

- `/upgrade now`
- `npm install/update -g @chikhamx/voidx`
- npm launcher recovery on first run
- `scripts/install.sh` direct fallback
- `scripts/install.ps1`
- documented direct pip upgrades

## Installation Invariant

An installation is complete only when all of the following are true:

1. The installed `voidx` distribution version equals the requested target.
2. The installed `voidx-cli` distribution version equals the requested target.
3. `voidx_cli` is importable in the target Python environment.

No upgrade path may write `.voidx-install-version`, report success, or reuse a
cached environment until this invariant has been verified.

All managed installers pass the core and CLI requirements to one pip command.
The resolver must accept the exact pair or fail before the installer treats the
target as available. A path that installs or validates only one distribution
never counts as successful.

Verification runs in a fresh subprocess using the intended venv interpreter.
It reads both distribution versions, imports `voidx` and `voidx_cli`, and runs
the installed `voidx --version` entry point. This avoids accepting modules
cached by the still-running pre-upgrade process.

## Shared Install Rules

The normal install command uses exact pair requirements in one invocation:

```text
python -m pip install --upgrade --no-cache-dir voidx==V voidx-cli==V
```

For npm, the second requirement is the bundled `voidx_cli-V` wheel rather than
an index requirement. If post-install verification fails, the installer retries
the same pair once with forced reinstallation to repair files or metadata left
by an interrupted pip operation. A second verification failure is fatal.

Install markers are written atomically through a temporary file and replace.
The marker records the target version, bundled Python build, Python version, and
platform/architecture target. Its location inside the managed venv identifies
the environment. A stale, malformed, or mismatched marker triggers pair
verification and repair; it is never accepted on marker text alone.

## Python Self-Update

`perform_upgrade()` first verifies the pre-upgrade environment in a separate
subprocess. A valid pre-upgrade pair is recorded as the rollback target. An
invalid, missing, or mismatched pre-upgrade state is not considered
rollback-capable; any existing success marker is invalidated before mutation.

It then installs the requested pair in one pip subprocess.

After installation, it verifies the invariant in a separate interpreter
subprocess. An old but importable CLI therefore cannot satisfy a new target. If
the first target verification fails, self-update force-reinstalls the target
pair once and performs the full verification again.

If the repair verification still fails and the previous environment was valid,
rollback restores the previous exact pair in one pip command and verifies it
independently. If the previous environment was already invalid, no rollback
success is claimed: the new marker remains absent and the error reports exact
target-pair repair commands. The install marker is updated only after the target
installation invariant passes.

The currently running process continues using already-loaded old modules until
the user restarts it. On Windows, a locked executable or package file causes pip
to fail; `/upgrade now` reports that the user must exit voidx and run the normal
installer. It does not write a new marker or claim an on-disk upgrade succeeded.

## npm Bootstrap And Recovery

The npm postinstall script installs `voidx==<npm package version>` and the
bundled `voidx_cli-<version>` wheel in one pip command.

The bundled wheel is required. A missing wheel or failed CLI install is fatal.
The script verifies the installation invariant before writing the marker.

The runtime launcher recovery path uses the same pair installation,
force-repair retry, and verification as postinstall. It must not repair only
the core and then mark the environment complete.

The npm package version remains the source of truth for the managed environment.

## Shell And PowerShell Installers

The direct fallback installers pass exact version pins for both distributions
to one pip command. Any pair installation failure is fatal.

Before creating the symlink, updating PATH state, writing the marker, or
reporting success, each installer verifies metadata, imports, and the installed
entry point using the managed venv Python. Any mismatch performs one forced pair
repair, then exits with an actionable error if verification still fails. The
previous marker remains unchanged.

## Direct pip Documentation

Documentation must not suggest upgrading only the core package for terminal
users. The supported upgrade command installs both packages in one operation:

```bash
python -m pip install --upgrade voidx voidx-cli
```

Because `voidx-cli` pins the matching core version, pip must resolve a coherent
pair or fail rather than intentionally leaving the CLI behind.

For an explicit target, documentation uses matching exact pins for both
distributions. Externally managed Python environments continue to follow pip's
normal refusal behavior; the documentation recommends a virtual environment or
the managed npm/install-script path rather than bypass flags.

## Failure And Recovery Behavior

- A network or index failure during the CLI step is an overall upgrade failure.
- An interrupted operation may leave files partially changed, but no new marker
  is written atomically.
- Re-running the same installer verifies the pair, force-reinstalls both
  packages once when needed, and verifies again.
- Rollback errors are reported with exact manual repair commands.
- Existing unrelated installations elsewhere on `PATH` are not treated as
  proof that the managed venv is valid.
- A target is accepted only when both exact artifacts are available from the
  configured index or bundled npm package.
- Custom indexes and caches use the existing path-specific configuration, but
  cannot weaken exact-version or verification requirements.

## Testing

Regression coverage will verify:

- `/upgrade now` rejects an old but importable CLI after a failed CLI install.
- `/upgrade now` verifies exact core and CLI target versions before success.
- rollback restores the prior CLI version rather than assuming it matched core.
- each upgrade path submits a single exact core/CLI pair to pip.
- npm postinstall requires the bundled CLI wheel and verifies both versions.
- npm launcher recovery installs and verifies the bundled CLI before writing its
  marker.
- shell and PowerShell installers treat pair installation failure as fatal.
- shell and PowerShell markers are written atomically only after pair
  verification.
- stale, malformed, and target-matching-but-invalid markers trigger repair.
- missing core, missing CLI, mismatches in either direction, corrupt imports,
  unavailable targets, pip failures, and failed entry-point smoke tests do not
  report success.
- every managed path force-reinstalls the complete pair at most once after an
  initial verification failure, re-verifies the full invariant, and leaves the
  marker unchanged or absent when the retry fails.
- self-update tests distinguish rollback from a valid previous pair and repair
  failure from an already-invalid previous environment.
- README pip upgrade guidance names both distributions.

Focused backend and packaging tests run first, followed by the relevant broader
backend suite and JavaScript syntax checks.

Concurrent installers targeting the same managed venv are outside this change.
Users must not run multiple upgrade mechanisms against one environment at the
same time; cross-language locking can be designed separately if needed.
