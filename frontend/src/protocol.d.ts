/* generated from protocol.schema.json */

export type VoidxUiProtocol =
  | JsonRpcRequest
  | JsonRpcNotification
  | JsonRpcResult
  | JsonRpcError
  | ErrorPayload
  | WorkspaceSnapshot
  | ThreadSnapshot
  | ThreadInfo
  | TurnInfo
  | Item
  | TranscriptSnapshot
  | (UiChoiceRequest | UiTextRequest | UiPermissionRequest)
  | (UiSubmitCommand | UiCancelCommand);
export type Id = number | string;
export type Jsonrpc = string;
export type Method = string;
export type Jsonrpc1 = string;
export type Method1 = string;
export type Id1 = number | string;
export type Jsonrpc2 = string;
export type Code = number;
export type Data = {
  [k: string]: unknown;
} | null;
export type Message = string;
export type Id2 = number | string | null;
export type Jsonrpc3 = string;
export type AgentName = string | null;
export type AgentRunId = string | null;
export type BodyLines = string[];
export type ChildIds = string[];
export type Collapsed = boolean;
export type Elapsed = number | null;
export type Header = string;
export type HeaderStyle = string;
export type Id3 = string;
export type MessageId = number | null;
export type Meta = string | null;
export type NodeType =
  | "root"
  | "startup"
  | "turn"
  | "tool_call"
  | "tool_result"
  | "todo"
  | "subagent"
  | "message"
  | "assistant"
  | "thought"
  | "status"
  | "permission"
  | "checkpoint"
  | "error"
  | "warn"
  | "diff";
export type ParentId = string | null;
export type Status = "running" | "done" | "error";
export type StepInfo = string | null;
export type Title = string;
export type ToolCallId = string | null;
export type Nodes = TranscriptNode[];
export type Revision = number;
export type ThreadId = string;
export type ActiveThreadId = string;
export type Model = string;
export type ProfileConfigured = boolean | null;
export type Provider = string;
export type CreatedAt = string;
export type Directory = string;
export type MessageCount = number;
export type ModelName = string;
export type ModelProvider = string;
export type Status1 = "idle" | "running" | "waiting_for_user" | "waiting_for_write_lock" | "cancelling" | "failed";
export type ThreadId1 = string;
export type Title1 = string;
export type UpdatedAt = string;
export type Workspace = string;
export type Threads = ThreadInfo[];
export type Workspace1 = string;
export type Elapsed1 = number | null;
export type StartedAt = number;
export type Status2 = "running" | "completed" | "cancelled" | "failed";
export type ThreadId2 = string;
export type TurnId = string;
export type ItemId = string;
export type Kind = "message" | "assistant_stream" | "tool" | "todo" | "subagent" | "status" | "prompt";
export type Lifecycle = "started" | "delta" | "completed";
export type ThreadId3 = string;
export type TurnId1 = string;
export type Nodes1 = TranscriptNode[];
export type Revision1 = number;
export type RootId = string;
export type SessionId = string;
export type Choices = [unknown, unknown, unknown][];
export type Kind1 = "choice";
export type Prompt = string;
export type RequestId = string;
export type ThreadId4 = string;
export type Default = string;
export type Kind2 = "text";
export type Prompt1 = string;
export type RequestId1 = string;
export type Secret = boolean;
export type ThreadId5 = string;
export type Choices1 = [unknown, unknown, unknown][];
export type Kind3 = "permission";
export type Prompt2 = string;
export type RequestId2 = string;
export type ThreadId6 = string;
export type Name = string;
export type Pattern = string;
export type Tools = PermissionToolDetail[];
export type Kind4 = "submit";
export type Text = string;
export type ThreadId7 = string;
export type Kind5 = "cancel";
export type ThreadId8 = string;

