# Upgrade Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every supported upgrade path install, verify, and cache an exact matching `voidx`/`voidx-cli` pair.

**Architecture:** Python self-update will use pair-oriented pip and verification helpers, including one forced-repair attempt and conditional rollback. npm will move duplicated pair installation, verification, and atomic marker logic into one shared module used by postinstall and runtime recovery. Shell and PowerShell installers will mirror the same externally visible invariant without introducing a cross-language locking system.

**Tech Stack:** Python 3.11+, asyncio subprocesses, pytest, Node.js 16+, Bash, PowerShell 5.1+, pip package metadata.

---

**Dirty-worktree rule:** The current workspace already contains unrelated edits,
including edits in `src/voidx/selfupdate.py`. Implementation steps must not use
whole-file `git add` or create implementation commits from pre-dirty files.
Leave implementation changes unstaged for user review, or use interactive
hunk-only staging only when the hunk contains exclusively this plan's changes.

## File Map

- Modify `src/voidx/selfupdate.py`: exact pair installation, subprocess verification, repair, rollback, atomic marker updates.
- Modify `src/tests/test_selfupdate/test_selfupdate.py`: red/green coverage for pair installation and recovery states.
- Create `npm/bin/runtime-install.js`: shared npm pair install, verification, wheel resolution, and atomic marker helpers.
- Modify `npm/bin/postinstall.js`: use shared pair installer and verifier.
- Modify `npm/bin/voidx.js`: repair both packages and verify marker-backed cached environments.
- Modify `npm/package.json`: include the shared helper in syntax checks.
- Modify `src/tests/test_npm_package.py`: npm helper and integration regression coverage.
- Modify `scripts/install.sh`: install exact pair in one pip call, verify, force-repair once, atomically mark success.
- Modify `scripts/install.ps1`: PowerShell equivalent of the shell installer behavior.
- Modify `src/tests/test_install_sh.py`: structural and behavior-oriented installer assertions.
- Modify `README.md`: coherent pip install and upgrade guidance.
- Modify `docs/usage-guide.md`: explain upgrade behavior and npm routing.

### Task 1: Python Self-Update Pair Transaction

**Files:**
- Modify: `src/tests/test_selfupdate/test_selfupdate.py:72`
- Modify: `src/voidx/selfupdate.py:101`

- [ ] **Step 1: Replace the sequential-install expectation with a failing pair-install test**

Update `test_perform_upgrade_runs_pip_for_newer_stable_version` so it stubs the new verification helper and expects one pip command containing both exact requirements:

```python
monkeypatch.setattr(
    selfupdate,
    "_verify_installation",
    AsyncMock(return_value=selfupdate._VerificationResult(ok=True, message="ok")),
)

assert len(pip_commands) == 1
assert "voidx==9.0.0" in pip_commands[0]
assert "voidx-cli==9.0.0" in pip_commands[0]
```

- [ ] **Step 2: Add failing verification and repair-order tests**

Add focused tests proving:

```python
async def test_upgrade_force_repairs_pair_once_before_rollback(...):
    # pre-state valid, target verification fails twice
    # expected pip order: target normal, target force-reinstall, old pair rollback

async def test_upgrade_does_not_claim_rollback_from_invalid_pre_state(...):
    # pre-state verification fails, target and repair verification fail
    # expected: no old-pair pip command, marker absent, manual target-pair command

async def test_upgrade_rejects_old_but_importable_cli(...):
    # verifier reports core=9.0.0, cli=3.5.1
    # expected: repair then failure/rollback, never success
```

- [ ] **Step 3: Replace every obsolete current-process and sequential-pip test**

Explicitly rewrite or remove these existing assumptions:

- `test_perform_upgrade_rolls_back_when_voidx_cli_fails`
- `test_perform_upgrade_succeeds_when_voidx_cli_importable`
- `test_perform_upgrade_updates_install_marker`
- `test_perform_upgrade_skips_marker_when_absent`
- `test_perform_upgrade_rollback_failure_reports_manual_fix`
- `test_can_import_voidx_cli_true_when_importable`
- `test_can_import_voidx_cli_false_when_metadata_exists_but_import_fails`
- `test_can_import_voidx_cli_false_when_not_installed`

