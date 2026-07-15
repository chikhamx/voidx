// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import {
  setTranscriptElement,
  getOrCreateStream,
  appendStreamText,
  commitStream,
  discardStream,
  takeCommittedStreams,
  _resetForTest,
} from "../../src/utils/stream";

beforeEach(() => {
  _resetForTest();
  const transcript = document.querySelector("#transcript");
  setTranscriptElement(transcript);
});

describe("getOrCreateStream", () => {
  it("creates a new stream with DOM elements", () => {
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("");
    expect(stream.thinking).toBe("");
    expect(stream.phase).toBe("text");
    expect(stream.el.className).toBe("stream-buffer");
    expect(stream.el.dataset.streamId).toBe("s1");
    expect(stream.thinkingEl.className).toBe("stream-thinking");
    expect(stream.textEl.className).toBe("markdown-body");
  });

  it("returns existing stream for same id", () => {
    const s1 = getOrCreateStream("s1", "text");
    const s2 = getOrCreateStream("s1", "text");
    expect(s1).toBe(s2);
  });

  it("appends stream element to transcript", () => {
    getOrCreateStream("s1", "text");
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".stream-buffer")).not.toBeNull();
  });
});

describe("appendStreamText", () => {
  it("appends text to stream.text", () => {
    appendStreamText("s1", "hello", "text");
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("hello");
  });

  it("appends thinking to stream.thinking", () => {
    appendStreamText("s1", "analyzing", "thinking");
    const stream = getOrCreateStream("s1", "thinking");
    expect(stream.thinking).toBe("analyzing");
  });

  it("replaces thinking phase content from full stream snapshots", () => {
    appendStreamText("s1", "part1", "thinking");
    appendStreamText("s1", "part1 part2", "thinking");
    const stream = getOrCreateStream("s1", "thinking");
    expect(stream.thinking).toBe("part1 part2");
  });

  it("shows thinking as a transient widget while streaming", async () => {
    appendStreamText("s1", "long internal thought", "thinking");
    const stream = getOrCreateStream("s1", "thinking");

    await new Promise((r) => setTimeout(r, 150));
    expect(stream.thinkingEl.tagName).toBe("DIV");
    expect(stream.thinkingEl.hidden).toBe(false);
    expect(stream.thinkingEl.textContent).toContain("Thinking");
    expect(stream.thinkingEl.textContent).toContain("long internal thought");
  });

  it("shows only the last five thinking lines", async () => {
    appendStreamText("s1", "one\ntwo\nthree\nfour\nfive\nsix", "thinking");
    const stream = getOrCreateStream("s1", "thinking");

    await new Promise((r) => setTimeout(r, 150));
    expect(stream.thinkingBody.textContent).toBe("two\nthree\nfour\nfive\nsix");
  });

  it("hides thinking when answer text starts", async () => {
    appendStreamText("s1", "thinking line", "thinking");
    await new Promise((r) => setTimeout(r, 150));
    appendStreamText("s1", "final answer", "text");
    const stream = getOrCreateStream("s1", "text");

    expect(stream.thinkingEl.hidden).toBe(true);
    expect(stream.thinkingBody.textContent).toBe("");
  });

  it("strips terminal assistant prefix bullet from text stream", () => {
    appendStreamText("s1", "● final answer", "text");
    const stream = getOrCreateStream("s1", "text");

    expect(stream.text).toBe("final answer");
  });

  it("replaces text phase content (not accumulate)", () => {
    appendStreamText("s1", "v1", "text");
    appendStreamText("s1", "v2", "text");
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("v2");
  });
});

describe("commitStream", () => {
  it("returns committed stream data", () => {
    appendStreamText("s1", "final text", "text");
    appendStreamText("s1", "thoughts", "thinking");
    const result = commitStream("s1");
    expect(result).not.toBeNull();
    expect(result.text).toBe("final text");
    expect(result.thinking).toBe("thoughts");
    expect(result.el).toBeDefined();
    expect(result.el.querySelector(".stream-thinking").hidden).toBe(true);
  });

  it("removes stream from active streams", () => {
    appendStreamText("s1", "text", "text");
    commitStream("s1");
    const stream = getOrCreateStream("s1", "text");
    expect(stream.text).toBe("");
  });

  it("returns null for non-existent stream", () => {
    expect(commitStream("nonexistent")).toBeNull();
  });

  it("adds element to committed list", () => {
    appendStreamText("s1", "text", "text");
    commitStream("s1");
    const committed = takeCommittedStreams();
    expect(committed).toHaveLength(1);
  });
});

describe("takeCommittedStreams", () => {
  it("returns empty array when nothing committed", () => {
    expect(takeCommittedStreams()).toHaveLength(0);
  });

  it("returns committed elements and clears list", () => {
    appendStreamText("s1", "a", "text");
    commitStream("s1");
    appendStreamText("s2", "b", "text");
    commitStream("s2");
    const first = takeCommittedStreams();
    expect(first).toHaveLength(2);
    const second = takeCommittedStreams();
    expect(second).toHaveLength(0);
  });
});

describe("discardStream", () => {
  it("removes stream without committing", () => {
    appendStreamText("s1", "text", "text");
    discardStream("s1");
    const committed = takeCommittedStreams();
    expect(committed).toHaveLength(0);
  });

  it("does nothing for non-existent stream", () => {
    expect(() => discardStream("nonexistent")).not.toThrow();
  });

  it("removes stream element from transcript", () => {
    appendStreamText("s1", "text", "text");
    const transcript = document.querySelector("#transcript");
    expect(transcript.querySelector(".stream-buffer")).not.toBeNull();
    discardStream("s1");
    expect(transcript.querySelector(".stream-buffer")).toBeNull();
  });
});

describe("stream cursor", () => {
  it("shows cursor while streaming (not committed)", async () => {
    appendStreamText("s1", "hello", "text");
    await new Promise((r) => setTimeout(r, 150));
    const cursor = document.querySelector(".stream-cursor");
    expect(cursor).not.toBeNull();
  });

  it("removes cursor after commit", async () => {
    appendStreamText("s1", "hello", "text");
    await new Promise((r) => setTimeout(r, 150));
    commitStream("s1");
    const cursor = document.querySelector(".stream-cursor");
    expect(cursor).toBeNull();
  });
});
