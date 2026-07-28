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
