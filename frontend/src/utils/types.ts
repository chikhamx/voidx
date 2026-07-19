/** Model/provider profile summary returned by settings. */
export interface ProfileSummary {
  name: string;
  provider: string;
  model: string;
  base_url?: string | null;
  protocol?: string | null;
  configured?: boolean;
}

/** RPC-layer transcript node (alias for protocol.d.ts Item). */
export type TranscriptNode = Record<string, unknown>;

/** Internal stream state during live streaming. */
export interface StreamState {
  text: string;
  thinking: string;
  phase: string;
  el: HTMLElement;
  thinkingEl: HTMLElement;
  thinkingLabel: HTMLElement;
  thinkingBody: HTMLElement;
  textEl: HTMLElement;
  debounceTimer: ReturnType<typeof setTimeout> | null;
  committed?: boolean;
}

/** Dock tab identifiers. */
export type DockTab = "todo" | "terminal" | "diff" | "status";

/** Slash command definition from the server. */
export interface SlashCommand {
  command: string;
  description: string;
  category: string;
  execution: string;
  dangerous: boolean;
  requiresArgs: boolean;
  uiTarget?: string;
  [k: string]: unknown;
}

/** Sidebar callback signatures. */
export type SidebarCallbacks = {
  onThreadSelect: ((threadId: string) => void) | null;
  onNewThread: (() => void) | null;
  onThreadDelete: ((threadId: string) => void) | null;
  onThreadRename: ((threadId: string, title: string) => void) | null;
};

/** Terminal callback signatures. */
export type TerminalCallbacks = {
  onInput: ((text: string) => void) | null;
  onStart: (() => void) | null;
};

/** Diff review types. */
export interface DiffLine {
  content: string;
  type: "add" | "remove" | "context" | "header";
  oldLine?: number;
  newLine?: number;
}

export interface DiffHunk {
  header: string;
  lines: DiffLine[];
}

export interface DiffFile {
  path: string;
  hunks: DiffHunk[];
}

/** Settings state. */
export interface SettingsState {
  dialog: HTMLDialogElement | null;
  content: HTMLElement | null;
  tabs: HTMLElement | null;
  saveBtn: HTMLButtonElement | null;
  errorEl: HTMLElement | null;
  initialized: boolean;
  tabCallbacks: Map<string, () => void>;
}
