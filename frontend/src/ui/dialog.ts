import { isRpcConnected, rpcCall, rpcRespond } from "../rpc";
import {
  requestDialogEl,
  requestTitleEl,
  requestDetailsEl,
  requestControlsEl,
} from "../services/state";

export interface PermissionToolDetail {
  name: string;
  pattern?: string;
  args?: Record<string, unknown>;
  risk?: {
    level?: string;
    tags?: string[];
    reason?: string;
    tool_name?: string;
    pattern?: string;
  } | null;
  allowed_scopes?: string[];
  default_scope?: string | null;
}

export interface UiRequest {
  prompt: string;
  kind: string;
  request_id: string;
  thread_id?: string;
  tools?: PermissionToolDetail[];
  choices?: [string, string, string][];
  default?: string;
  secret?: boolean;
  response_method?: string;
}

export const pendingUiRequests: UiRequest[] = [];

export function _resetDialogForTest(): void {
  pendingUiRequests.length = 0;
  requestDialogEl.dataset.responseMethod = "";
  requestDialogEl.dataset.responseThreadId = "";
  requestDialogEl.close();
}

export function showRequest(request: Record<string, unknown>): void {
  const req = request as unknown as UiRequest;
  if (requestDialogEl.open) {
    pendingUiRequests.push(req);
    return;
  }
  renderRequest(req);
}

export function renderRequest(req: UiRequest): void {
  requestDialogEl.dataset.responseMethod = req.response_method || "";
  requestDialogEl.dataset.responseThreadId = req.thread_id || "";
  requestTitleEl.textContent = req.prompt;
  requestDetailsEl.replaceChildren();
  requestControlsEl.replaceChildren();

  if (req.kind === "permission") {
    renderPermissionDetails(req);
    renderChoiceButtons(req);
  } else if (req.kind === "choice") {
    requestDetailsEl.className = "";
    renderChoiceButtons(req);
  } else if (req.kind === "text") {
    requestDetailsEl.className = "";
    renderTextRequest(req);
  }

  requestDialogEl.showModal();
}

export function showNextQueuedRequest(): void {
  const next = pendingUiRequests.shift();
  if (!next) {
    return;
  }
  renderRequest(next);
}

export function showPromptItemRequest(data: Record<string, unknown>): void {
  const promptType = data.prompt_type as string;
  if (promptType === "permission") {
    if (!data.request_id || data.interactive === false) {
      return;
    }
    showRequest({
      kind: "permission",
      request_id: (data.request_id as string) || "permission",
      thread_id: (data.thread_id as string) || "",
      prompt: (data.prompt as string) || "Allow action?",
      choices: data.choices || [],
      tools: data.tools || [],
      response_method: "session.respond",
    });
    return;
  }
  if (promptType === "clarify") {
    const options = ((data.options as string[]) || []).map((option) => [
      option,
      option,
      option,
    ]);
    showRequest({
      kind: "choice",
      request_id: (data.clarify_id as string) || (data.request_id as string) || "clarify",
      thread_id: (data.thread_id as string) || "",
      prompt: (data.question as string) || "Clarify",
      choices: options,
      response_method: "session.respond",
    });
    return;
  }
  if (promptType === "checkpoint") {
    const plan = (data.plan as Record<string, unknown>) || {};
    const choices = ((data.choices as Array<Record<string, unknown>>) || []).map((choice) => [
      (choice.label as string) || (choice.value as string) || "",
      (choice.value as string) || (choice.label as string) || "",
      (choice.description as string) || (choice.label as string) || (choice.value as string) || "",
    ]);
    showRequest({
      kind: "choice",
      request_id: (data.checkpoint_id as string) || (data.request_id as string) || "checkpoint",
      thread_id: (data.thread_id as string) || "",
      prompt: (plan.plan_summary as string) || "Review plan",
      choices,
      response_method: "session.respond",
    });
  }
}