Replace the `_can_import_voidx_cli` tests with direct tests of the fresh-process
verifier. No final test may assume two pip commands for a successful target
installation. Marker success tests must stub both a coherent pre-upgrade
verification and a successful target verification so they reach their marker
assertions without depending on raw subprocess call counts.

- [ ] **Step 4: Add the complete self-update verification failure matrix**

Parameterize `_verify_installation` tests for:

```text
core missing
CLI missing
new core + old CLI
old core + new CLI
metadata present but core import fails
metadata present but CLI import fails
entry point exits nonzero
entry point reports the wrong version
```

Add orchestration tests for initial pip failure, forced-repair pip failure,
rollback pip failure, rollback verification failure, unavailable target
requirements, and preservation/removal of the marker after each failed path.

- [ ] **Step 5: Add a failing Windows locked-file guidance test**

Feed a pip failure containing `WinError 32`, `WinError 5`, or a Windows
permission-denied replacement error into the upgrade failure formatter.
Expected message:

```text
Exit voidx, then run the normal installer again.
```

The test must also prove no success marker is written.

- [ ] **Step 6: Add failing atomic-marker tests**

Add tests that monkeypatch `os.replace` and assert:

```python
selfupdate._update_install_marker("9.0.0")

assert replace_calls == [(temporary_marker, marker_path)]
assert not temporary_marker.exists()
```

Also test that an invalid pre-upgrade state removes an existing success marker before pip mutation.

- [ ] **Step 7: Run the focused tests and confirm RED**

Run:

```bash
./test.py --backend -- src/tests/test_selfupdate/test_selfupdate.py -v
```

Expected: failures because pair-oriented `_pip_install`, `_VerificationResult`, `_verify_installation`, repair ordering, and atomic marker replacement do not exist yet.

- [ ] **Step 8: Implement pair-oriented pip installation**

Change the helper to accept multiple requirements and an optional forced reinstall:

```python
async def _pip_install(
    specs: tuple[str, ...],
    env: dict[str, str],
    timeout: float,
    *,
    force_reinstall: bool = False,
) -> _PipResult:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-cache-dir",
    ]
    if force_reinstall:
        command.append("--force-reinstall")
    command.extend(specs)
```

Use `(f"voidx=={target}", f"voidx-cli=={target}")` for target installation and exact previous versions for rollback.

- [ ] **Step 9: Implement fresh-process installation verification**

Add a result type and helper that uses `sys.executable` to:

1. read `importlib.metadata.version("voidx")`;
2. read `importlib.metadata.version("voidx-cli")`;
3. import `voidx` and `voidx_cli`;
4. require both versions to equal the expected target;
5. run the installed entry point with `--version` and require the target version.

Return a structured failure message instead of importing the post-upgrade modules into the current process.

- [ ] **Step 10: Implement repair, rollback, and invalid-prestate handling**

The flow in `perform_upgrade()` must be:

```text
verify previous state
invalidate marker if previous state is not a coherent pair
install target pair
verify target
if invalid: force-reinstall target pair once
verify target again
if still invalid and previous state was valid: rollback exact previous pair and verify it
if still invalid and previous state was invalid: return manual repair command
atomically update marker only after target verification succeeds
```

Rollback must preserve independently observed previous core and CLI versions and only run when they formed a valid same-version pair.

- [ ] **Step 11: Implement Windows locked-file guidance**

When pip output on Windows indicates executable or package replacement was
blocked, return the normal failure plus a deterministic instruction to exit
voidx and rerun `install.ps1`. This path must not write or update the marker.

- [ ] **Step 12: Implement atomic marker replacement**

Write preserved marker lines to a sibling temporary file and call `os.replace(temp_path, marker_path)`. Clean the temporary file in `finally`. Add `_clear_install_marker()` for invalid pre-state handling.

- [ ] **Step 13: Run the self-update tests and confirm GREEN**

Run:

```bash
./test.py --backend -- src/tests/test_selfupdate/test_selfupdate.py -v
```

Expected: all self-update tests pass.

- [ ] **Step 14: Inspect the Python diff without staging**

```bash
git diff -- src/voidx/selfupdate.py src/tests/test_selfupdate/test_selfupdate.py
```

Expected: the upgrade changes coexist with the pre-existing logging edit in
`selfupdate.py`; nothing is staged automatically.

### Task 2: Shared npm Runtime Installer

**Files:**
- Create: `npm/bin/runtime-install.js`
- Modify: `src/tests/test_npm_package.py`

- [ ] **Step 1: Add failing npm helper tests**

Add Node-backed tests for exported helpers:

```python
def test_runtime_installer_builds_one_pair_pip_command(...):
    # fake Python records argv
    # expect one command containing core spec and bundled wheel

def test_runtime_installer_requires_bundled_cli_wheel(...):
    # resolveBundledCliWheel must throw when the exact wheel is absent

def test_runtime_installer_verifies_both_metadata_versions(...):
    # fake verifier returns core target and old CLI
    # expect verifyPair(...).ok is false

def test_runtime_installer_writes_marker_atomically(...):
    # assert final marker content and no leftover temporary marker
```

Parameterize `verifyPair` for missing core, missing CLI, both mismatch
directions, import failure, entry-point failure, and wrong entry-point version.
Add an `installVerifyAndRepair` test proving exactly one forced full-pair retry
and unchanged marker state when the retry verification fails.

- [ ] **Step 2: Run npm helper tests and confirm RED**

Run:

```bash
./test.py --backend -- src/tests/test_npm_package.py -k "runtime_installer" -v
```

Expected: failures because `npm/bin/runtime-install.js` does not exist.

- [ ] **Step 3: Implement the shared helper module**

Export focused functions:

```javascript
function resolveBundledCliWheel(npmDir, version) { ... }
function installPair({ venvPython, coreSpec, cliSpec, env, forceReinstall }) { ... }
function verifyPair({ venvPython, executable, expectedVersion, env }) { ... }
function writeMarkerAtomic(markerPath, marker) { ... }
function installVerifyAndRepair(options) { ... }
```

`installVerifyAndRepair` performs one normal pair install, verifies, performs at most one forced pair reinstall on verification failure, re-verifies, and throws on final failure. It never writes the marker itself.

- [ ] **Step 4: Run npm helper tests and confirm GREEN**

Run:

```bash
./test.py --backend -- src/tests/test_npm_package.py -k "runtime_installer" -v
node --check npm/bin/runtime-install.js
```

Expected: helper tests pass and Node syntax check exits 0.

- [ ] **Step 5: Inspect the shared npm helper diff**

```bash
git diff -- npm/bin/runtime-install.js src/tests/test_npm_package.py
```

### Task 3: npm Postinstall And Launcher Recovery

**Files:**
- Modify: `npm/bin/postinstall.js:257`
- Modify: `npm/bin/voidx.js:191`
- Modify: `npm/package.json:32`
- Modify: `src/tests/test_npm_package.py`

- [ ] **Step 1: Add failing postinstall integration assertions**

Add tests proving:

- postinstall passes `voidx==<package version>` and the exact bundled CLI wheel to one shared pair install;
- a missing bundled wheel is fatal;
- no marker is written before pair verification;
- a matching marker is not accepted when verification fails.
- a failed forced repair leaves the previous marker unchanged and exits nonzero.

- [ ] **Step 2: Add failing launcher-recovery assertions**

Add a fake managed venv test where:

1. marker says the npm target;
2. core metadata reports the target;
3. CLI metadata reports an older version.

Expected: launcher invokes pair repair including the bundled wheel and only then writes the marker.

- [ ] **Step 3: Run npm integration tests and confirm RED**

Run:

```bash
./test.py --backend -- src/tests/test_npm_package.py -k "postinstall or launcher" -v
```

Expected: failures because postinstall and launcher still have separate core-only recovery logic.

