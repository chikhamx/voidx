/* generated from protocol.schema.json */

export type VoidxUiProtocol =
  | AgentCatalogDto
  | AgentCatalogEdgeDto
  | AgentCatalogIntegrationDto
  | AgentCatalogNodeDto
  | AgentCatalogToolDto
  | AgentProfileDetailDto
  | AgentProfileDiagnosticDto
  | AgentProfileInfoDto
  | AgentProfileListDto
  | AgentProfileSaveDto
  | AgentProfileSnapshotDto
  | AgentProfileValidationDto
  | JsonRpcRequest
  | JsonRpcNotification
  | JsonRpcResult
  | JsonRpcError
  | ErrorPayload
  | ClientCapabilities
  | GatewayCapabilities
  | WorkspacePatch
  | StreamAppendDelta
  | WorkspaceSnapshot
  | ThreadSnapshot
  | ThreadInfo
  | TurnInfo
  | Item
  | TranscriptSnapshot
  | (UiChoiceRequest | UiTextRequest | UiPermissionRequest)
  | (UiSubmitCommand | UiCancelCommand);
export type Description = string;
export type Name = string;
export type BuiltinNodes = AgentCatalogNodeDto[];
export type Condition = string;
export type Label = string;
export type Source = string;
export type Target = string;
export type DefaultEdges = AgentCatalogEdgeDto[];
export type Description1 = string;
export type Name1 = string;
export type McpServers = AgentCatalogIntegrationDto[];
export type Skills = AgentCatalogIntegrationDto[];
export type Description2 = string;
export type Id = string;
export type Tools = AgentCatalogToolDto[];
export type Availability = "available" | "unavailable";
export type ContentHash = string;
export type Code = string;
export type Message = string;
export type Path = string;
export type Severity = "error" | "warning";
export type Diagnostics = AgentProfileDiagnosticDto[];
export type DisplayName = string;
export type HitlMode = "interactive" | "autonomous";
export type Name2 = string;
export type Revision = number;
export type RunMode = string;
export type Source1 = "bundled" | "global" | "project";
export type ReadOnly = boolean;
export type Yaml = string;
export type Profiles = AgentProfileInfoDto[];
export type Diagnostics1 = AgentProfileDiagnosticDto[];
export type ContentHash1 = string;
export type ProfileId = string;
export type Revision1 = number;
export type SnapshotHash = string;
export type Source2 = "bundled" | "global" | "project";
export type Diagnostics2 = AgentProfileDiagnosticDto[];
export type Valid = boolean;
export type Id1 = number | string;
export type Jsonrpc = string;
export type Method = string;
export type Jsonrpc1 = string;
export type Method1 = string;
export type Id2 = number | string;
export type Jsonrpc2 = string;
export type Code1 = number;
export type Data = {
  [k: string]: unknown;
} | null;
export type Message1 = string;
export type Id3 = number | string | null;
export type Jsonrpc3 = string;
export type Capabilities = string[];
export type Protocol = string;
export type Capabilities1 = string[];
export type Protocol1 = string;
export type Revision2 = number;
export type ActiveThreadId = string;
export type AiApprovalCount = number;
export type Model = string;
export type PermissionMode = string;
export type ProfileConfigured = boolean | null;
export type Provider = string;
export type Revision3 = number;
export type CreatedAt = string;
export type Directory = string;
export type MessageCount = number;
export type ModelName = string;
export type ModelProvider = string;
export type RuntimeProfile = string;
export type Status = "idle" | "running" | "waiting_for_user" | "waiting_for_write_lock" | "cancelling" | "failed";
export type Temporary = boolean;
export type ThreadId = string;
export type Title = string;
export type UpdatedAt = string;
export type Workspace = string;
export type Threads = ThreadInfo[];
export type Workspace1 = string;
export type BaseRevision = number;
export type ItemId = string;
export type Phase = string;
export type Revision4 = number;
export type StreamId = string;
export type Text = string;
export type ThreadId1 = string;
export type TurnId = string;
export type WorkspaceRevision = number;
export type AfterTurnId = number | null;
export type BeforeTurnId = number | null;
export type HasEarlier = boolean;
export type HasLater = boolean;
export type AgentName = string | null;
export type AgentRunId = string | null;
export type BodyLines = string[];
export type ChildIds = string[];
export type Collapsed = boolean;
export type Elapsed = number | null;
export type Header = string;
export type HeaderStyle = string;
export type Id4 = string;
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
export type Status1 = "running" | "done" | "error";
export type StepInfo = string | null;
export type Title1 = string;
export type ToolCallId = string | null;
export type Nodes = TranscriptNode[];
export type Revision5 = number;
export type ThreadId2 = string;
export type Windowed = boolean;
export type ActiveThreadId1 = string;
export type AiApprovalCount1 = number;
export type Model1 = string;
export type PermissionMode1 = string;
export type ProfileConfigured1 = boolean | null;
export type Provider1 = string;
export type Revision6 = number;
export type Threads1 = ThreadInfo[];
export type Workspace2 = string;
export type Elapsed1 = number | null;
export type StartedAt = number;
export type Status2 = "running" | "completed" | "cancelled" | "failed";
export type ThreadId3 = string;
export type TurnId1 = string;
export type ItemId1 = string;
export type Kind = "message" | "assistant_stream" | "tool" | "todo" | "subagent" | "status" | "prompt";
export type Lifecycle = "started" | "delta" | "completed";
export type ThreadId4 = string;
export type TurnId2 = string;
export type Nodes1 = TranscriptNode[];
export type Revision7 = number;
export type RootId = string;
export type SessionId = string;
export type Choices = [unknown, unknown, unknown][];
export type Kind1 = "choice";
export type Prompt = string;
export type RequestId = string;
export type ThreadId5 = string;
export type Default = string;
export type Kind2 = "text";
export type Prompt1 = string;
export type RequestId1 = string;
export type Secret = boolean;
export type ThreadId6 = string;
export type Choices1 = [unknown, unknown, unknown][];
export type Kind3 = "permission";
export type Prompt2 = string;
export type RequestId2 = string;
export type ThreadId7 = string;
export type AiApprovalFailure = string;
export type AllowedScopes = string[];
export type DefaultScope = string | null;
export type Name3 = string;
export type Pattern = string;
export type Risk = {
  [k: string]: unknown;
} | null;
export type Tools1 = PermissionToolDetail[];
export type Kind4 = "submit";
export type RuntimeProfile1 = string;
export type SessionId1 = string;
export type Text1 = string;
export type ThreadId8 = string;
export type Workspace3 = string;
export type Kind5 = "cancel";
export type ThreadId9 = string;