export function renderPermissionDetails(request: UiRequest): void {
  requestDetailsEl.className = "request-details";
  requestDetailsEl.replaceChildren();
  if (!request.tools?.length) {
    return;
  }
  for (const tool of request.tools) {
    const block = document.createElement("section");
    block.className = "request-tool-detail";

    const title = document.createElement("div");
    title.className = "request-tool-title";
    title.textContent = [tool.name, tool.pattern].filter(Boolean).join(" ");
    block.append(title);

    if (tool.risk) {
      const risk = document.createElement("div");
      risk.className = `request-risk request-risk-${tool.risk.level || "unknown"}`;
      const tags = tool.risk.tags?.length ? ` · ${tool.risk.tags.join(", ")}` : "";
      risk.textContent = `Risk: ${tool.risk.level || "unknown"}${tags}`;
      block.append(risk);
      if (tool.risk.reason) {
        const reason = document.createElement("div");
        reason.className = "request-risk-reason";
        reason.textContent = tool.risk.reason;
        block.append(reason);
      }
    }

    const isBlockedRisk = tool.risk?.level === "blocked";
    if (!isBlockedRisk && tool.allowed_scopes?.length) {
      const scopes = document.createElement("div");
      scopes.className = "request-approval-scopes";
      scopes.textContent = `Allowed scopes: ${tool.allowed_scopes.join(", ")}`;
      block.append(scopes);
    }

    if (!isBlockedRisk && tool.default_scope) {
      const defaultScope = document.createElement("div");
      defaultScope.className = "request-default-scope";
      defaultScope.textContent = `Default scope: ${tool.default_scope}`;
      block.append(defaultScope);
    }

    const args = document.createElement("pre");
    args.className = "request-tool-args";
    args.textContent = JSON.stringify(tool.args || {}, null, 2);
    block.append(args);
    requestDetailsEl.append(block);
  }
}

export function renderChoiceButtons(request: UiRequest): void {
  const actions = document.createElement("div");
  actions.className = "request-actions";
  for (const [label, value, desc] of request.choices || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = desc || label;
    button.addEventListener("click", () =>
      sendResponse(request.request_id, value),
    );
    actions.append(button);
  }
  const acknowledgementOnly =
    request.kind === "permission" &&
    (request.choices || []).length === 1 &&
    (request.choices || [])[0]?.[1] === "n";
  if (acknowledgementOnly) {
    requestControlsEl.append(actions);
    return;
  }
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () =>
    sendResponse(request.request_id, null),
  );
  actions.append(cancel);
  requestControlsEl.append(actions);
}

export function renderTextRequest(request: UiRequest): void {
  const input = document.createElement("textarea");
  input.rows = 3;
  input.value = request.default || "";
  input.placeholder = request.secret ? "Input hidden in terminal UI" : "";
  const actions = document.createElement("div");
  actions.className = "request-actions";
  const submit = document.createElement("button");
  submit.type = "button";
  submit.textContent = "Submit";
  submit.addEventListener("click", () =>
    sendResponse(request.request_id, input.value),
  );
  actions.append(submit);
  requestControlsEl.append(input, actions);
  setTimeout(() => input.focus(), 0);
}

export function sendResponse(requestId: string, value: unknown): void {
  if (!isRpcConnected()) {
    return;
  }
  const responseMethod = requestDialogEl.dataset.responseMethod || "";
  if (responseMethod) {
    const threadId = requestDialogEl.dataset.responseThreadId || "";
    const params: Record<string, unknown> = { request_id: requestId, value };
    if (threadId) {
      params.thread_id = threadId;
    }
    rpcCall(responseMethod, params).catch(() => {});
    requestDialogEl.dataset.responseMethod = "";
    requestDialogEl.dataset.responseThreadId = "";
    requestDialogEl.close();
    showNextQueuedRequest();
    return;
  }
  rpcRespond(requestId, value);
  requestDialogEl.close();
  showNextQueuedRequest();
}
