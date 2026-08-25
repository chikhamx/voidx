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
  ai_approval_failure?: string;
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
  requestDialogEl.dataset.requestKind = "";
  requestDialogEl.dataset.requestId = "";
  requestDialogEl.dataset.responseMethod = "";
  requestDialogEl.dataset.responseThreadId = "";
  requestDialogEl.close();
}

export function clearPermissionRequests(requestId?: string): void {
  for (let index = pendingUiRequests.length - 1; index >= 0; index -= 1) {
    const request = pendingUiRequests[index];
    if (
      request.kind === "permission" &&
      (!requestId || request.request_id === requestId)
    ) {
      pendingUiRequests.splice(index, 1);
    }
  }

  if (
    requestDialogEl.dataset.requestKind !== "permission" ||
    (requestId && requestDialogEl.dataset.requestId !== requestId)
  ) {
    return;
  }

  requestDialogEl.dataset.requestKind = "";
  requestDialogEl.dataset.requestId = "";
  requestDialogEl.dataset.responseMethod = "";
  requestDialogEl.dataset.responseThreadId = "";
  requestDialogEl.close();
  showNextQueuedRequest();
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
  requestDialogEl.dataset.requestKind = req.kind;
  requestDialogEl.dataset.requestId = req.request_id || "";
  requestDialogEl.dataset.responseMethod = req.response_method || "";
  requestDialogEl.dataset.responseThreadId = req.thread_id || "";
  requestTitleEl.textContent = req.kind === "permission" ? "权限审批" : req.prompt;
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
    return;
  }
  if (promptType === "goal_spec") {
    const spec = (data.spec as Record<string, unknown>) || {};
    const choices = ((data.choices as Array<Record<string, unknown>>) || []).map((choice) => [
      (choice.label as string) || (choice.value as string) || "",
      (choice.value as string) || (choice.label as string) || "",
      (choice.description as string) || (choice.label as string) || (choice.value as string) || "",
    ]);
    const lines = [
      `Goal: ${(spec.objective as string) || ""}`,
      `Acceptance: ${(spec.acceptance_condition as string) || ""}`,
    ];
    if (spec.achievement_method) {
      lines.push(`Method: ${spec.achievement_method as string}`);
    }
    lines.push(`Max attempts: ${(spec.max_attempts as number) || 20}`);
    showRequest({
      kind: "choice",
      request_id: (data.prompt_id as string) || (data.request_id as string) || "goal_spec",
      thread_id: (data.thread_id as string) || "",
      prompt: lines.join("\n"),
      choices,
      response_method: "session.respond",
    });
  }
}

