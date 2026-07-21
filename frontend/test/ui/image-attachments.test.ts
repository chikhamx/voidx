// @ts-nocheck
import { describe, it, expect, beforeEach } from "vitest";
import {
  addImageAttachment,
  clearImageAttachments,
  imageAttachmentTokens,
  _imageAttachmentsForTest,
} from "../../src/ui/image-attachments";

const strip = () => document.querySelector("#attachment-strip");

beforeEach(() => {
  clearImageAttachments();
});

describe("addImageAttachment", () => {
  it("renders a thumbnail chip with the image", () => {
    addImageAttachment("clip-1", "data:image/png;base64,AAAA");
    const chip = strip().querySelector(".attachment-chip");
    expect(chip).toBeTruthy();
    const img = chip.querySelector("img");
    expect(img.src).toContain("data:image/png");
    expect(strip().hidden).toBe(false);
  });

  it("tracks pending attachments and exposes their tokens", () => {
    addImageAttachment("clip-1", "data:image/png;base64,AAAA");
    addImageAttachment("clip-2", "data:image/png;base64,BBBB");
    expect(_imageAttachmentsForTest().map((a) => a.stem)).toEqual(["clip-1", "clip-2"]);
    expect(imageAttachmentTokens()).toBe("[image-clip-1] [image-clip-2]");
  });

  it("removes the chip when its close button is clicked", () => {
    addImageAttachment("clip-1", "data:image/png;base64,AAAA");
    addImageAttachment("clip-2", "data:image/png;base64,BBBB");
    strip().querySelectorAll(".attachment-chip-remove")[0].click();
    expect(_imageAttachmentsForTest().map((a) => a.stem)).toEqual(["clip-2"]);
    expect(strip().querySelectorAll(".attachment-chip")).toHaveLength(1);
  });
});

describe("clearImageAttachments", () => {
  it("removes all chips and hides the strip", () => {
    addImageAttachment("clip-1", "data:image/png;base64,AAAA");
    clearImageAttachments();
    expect(_imageAttachmentsForTest()).toHaveLength(0);
    expect(strip().querySelectorAll(".attachment-chip")).toHaveLength(0);
    expect(strip().hidden).toBe(true);
  });

  it("returns empty tokens when nothing is pending", () => {
    expect(imageAttachmentTokens()).toBe("");
  });
});