export interface JsonRpcRequest {
  id: Id;
  jsonrpc?: Jsonrpc;
  method: Method;
  params?: Params;
  [k: string]: unknown;
}
export interface Params {
  [k: string]: unknown;
}
export interface JsonRpcNotification {
  jsonrpc?: Jsonrpc1;
  method: Method1;
  params?: Params1;
  [k: string]: unknown;
}
export interface Params1 {
  [k: string]: unknown;
}
export interface JsonRpcResult {
  id: Id1;
  jsonrpc?: Jsonrpc2;
  result: Result;
  [k: string]: unknown;
}
export interface Result {
  [k: string]: unknown;
}
export interface JsonRpcError {
  error: ErrorPayload;
  id: Id2;
  jsonrpc?: Jsonrpc3;
  [k: string]: unknown;
}
export interface ErrorPayload {
  code: Code;
  data?: Data;
  message: Message;
  [k: string]: unknown;
}
/**
 * Full workspace state pushed on connect / refresh.
 *
 * Only the active thread carries a complete transcript snapshot; other
 * threads are listed as ThreadInfo metadata only.
 */
export interface WorkspaceSnapshot {
  active_snapshot?: ThreadSnapshot | null;
  active_thread_id?: ActiveThreadId;
  model?: Model;
  profile_configured?: ProfileConfigured;
  provider?: Provider;
  runtime?: Runtime;
  threads?: Threads;
  workspace?: Workspace1;
  workspace_write_lock?: WorkspaceWriteLock;
  [k: string]: unknown;
}
/**
 * Transcript snapshot for a single thread.
 */
export interface ThreadSnapshot {
  nodes?: Nodes;
  revision?: Revision;
  thread_id: ThreadId;
  [k: string]: unknown;
}
export interface TranscriptNode {
  agent_name?: AgentName;
  agent_run_id?: AgentRunId;
  body_lines?: BodyLines;
  child_ids?: ChildIds;
  collapsed?: Collapsed;
  elapsed?: Elapsed;
  header?: Header;
  header_style?: HeaderStyle;
  id: Id3;
  message_id?: MessageId;
  meta?: Meta;
  node_type: NodeType;
  parent_id?: ParentId;
  payload?: Payload;
  status?: Status;
  step_info?: StepInfo;
  title?: Title;
  tool_call_id?: ToolCallId;
  [k: string]: unknown;
}
export interface Payload {
  [k: string]: unknown;
}
export interface Runtime {
  [k: string]: unknown;
}
export interface ThreadInfo {
  created_at?: CreatedAt;
  directory?: Directory;
  message_count?: MessageCount;
  model_name?: ModelName;
  model_provider?: ModelProvider;
  status?: Status1;
  thread_id: ThreadId1;
  title?: Title1;
  updated_at?: UpdatedAt;
  workspace?: Workspace;
  [k: string]: unknown;
}
export interface WorkspaceWriteLock {
  [k: string]: unknown;
}
export interface TurnInfo {
  elapsed?: Elapsed1;
  started_at?: StartedAt;
  status?: Status2;
  thread_id: ThreadId2;
  turn_id: TurnId;
  [k: string]: unknown;
}
export interface Item {
  data?: Data1;
  item_id: ItemId;
  kind: Kind;
  lifecycle?: Lifecycle;
  thread_id: ThreadId3;
  turn_id: TurnId1;
  [k: string]: unknown;
}
export interface Data1 {
  [k: string]: unknown;
}
export interface TranscriptSnapshot {
  nodes?: Nodes1;
  revision?: Revision1;
  root_id?: RootId;
  session_id?: SessionId;
  [k: string]: unknown;
}
export interface UiChoiceRequest {
  choices?: Choices;
  kind?: Kind1;
  prompt: Prompt;
  request_id: RequestId;
  thread_id?: ThreadId4;
  [k: string]: unknown;
}
export interface UiTextRequest {
  default?: Default;
  kind?: Kind2;
  prompt: Prompt1;
  request_id: RequestId1;
  secret?: Secret;
  thread_id?: ThreadId5;
  [k: string]: unknown;
}
export interface UiPermissionRequest {
  choices?: Choices1;
  kind?: Kind3;
  prompt: Prompt2;
  request_id: RequestId2;
  thread_id?: ThreadId6;
  tools?: Tools;
  [k: string]: unknown;
}
export interface PermissionToolDetail {
  args?: Args;
  name: Name;
  pattern?: Pattern;
  [k: string]: unknown;
}
export interface Args {
  [k: string]: unknown;
}
export interface UiSubmitCommand {
  kind?: Kind4;
  text: Text;
  thread_id?: ThreadId7;
  [k: string]: unknown;
}
export interface UiCancelCommand {
  kind?: Kind5;
  thread_id?: ThreadId8;
  [k: string]: unknown;
}
