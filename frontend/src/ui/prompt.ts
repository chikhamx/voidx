import { renderMarkdown } from "../utils/markdown";
import {
  getTranscriptElement,
  requestTranscriptFollowAfterMutation,
} from "../utils/stream";

export type ConversationPromptType = "clarify" | "checkpoint" | "goal_spec";

export interface ConversationPromptChoice {
  label: string;
  value: string;
  description: string;
}

export interface ConversationPrompt {
  itemId: string;
  requestId: string;
  threadId: string;
  type: ConversationPromptType;
  text: string;
  choices: ConversationPromptChoice[];
  element: HTMLElement;
  submitting: boolean;
  onReply: ConversationPromptReplyHandler;
}

export type ConversationPromptReplyHandler = (
  prompt: ConversationPrompt,
  value: string,
  displayValue: string,
) => void;

let activePrompt: ConversationPrompt | null = null;
let promptGeneration = 0;
let promptQuiesceEpoch = 0;

export interface BlockedPromptQuiesceToken {
  generation: number;
  quiesceEpoch: number;
  previousRequestId: string | null;
}

export interface ConversationPromptToken {
  threadId: string;
  requestId: string;
  element: HTMLElement;
  generation: number;
  submitting: boolean;
}

export function peekConversationPromptToken(threadId: string): ConversationPromptToken | null {
  if (!activePrompt || activePrompt.threadId !== threadId) return null;
  return {
    threadId,
    requestId: activePrompt.requestId,
    element: activePrompt.element,
    generation: promptGeneration,
    submitting: activePrompt.submitting,
  };
}

export function validateConversationPromptToken(token: ConversationPromptToken): boolean {
  return Boolean(activePrompt)
    && token.threadId === activePrompt!.threadId
    && token.requestId === activePrompt!.requestId
    && token.element === activePrompt!.element
    && token.generation === promptGeneration
    && token.submitting === activePrompt!.submitting;
}

export function quiesceConversationPromptForBlockedInstallNoDom(): BlockedPromptQuiesceToken {
  const previousRequestId = activePrompt?.requestId ?? null;
  promptGeneration += 1;
  promptQuiesceEpoch += 1;
  activePrompt = null;
  return { generation: promptGeneration, quiesceEpoch: promptQuiesceEpoch, previousRequestId };
}

export function validateBlockedPromptQuiesceToken(proof: BlockedPromptQuiesceToken): boolean {
  return proof.generation === promptGeneration
    && proof.quiesceEpoch === promptQuiesceEpoch
    && activePrompt === null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(asString).filter(Boolean)
    : [];
}

function promptType(data: Record<string, unknown>): ConversationPromptType | null {
  const type = data.prompt_type;
  return type === "clarify" || type === "checkpoint" || type === "goal_spec"
    ? type
    : null;
}

function promptRequestId(
  type: ConversationPromptType,
  data: Record<string, unknown>,
): string {
  if (type === "clarify") {
    return asString(data.clarify_id) || asString(data.request_id);
  }
  if (type === "checkpoint") {
    return asString(data.checkpoint_id) || asString(data.request_id);
  }
  return asString(data.prompt_id) || asString(data.request_id);
}

function promptChoices(
  type: ConversationPromptType,
  data: Record<string, unknown>,
): ConversationPromptChoice[] {
  if (type === "clarify") {
    return stringList(data.options).map((option) => ({
      label: option,
      value: option,
      description: "",
    }));
  }

  if (!Array.isArray(data.choices)) return [];
  return data.choices.flatMap((rawChoice) => {
    if (Array.isArray(rawChoice)) {
      const label = asString(rawChoice[0]) || asString(rawChoice[1]);
      const value = asString(rawChoice[1]) || label;
      if (!label || !value) return [];
      return [{ label, value, description: asString(rawChoice[2]) }];
    }
    if (!rawChoice || typeof rawChoice !== "object") return [];
    const choice = rawChoice as Record<string, unknown>;
    const label = asString(choice.label) || asString(choice.value);
    const value = asString(choice.value) || label;
    if (!label || !value) return [];
    return [{ label, value, description: asString(choice.description) }];
  });
}

function markdownSection(title: string, value: string): string {
  return value ? `**${title}**\n${value}` : "";
}

function markdownList(title: string, values: string[], ordered = false): string {
  if (values.length === 0) return "";
  const lines = values.map((value, index) => `${ordered ? `${index + 1}.` : "-"} ${value}`);
  return `**${title}**\n${lines.join("\n")}`;
}