- [ ] **Step 4: Integrate `runtime-install.js` into postinstall**

Remove local pair-install duplication. Resolve the exact bundled wheel, call `installVerifyAndRepair`, then call `writeMarkerAtomic`. A missing wheel or final verification failure must propagate to the existing top-level failure handler.

- [ ] **Step 5: Integrate `runtime-install.js` into launcher recovery**

`ensureVenv()` must:

1. verify an executable/marker cache before returning;
2. resolve the exact bundled wheel;
3. install and verify both packages when cache verification fails;
4. write the marker atomically only after verification.

Do not retain the current core-only pip block.

- [ ] **Step 6: Extend npm syntax checks**

Change `npm/package.json`:

```json
"check": "node --check bin/voidx.js && node --check bin/postinstall.js && node --check bin/runtime-install.js"
```

- [ ] **Step 7: Run npm tests and confirm GREEN**

Run:

```bash
./test.py --backend -- src/tests/test_npm_package.py -v
npm --prefix npm run check
```

Expected: all npm packaging tests pass and all three JavaScript files parse.

- [ ] **Step 8: Inspect npm integration diff**

```bash
git diff -- npm/bin/postinstall.js npm/bin/voidx.js npm/package.json src/tests/test_npm_package.py
```

### Task 4: Bash Installer Pair Verification

**Files:**
- Modify: `src/tests/test_install_sh.py`
- Modify: `scripts/install.sh:348`

- [ ] **Step 1: Add failing Bash installer tests**

Add assertions proving:

- `PIP_ARGS` contains both `voidx==${VERSION}` and `voidx-cli==${VERSION}`;
- the old second non-fatal CLI pip block is absent;
- a verification helper checks both metadata versions, imports both modules, and executes `${VOIDX_BIN} --version`;
- verification failure adds `--force-reinstall` to the complete pair exactly once;
- marker writing occurs after verification and uses temporary-file-plus-rename;
- a matching marker still calls verification before the early return.
- missing-package, mismatch, import-failure, and entry-point-failure probe
  results all flow to the forced pair repair and final fatal branch.

- [ ] **Step 2: Run Bash installer tests and confirm RED**

Run:

```bash
./test.py --backend -- src/tests/test_install_sh.py -k "InstallSh" -v
```

Expected: failures because CLI installation remains non-fatal and marker caching trusts text alone.

- [ ] **Step 3: Implement single-command pair installation**

Append both exact requirements to `PIP_ARGS` and delete `CLI_PIP_ARGS`. Keep custom index and trusted-host handling on the one command.

- [ ] **Step 4: Implement verification and forced repair**

Add `_verify_managed_install()` that runs the managed Python metadata/import probe and `${VOIDX_BIN} --version`. On failure, rerun the full pair command once with `--force-reinstall`, then verify again. Final failure exits nonzero with both exact manual requirements in the message.

- [ ] **Step 5: Verify cached installs and atomically write markers**

The marker-match branch returns only after `_verify_managed_install` succeeds. Write the marker to `${MARKER_PATH}.tmp.$$`, then `mv` it over the final path after successful verification.

- [ ] **Step 6: Run Bash installer tests and confirm GREEN**

Run:

```bash
./test.py --backend -- src/tests/test_install_sh.py -k "InstallSh" -v
bash -n scripts/install.sh
```

Expected: Bash installer tests pass and syntax validation exits 0.

- [ ] **Step 7: Inspect Bash installer changes**

```bash
git diff -- scripts/install.sh src/tests/test_install_sh.py
```

### Task 5: PowerShell Installer Pair Verification

**Files:**
- Modify: `src/tests/test_install_sh.py`
- Modify: `scripts/install.ps1:346`

- [ ] **Step 1: Add failing PowerShell structural tests**

Add assertions proving:

- `$PipArgs` includes both exact package requirements;
- `$CliPipArgs` and the non-fatal CLI warning block are absent;
- `Verify-ManagedInstall` checks metadata, imports, and `$VoidxBin --version`;
- one forced pair reinstall is attempted after verification failure;
- marker replacement uses a temporary marker and `Move-Item -Force`;
- marker-match early return verifies the environment first.
- all verification failure categories reach one forced pair repair and then
  `Abort-Install` without changing the marker.

