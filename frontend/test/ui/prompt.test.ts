import { beforeEach, describe, expect, it, vi } from "vitest";
import * as promptApi from "../../src/ui/prompt";
import {
  pendingConversationPrompt,
  resetConversationPrompts,
  showConversationPrompt,
  validateBlockedPromptQuiesceToken,
  beginConversationPromptResponse,
  failConversationPromptResponse,
  peekConversationPromptToken,
  validateConversationPromptToken,
} from "../../src/ui/prompt";
import {
  _resetForTest as resetStreams,
  setTranscriptElement,
} from "../../src/utils/stream";

beforeEach(() => {
  resetConversationPrompts();
  resetStreams();
  document.querySelector("[data-prompt-test-transcript]")?.remove();
  const transcript = document.createElement("div");
  transcript.dataset.promptTestTranscript = "true";
  document.body.append(transcript);
  setTranscriptElement(transcript);
});

describe("blocked prompt quiesce", () => {
  const quietPrompt = () => {
    const method = "quiesceConversationPromptForBlocked" + "InstallNoDom";
    return (promptApi as unknown as Record<string, () => unknown>)[method]();
  };

  it("clears active ownership without removing transcript DOM or reply controls", () => {
    const onReply = vi.fn();
    const prompt = showConversationPrompt("item-1", "thread-1", {
      prompt_type: "clarify",
      clarify_id: "request-1",
      question: "Choose",
      options: ["A"],
    }, onReply)!;
    const element = prompt.element;
    const button = element.querySelector<HTMLButtonElement>(".prompt-reply")!;

    const proof = quietPrompt() as { previousRequestId: string | null };

    expect(proof.previousRequestId).toBe("request-1");
    expect(validateBlockedPromptQuiesceToken(proof as never)).toBe(true);
    expect(pendingConversationPrompt("thread-1")).toBeNull();
    expect(element.isConnected).toBe(true);
    expect(element.querySelector(".prompt-replies")).not.toBeNull();
    button.click();
    expect(onReply).not.toHaveBeenCalled();
  });

  it("permanently invalidates an old proof when a new prompt is installed", () => {
    showConversationPrompt("item-1", "thread-1", {
      prompt_type: "clarify",
      clarify_id: "request-1",
      question: "First",
      options: ["A"],
    }, vi.fn());
    const proof = quietPrompt();
    expect(validateBlockedPromptQuiesceToken(proof as never)).toBe(true);

    showConversationPrompt("item-2", "thread-1", {
      prompt_type: "clarify",
      clarify_id: "request-2",
      question: "Second",
      options: ["B"],
    }, vi.fn());

    expect(validateBlockedPromptQuiesceToken(proof as never)).toBe(false);
  });
});

describe("live conversation prompt token", () => {
  const showPrompt = () => showConversationPrompt("item-live", "thread-live", {
    prompt_type: "clarify",
    clarify_id: "request-live",
    question: "Choose",
    options: ["A"],
  }, vi.fn())!;

  it("invalidates tokens when response state changes without replacing the prompt element", () => {
    const prompt = showPrompt();
    const element = prompt.element;
    const initial = peekConversationPromptToken("thread-live");
    expect(initial).not.toBeNull();
    expect(validateConversationPromptToken(initial!)).toBe(true);

    expect(beginConversationPromptResponse("request-live")?.element).toBe(element);
    expect(validateConversationPromptToken(initial!)).toBe(false);
    const submitting = peekConversationPromptToken("thread-live");
    expect(submitting).not.toBeNull();
    expect(validateConversationPromptToken(submitting!)).toBe(true);

    failConversationPromptResponse("request-live");
    expect(prompt.element).toBe(element);
    expect(validateConversationPromptToken(submitting!)).toBe(false);
    const retryable = peekConversationPromptToken("thread-live");
    expect(retryable).not.toBeNull();
    expect(validateConversationPromptToken(retryable!)).toBe(true);
  });
});