export interface AgentCatalogDto {
  builtin_nodes?: BuiltinNodes;
  default_edges?: DefaultEdges;
  mcp_servers?: McpServers;
  skills?: Skills;
  tools?: Tools;
  [k: string]: unknown;
}
export interface AgentCatalogNodeDto {
  description: Description;
  name: Name;
  [k: string]: unknown;
}
export interface AgentCatalogEdgeDto {
  condition: Condition;
  label?: Label;
  source: Source;
  target: Target;
  [k: string]: unknown;
}
/**
 * Checkbox-list entry for skills / MCP servers (UI consumes name+description).
 */
export interface AgentCatalogIntegrationDto {
  description: Description1;
  name: Name1;
  [k: string]: unknown;
}
export interface AgentCatalogToolDto {
  description: Description2;
  id: Id;
  [k: string]: unknown;
}
export interface AgentProfileDetailDto {
  profile: AgentProfileInfoDto;
  read_only: ReadOnly;
  yaml: Yaml;
  [k: string]: unknown;
}
export interface AgentProfileInfoDto {
  availability: Availability;
  content_hash: ContentHash;
  diagnostics?: Diagnostics;
  display_name: DisplayName;
  hitl_mode: HitlMode;
  name: Name2;
  revision: Revision;
  run_mode: RunMode;
  source: Source1;
  [k: string]: unknown;
}
export interface AgentProfileDiagnosticDto {
  code: Code;
  message: Message;
  path: Path;
  severity?: Severity;
  [k: string]: unknown;
}
export interface AgentProfileListDto {
  profiles?: Profiles;
  [k: string]: unknown;
}
export interface AgentProfileSaveDto {
  diagnostics?: Diagnostics1;
  snapshot: AgentProfileSnapshotDto;
  [k: string]: unknown;
}
export interface AgentProfileSnapshotDto {
  content_hash: ContentHash1;
  profile_id: ProfileId;
  revision: Revision1;
  snapshot_hash: SnapshotHash;
  source: Source2;
  [k: string]: unknown;
}
export interface AgentProfileValidationDto {
  diagnostics?: Diagnostics2;
  snapshot?: AgentProfileSnapshotDto | null;
  valid: Valid;
  [k: string]: unknown;
}
export interface JsonRpcRequest {
  id: Id1;
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
  id: Id2;
  jsonrpc?: Jsonrpc2;
  result: Result;
  [k: string]: unknown;
}
export interface Result {
  [k: string]: unknown;
}
export interface JsonRpcError {
  error: ErrorPayload;
  id: Id3;
  jsonrpc?: Jsonrpc3;
  [k: string]: unknown;
}
export interface ErrorPayload {
  code: Code1;
  data?: Data;
  message: Message1;
  [k: string]: unknown;
}
/**
 * Capabilities announced by a UI client after the socket opens.
 */
export interface ClientCapabilities {
  capabilities?: Capabilities;
  protocol?: Protocol;
  [k: string]: unknown;
}
/**
 * Capabilities supported by this Gateway instance.
 */
