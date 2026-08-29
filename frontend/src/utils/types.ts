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
  debounceTimer: ReturnType<typeof setTimeout> | number | null;
  markdownProjection?: import("./markdown").StreamingMarkdownProjection;
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

/** Metadata-only workspace revision update from the incremental gateway protocol. */
export interface WorkspacePatch {
  revision: number;
  active_thread_id?: string;
  threads?: Array<Record<string, unknown>>;
  provider?: string;
  model?: string;
  workspace?: string;
  profile_configured?: boolean | null;
  permission_mode?: string;
  ai_approval_count?: number;
  runtime?: Record<string, unknown>;
  workspace_write_lock?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/** Append-only assistant stream update from the incremental gateway protocol. */
export interface StreamAppendDelta {
  item_id: string;
  turn_id: string;
  thread_id: string;
  stream_id: string;
  base_revision: number;
  revision: number;
  text: string;
  phase?: string;
  workspace_revision?: number;
  op?: string;
  [key: string]: unknown;
}

/** Client-side cursor for one stable thread/turn/item stream identity. */
export interface IncrementalStreamState {
  threadId: string;
  turnId: string;
  itemId: string;
  streamId: string;
  revision: number;
  phase: string;
  text: string;
}

export const DESKTOP_GATEWAY_CAPABILITIES = [
  "stream_append_v1",
  "workspace_patch_v1",
] as const;