- [ ] **Step 2: Run PowerShell installer tests and confirm RED**

Run:

```bash
./test.py --backend -- src/tests/test_install_sh.py -k "InstallPs1" -v
```

Expected: failures because PowerShell still installs and accepts the CLI independently.

- [ ] **Step 3: Implement one exact pair pip command**

Add both requirements to `$PipArgs`, remove `$CliPipArgs`, and preserve existing PowerShell 5.1-safe try/catch handling around native stderr.

- [ ] **Step 4: Implement verification, repair, and atomic marker replacement**

Create `Verify-ManagedInstall`. On initial verification failure, clone the pair arguments, add `--force-reinstall`, retry once, and verify again. Call `Abort-Install` on final failure. Write a temporary marker and replace the final marker only after verification succeeds.

- [ ] **Step 5: Run PowerShell installer tests and confirm GREEN**

Run:

```bash
./test.py --backend -- src/tests/test_install_sh.py -k "InstallPs1" -v
```

If `pwsh` is installed, also run:

```bash
pwsh -NoProfile -Command "[scriptblock]::Create((Get-Content scripts/install.ps1 -Raw)) | Out-Null"
```

Expected: tests pass; optional PowerShell parser exits 0.

- [ ] **Step 6: Inspect PowerShell installer changes**

```bash
git diff -- scripts/install.ps1 src/tests/test_install_sh.py
```

### Task 6: Customer Upgrade Documentation

**Files:**
- Modify: `README.md:24`
- Modify: `docs/usage-guide.md:245`
- Modify: `src/tests/test_npm_package.py`

- [ ] **Step 1: Add failing documentation assertions**

Add a test requiring README to contain:

```text
python -m pip install --upgrade voidx voidx-cli
npm update -g @chikhamx/voidx
```

Also require the usage guide to state that npm-launched installations upgrade through npm rather than `/upgrade now`.

- [ ] **Step 2: Run the documentation test and confirm RED**

Run:

```bash
./test.py --backend -- src/tests/test_npm_package.py -k "upgrade_docs" -v
```

Expected: failure because current README only installs the core package.

- [ ] **Step 3: Update customer-facing guidance**

Show coherent pip installation and upgrade commands, npm upgrade commands, and the restart requirement after `/upgrade now`. Do not recommend bypassing externally managed Python protections.

- [ ] **Step 4: Run the documentation test and confirm GREEN**

Run:

```bash
./test.py --backend -- src/tests/test_npm_package.py -k "upgrade_docs" -v
```

Expected: documentation assertion passes.

- [ ] **Step 5: Inspect documentation changes**

```bash
git diff -- README.md docs/usage-guide.md src/tests/test_npm_package.py
```

### Task 7: Integrated Verification

**Files:**
- Verify all files changed in Tasks 1-6.

- [ ] **Step 1: Run focused upgrade tests**

```bash
./test.py --backend -- \
  src/tests/test_selfupdate/test_selfupdate.py \
  src/tests/test_npm_package.py \
  src/tests/test_install_sh.py \
  -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run packaging and syntax checks**

```bash
./python.py scripts/package.py --check-only
npm --prefix npm run check
bash -n scripts/install.sh
```

Expected: all commands exit 0.

- [ ] **Step 3: Run adjacent upgrade command tests**

```bash
./test.py --backend -- src/tests/test_agent/slash/test_slash_upgrade.py -v
```

Expected: slash command behavior remains green.

- [ ] **Step 4: Run the backend suite**

```bash
./test.py --backend
```

Expected: backend and TUI tests pass. If unrelated dirty-tree tests fail, record the exact failures and rerun all upgrade-focused tests to prove this change remains green.

- [ ] **Step 5: Inspect final diff**

```bash
git diff --check
git diff --stat
git status --short
```

Expected: no whitespace errors; only intended upgrade files are part of this implementation, alongside pre-existing unrelated worktree changes.