export interface GatewayCapabilities {
  capabilities?: Capabilities1;
  protocol?: Protocol1;
  revision?: Revision2;
  [k: string]: unknown;
}
/**
 * Metadata-only workspace update.
 *
 * It deliberately has no transcript field: a patch cannot implicitly delete
 * or replace canonical transcript items. A revision gap requires a snapshot.
 */
export interface WorkspacePatch {
  active_thread_id?: ActiveThreadId;
  ai_approval_count?: AiApprovalCount;
  model?: Model;
  permission_mode?: PermissionMode;
  profile_configured?: ProfileConfigured;
  provider?: Provider;
  revision: Revision3;
  runtime?: Runtime;
  threads?: Threads;
  workspace?: Workspace1;
  workspace_write_lock?: WorkspaceWriteLock;
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
  runtime_profile?: RuntimeProfile;
  status?: Status;
  temporary?: Temporary;
  thread_id: ThreadId;
  title?: Title;
  updated_at?: UpdatedAt;
  workspace?: Workspace;
  [k: string]: unknown;
}
export interface WorkspaceWriteLock {
  [k: string]: unknown;
}
/**
 * A contiguous append to one assistant stream.
 */
export interface StreamAppendDelta {
  base_revision: BaseRevision;
  item_id: ItemId;
  phase?: Phase;
  revision: Revision4;
  stream_id: StreamId;
  text: Text;
  thread_id: ThreadId1;
  turn_id: TurnId;
  workspace_revision?: WorkspaceRevision;
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
  active_thread_id?: ActiveThreadId1;
  ai_approval_count?: AiApprovalCount1;
  model?: Model1;
  permission_mode?: PermissionMode1;
  profile_configured?: ProfileConfigured1;
  provider?: Provider1;
  revision?: Revision6;
  runtime?: Runtime1;
  threads?: Threads1;
  workspace?: Workspace2;
  workspace_write_lock?: WorkspaceWriteLock1;
  [k: string]: unknown;
}
/**
 * Transcript snapshot for a single thread.
 */
export interface ThreadSnapshot {
  after_turn_id?: AfterTurnId;
  before_turn_id?: BeforeTurnId;
  has_earlier?: HasEarlier;
  has_later?: HasLater;
  nodes?: Nodes;
  revision?: Revision5;
  thread_id: ThreadId2;
  windowed?: Windowed;
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
  id: Id4;
  message_id?: MessageId;
  meta?: Meta;
  node_type: NodeType;
  parent_id?: ParentId;
  payload?: Payload;
  status?: Status1;
  step_info?: StepInfo;
  title?: Title1;
  tool_call_id?: ToolCallId;
  [k: string]: unknown;
}
export interface Payload {
  [k: string]: unknown;
}
export interface Runtime1 {
  [k: string]: unknown;
}
export interface WorkspaceWriteLock1 {
  [k: string]: unknown;
}
export interface TurnInfo {
  elapsed?: Elapsed1;
  started_at?: StartedAt;
  status?: Status2;
  thread_id: ThreadId3;
  turn_id: TurnId1;
  [k: string]: unknown;
}
export interface Item {
  data?: Data1;
  item_id: ItemId1;
  kind: Kind;
  lifecycle?: Lifecycle;
  thread_id: ThreadId4;
  turn_id: TurnId2;
  [k: string]: unknown;
}
export interface Data1 {
  [k: string]: unknown;
}
export interface TranscriptSnapshot {
  nodes?: Nodes1;
  revision?: Revision7;
  root_id?: RootId;
  session_id?: SessionId;
  [k: string]: unknown;
}
export interface UiChoiceRequest {
  choices?: Choices;
  kind?: Kind1;
  prompt: Prompt;
  request_id: RequestId;
  thread_id?: ThreadId5;
  [k: string]: unknown;
}
export interface UiTextRequest {
  default?: Default;
  kind?: Kind2;
  prompt: Prompt1;
  request_id: RequestId1;
  secret?: Secret;
  thread_id?: ThreadId6;
  [k: string]: unknown;
}
export interface UiPermissionRequest {
  choices?: Choices1;
  kind?: Kind3;
  prompt: Prompt2;
  request_id: RequestId2;
  thread_id?: ThreadId7;
  tools?: Tools1;
  [k: string]: unknown;
}
export interface PermissionToolDetail {
  ai_approval_failure?: AiApprovalFailure;
  allowed_scopes?: AllowedScopes;
  args?: Args;
  default_scope?: DefaultScope;
  name: Name3;
  pattern?: Pattern;
  risk?: Risk;
  [k: string]: unknown;
}
export interface Args {
  [k: string]: unknown;
}
export interface UiSubmitCommand {
  kind?: Kind4;
  runtime_profile?: RuntimeProfile1;
  session_id?: SessionId1;
  text: Text1;
  thread_id?: ThreadId8;
  workspace?: Workspace3;
  [k: string]: unknown;
}
export interface UiCancelCommand {
  kind?: Kind5;
  thread_id?: ThreadId9;
  [k: string]: unknown;
}