export function formatConversationPrompt(data: Record<string, unknown>): string {
  const type = promptType(data);
  if (type === "clarify") {
    return asString(data.question) || "请补充说明。";
  }

  if (type === "checkpoint") {
    const plan = data.plan && typeof data.plan === "object"
      ? data.plan as Record<string, unknown>
      : {};
    return [
      "请确认以下实施计划。",
      markdownSection("目标", asString(plan.goal)),
      markdownSection("方案", asString(plan.plan_summary)),
      markdownList("步骤", stringList(plan.steps), true),
      markdownList("涉及文件", stringList(plan.affected_files)),
      markdownList("风险", stringList(plan.risks)),
    ].filter(Boolean).join("\n\n");
  }

  if (type === "goal_spec") {
    const spec = data.spec && typeof data.spec === "object"
      ? data.spec as Record<string, unknown>
      : {};
    const attempts = typeof spec.max_attempts === "number"
      ? String(spec.max_attempts)
      : "";
    return [
      "请确认以下目标设定。",
      markdownSection("目标", asString(spec.objective)),
      markdownSection("完成条件", asString(spec.acceptance_condition)),
      markdownSection("实现方式", asString(spec.achievement_method)),
      markdownSection("最大尝试次数", attempts),
    ].filter(Boolean).join("\n\n");
  }

  return "";
}

function removeReplyControls(prompt: ConversationPrompt): void {
  prompt.element.querySelector(".prompt-replies")?.remove();
}

function renderReplyControls(prompt: ConversationPrompt): void {
  removeReplyControls(prompt);
  if (prompt.choices.length === 0 || prompt.submitting) return;

  const controls = document.createElement("div");
  controls.className = "prompt-replies";
  controls.setAttribute("aria-label", "快捷回复");
  for (const choice of prompt.choices) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "prompt-reply";
    button.textContent = choice.label;
    if (choice.description) button.title = choice.description;
    button.addEventListener("click", () => {
      if (activePrompt !== prompt || prompt.submitting) return;
      prompt.onReply(prompt, choice.value, choice.label);
    });
    controls.append(button);
  }
  prompt.element.append(controls);
}

export function showConversationPrompt(
  itemId: string,
  threadId: string,
  data: Record<string, unknown>,
  onReply: ConversationPromptReplyHandler,
): ConversationPrompt | null {
  const type = promptType(data);
  if (!type) return null;
  const requestId = promptRequestId(type, data);
  if (!requestId) return null;

  if (activePrompt?.requestId === requestId && activePrompt.threadId === threadId) {
    return activePrompt;
  }
  if (activePrompt) removeReplyControls(activePrompt);

  const element = document.createElement("div");
  element.className = "prompt-message";
  element.dataset.itemId = itemId;
  element.dataset.promptRequestId = requestId;
  element.dataset.promptType = type;
  const text = formatConversationPrompt(data);
  element.append(renderMarkdown(text));

  const prompt: ConversationPrompt = {
    itemId,
    requestId,
    threadId,
    type,
    text,
    choices: promptChoices(type, data),
    element,
    submitting: false,
    onReply,
  };
  promptGeneration += 1;
  activePrompt = prompt;
  renderReplyControls(prompt);

  const transcript = getTranscriptElement();
  if (transcript) {
    transcript.append(element);
    requestTranscriptFollowAfterMutation();
  }
  return prompt;
}

export function pendingConversationPrompt(threadId: string): ConversationPrompt | null {
  if (!activePrompt || activePrompt.threadId !== threadId || activePrompt.submitting) {
    return null;
  }
  return activePrompt;
}

export function beginConversationPromptResponse(requestId: string): ConversationPrompt | null {
  if (!activePrompt || activePrompt.requestId !== requestId || activePrompt.submitting) {
    return null;
  }
  activePrompt.submitting = true;
  promptGeneration += 1;
  removeReplyControls(activePrompt);
  return activePrompt;
}

export function failConversationPromptResponse(requestId: string): void {
  if (!activePrompt || activePrompt.requestId !== requestId) return;
  activePrompt.submitting = false;
  promptGeneration += 1;
  renderReplyControls(activePrompt);
}

export function resolveConversationPrompt(requestId: string): void {
  if (!activePrompt || activePrompt.requestId !== requestId) return;
  removeReplyControls(activePrompt);
  promptGeneration += 1;
  activePrompt = null;
}

export function completeConversationPrompt(data: Record<string, unknown>): void {
  const type = promptType(data);
  if (!type) return;
  const requestId = promptRequestId(type, data);
  if (requestId) resolveConversationPrompt(requestId);
}

export function resetConversationPrompts(): void {
  if (activePrompt) removeReplyControls(activePrompt);
  promptGeneration += 1;
  activePrompt = null;
}
