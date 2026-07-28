import type { SlashCommand } from "../utils/types";
import { uiState, inputEl, slashMenuEl, refMenuEl } from "../services/state";
import { rpcCall, isRpcConnected } from "../rpc";
import { renderSlashMenu } from "./slash";
import {
  type RefCandidate,
  type FileCandidate,
  type SkillCandidate,
  type McpCandidate,
  findRefToken,
  refInsertionText,
  fileInsertionText,
  mcpInsertionText,
  skillInsertionText,
  renderRefMenu,
} from "./reference";

export function showSlashMenu(): void {
  updateSlashMenu();
  slashMenuEl.classList.add("visible");
}

export function hideSlashMenu(): void {
  slashMenuEl.classList.remove("visible");
  uiState.slashCommands = [];
  uiState.slashSelectedIndex = 0;
}

export function updateSlashMenu(): void {
  const menu = renderSlashMenu(
    uiState.slashCommands,
    uiState.slashSelectedIndex,
    (command: SlashCommand) => {
      runSlashCommand(command);
      hideSlashMenu();
    },
  );
  slashMenuEl.replaceChildren(...menu.childNodes);
}

export function runSlashCommand(command: SlashCommand): void {
  if (!command) return;
  if (command.execution === "open-ui") {
    inputEl.value = "";
    window.dispatchEvent(
      new CustomEvent("voidx:open-ui", {
        detail: { target: command.uiTarget, command },
      }),
    );
    return;
  }
  if (command.execution === "run" && !command.requiresArgs) {
    const confirmed =
      !command.dangerous ||
      window.confirm(`Run ${command.command}?`);
    if (!confirmed) return;
    rpcCall("commands.run", {
      text: command.command,
      confirmed,
    }).catch(() => {});
    inputEl.value = "";
    return;
  }
  inputEl.value = command.command + " ";
  inputEl.focus();
}

// ── @ file / # skill reference menu ────────────────────────────────────
let refRequestSeq = 0;
let refDebounceTimer: number | undefined;

export function refMenuVisible(): boolean {
  return refMenuEl.classList.contains("visible");
}

export function showRefMenu(): void {
  updateRefMenu();
  refMenuEl.classList.add("visible");
  hideSlashMenu();
}

export function hideRefMenu(): void {
  refRequestSeq += 1;
  refMenuEl.classList.remove("visible");
  uiState.refCandidates = [];
  uiState.refSelectedIndex = 0;
  uiState.refToken = null;
}

export function updateRefMenu(): void {
  const menu = renderRefMenu(
    uiState.refCandidates,
    uiState.refSelectedIndex,
    (candidate: RefCandidate) => acceptRefCandidate(candidate),
  );
  refMenuEl.replaceChildren(...menu.childNodes);
}

export function scheduleRefUpdate(): void {
  window.clearTimeout(refDebounceTimer);
  refDebounceTimer = window.setTimeout(() => {
    void refreshRefCandidates();
  }, 120);
}

export async function refreshRefCandidates(): Promise<void> {
  const token = findRefToken(
    inputEl.value,
    inputEl.selectionStart ?? inputEl.value.length,
  );
  if (!token || !isRpcConnected()) {
    hideRefMenu();
    return;
  }
  const seq = ++refRequestSeq;
  if (token.trigger === "@") {
    try {
      const result = (await rpcCall("attachments.candidates", {
        thread_id: uiState.sessionId,
        query: token.query,
        limit: 8,
      })) as { candidates?: Array<Record<string, unknown>> };
      if (seq !== refRequestSeq) return;
      const current = findRefToken(
        inputEl.value,
        inputEl.selectionStart ?? inputEl.value.length,
      );
      if (!current || current.trigger !== token.trigger || current.query !== token.query) {
        return;
      }
      const raw = result.candidates ?? [];
      const candidates: RefCandidate[] = raw.map(
        (c) => ({ type: "file", file: c as unknown as FileCandidate }),
      );
      if (candidates.length === 0) {
        hideRefMenu();
        return;
      }
      uiState.refToken = token;
      uiState.refCandidates = candidates;
      uiState.refSelectedIndex = 0;
      showRefMenu();
    } catch {
      if (seq === refRequestSeq) hideRefMenu();
    }
    return;
  }
  try {
    const [skillResult, mcpResult] = await Promise.all([
      rpcCall("skills.candidates", {
        thread_id: uiState.sessionId,
        query: token.query,
        limit: 8,
      }) as Promise<{ candidates?: Array<Record<string, unknown>> }>,
      rpcCall("mcp.candidates", {
        thread_id: uiState.sessionId,
        query: token.query,
        limit: 8,
      }) as Promise<{ candidates?: Array<Record<string, unknown>> }>,
    ]);
    if (seq !== refRequestSeq) return;
    const current = findRefToken(
      inputEl.value,
      inputEl.selectionStart ?? inputEl.value.length,
    );
    if (!current || current.trigger !== token.trigger || current.query !== token.query) {
      return;
    }
    const skillRaw = skillResult.candidates ?? [];
    const mcpRaw = mcpResult.candidates ?? [];
    const candidates: RefCandidate[] = [
      ...skillRaw.map((c) => ({ type: "skill" as const, skill: c as unknown as SkillCandidate })),
      ...mcpRaw.map((c) => ({ type: "mcp" as const, mcp: c as unknown as McpCandidate })),
    ];
    if (candidates.length === 0) {
      hideRefMenu();
      return;
    }
    uiState.refToken = token;
    uiState.refCandidates = candidates;
    uiState.refSelectedIndex = 0;
    showRefMenu();
  } catch {
    if (seq === refRequestSeq) hideRefMenu();
  }
}

export function acceptRefCandidate(candidate: RefCandidate): void {
  const token = uiState.refToken;
  if (!token) return;
  const insertion = refInsertionText(candidate);
  const text = inputEl.value;
  inputEl.value = text.slice(0, token.start) + insertion + text.slice(token.end);
  const cursor = token.start + insertion.length;
  inputEl.setSelectionRange(cursor, cursor);
  const drillDown = candidate.type === "file" && candidate.file.kind === "dir";
  hideRefMenu();
  inputEl.focus();
  if (drillDown) scheduleRefUpdate();
}
