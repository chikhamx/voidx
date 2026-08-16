import type { TranscriptNode } from '../rpc/protocol';


/* ── Local type aliases ── */

/** Concrete payload fields accessed by render.ts */
export interface NodePayload {
  tool_name?: string;
  diff_text?: string;
  args?: string | Record<string, unknown>;
  raw_args?: Record<string, unknown>;
  name?: string;
  description?: string;
  style?: string;
  raw_text?: string;
  title?: string;
  [k: string]: unknown;
}

export interface MessageItemData {
  style?: string;
  text?: string;
}

export interface ToolItemData {
  tool_call_id?: string | null;
  tool_name?: string;
  label?: string;
  args?: string | Record<string, unknown>;
  raw_args?: Record<string, unknown>;
  diff_text?: string;
  detail?: string;
  ok?: boolean;
  elapsed?: number | null;
}

export interface ThoughtItemData {
  text?: string;
  meta?: string | null;
  elapsed?: number | null;
}

export interface NoticeItemData {
  style?: string;
  text?: string;
}

export interface DiffItemData {
  text?: string;
  title?: string;
}

export interface StatusItemData {
  status_id?: string;
  label?: string;
  detail?: string;
  ok?: boolean;
  outcome?: string;
}

export interface TodoItem {
  status: string;
  content: string;
}

export interface TranscriptSnapshot {
  nodes: TranscriptNode[];
  thread_id?: string;
  revision?: number;
  windowed?: boolean;
  before_turn_id?: number | null;
  after_turn_id?: number | null;
  has_earlier?: boolean;
  has_later?: boolean;
}

export type ByIdMap = Map<string, TranscriptNode>;

export const TOOL_GROUP_PREVIEW_LIMIT = 3;

export interface ToolInfo {
  tool_name: string;
}