export function renderPermissionDetails(request: UiRequest): void {
  requestDetailsEl.className = "request-details request-permission-details";
  requestDetailsEl.replaceChildren();

  const question = document.createElement("section");
  question.className = "request-permission-question";
  question.dataset.permissionSection = "question";
  question.textContent = request.prompt || "是否允许执行此操作？";
  requestDetailsEl.append(question);

  const tools = request.tools || [];
  if (tools.length === 0) {
    return;
  }

  const execution = document.createElement("section");
  execution.className = "request-permission-section request-execution";
  execution.dataset.permissionSection = "execution";
  const executionTitle = document.createElement("h3");
  executionTitle.textContent = "将执行";
  execution.append(executionTitle);

  for (const tool of tools) {
    const riskLevel = tool.risk?.level || "default";
    const block = document.createElement("div");
    block.className = `request-tool-detail request-tool-risk-${riskLevel}`;

    const heading = document.createElement("div");
    heading.className = "request-tool-heading";
    const title = document.createElement("div");
    title.className = "request-tool-title";
    title.textContent = tool.name;
    heading.append(title);
    const command = typeof tool.args?.command === "string" ? tool.args.command : "";
    const executionText = tool.pattern || command;
    if (executionText) {
      const pattern = document.createElement("div");
      pattern.className = "request-tool-pattern";
      pattern.textContent = executionText;
      heading.append(pattern);
    }
    block.append(heading);
    execution.append(block);
  }
  requestDetailsEl.append(execution);

  const riskSection = document.createElement("section");
  riskSection.className = "request-permission-section request-risk-section";
  riskSection.dataset.permissionSection = "risk";
  const riskTitle = document.createElement("h3");
  riskTitle.textContent = "风险";
  riskSection.append(riskTitle);
  for (const tool of tools) {
    const riskLevel = tool.risk?.level || "default";
    const risk = document.createElement("div");
    risk.className = `request-risk request-risk-${riskLevel}`;
    const tags = tool.risk?.tags?.length ? ` · ${tool.risk.tags.join(", ")}` : "";
    risk.textContent = `${riskLevel}${tags}`;
    riskSection.append(risk);
    if (tool.risk?.reason) {
      const reason = document.createElement("div");
      reason.className = "request-risk-reason";
      reason.textContent = tool.risk.reason;
      riskSection.append(reason);
    }
    if (tool.ai_approval_failure) {
      const aiApproval = document.createElement("div");
      aiApproval.className = "request-risk-reason";
      aiApproval.textContent = tool.ai_approval_failure;
      riskSection.append(aiApproval);
    }
  }
  requestDetailsEl.append(riskSection);

  const scopeSection = document.createElement("section");
  scopeSection.className = "request-permission-section request-scope-section";
  scopeSection.dataset.permissionSection = "scope";
  const scopeTitle = document.createElement("h3");
  scopeTitle.textContent = "授权范围";
  scopeSection.append(scopeTitle);

  let hasScopeInformation = false;
  for (const tool of tools) {
    if (tool.risk?.level === "blocked") continue;
    if (tool.allowed_scopes?.length) {
      const scopes = document.createElement("div");
      scopes.className = "request-approval-scopes";
      scopes.textContent = `可授权：${tool.allowed_scopes.join(", ")}`;
      scopeSection.append(scopes);
      hasScopeInformation = true;
    }
    if (tool.default_scope) {
      const defaultScope = document.createElement("div");
      defaultScope.className = "request-default-scope";
      defaultScope.textContent = `默认范围：${tool.default_scope}`;
      scopeSection.append(defaultScope);
      hasScopeInformation = true;
    }
  }
  if (!hasScopeInformation) {
    const scopeFallback = document.createElement("div");
    scopeFallback.className = "request-default-scope";
    scopeFallback.textContent = "未提供额外授权范围";
    scopeSection.append(scopeFallback);
  }
  requestDetailsEl.append(scopeSection);

  const parameters = document.createElement("details");
  parameters.className = "request-parameters";
  parameters.dataset.permissionSection = "parameters";
  const summary = document.createElement("summary");
  summary.textContent = "参数详情";
  parameters.append(summary);
  for (const tool of tools) {
    const args = document.createElement("pre");
    args.className = "request-tool-args";
    args.textContent = JSON.stringify(tool.args || {}, null, 2);
    parameters.append(args);
  }
  requestDetailsEl.append(parameters);
}

export function renderChoiceButtons(request: UiRequest): void {
  const actions = document.createElement("div");
  actions.className = "request-actions";
  for (const [label, value, desc] of request.choices || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "request-choice";
    const labelEl = document.createElement("span");
    labelEl.className = "request-choice-label";
    labelEl.textContent = label;
    const descriptionEl = document.createElement("span");
    descriptionEl.className = "request-choice-description";
    descriptionEl.textContent = desc || "";
    button.append(labelEl, descriptionEl);
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
  cancel.className = "request-choice-cancel";
  cancel.textContent = "取消";
  cancel.addEventListener("click", () =>
    sendResponse(request.request_id, null),
  );
  actions.append(cancel);
  requestControlsEl.append(actions);
}


export function renderTextRequest(request: UiRequest): void {
  const input = request.secret
    ? (() => {
        const field = document.createElement("input");
        field.type = "password";
        field.autocomplete = "off";
        return field;
      })()
    : (() => {
        const field = document.createElement("textarea");
        field.rows = 3;
        return field;
      })();
  input.value = request.default || "";
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
