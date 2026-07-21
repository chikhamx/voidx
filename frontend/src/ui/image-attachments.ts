export interface ImageAttachment {
  stem: string;
  dataUrl: string;
}

let attachments: ImageAttachment[] = [];

function stripEl(): HTMLElement | null {
  return document.querySelector<HTMLElement>("#attachment-strip");
}

function syncStrip(): void {
  const strip = stripEl();
  if (!strip) return;
  strip.hidden = attachments.length === 0;
}

export function addImageAttachment(stem: string, dataUrl: string): void {
  attachments.push({ stem, dataUrl });
  const strip = stripEl();
  if (strip) {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    chip.dataset.stem = stem;

    const img = document.createElement("img");
    img.src = dataUrl;
    img.alt = stem;
    chip.appendChild(img);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-chip-remove";
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      attachments = attachments.filter((a) => a.stem !== stem);
      chip.remove();
      syncStrip();
    });
    chip.appendChild(remove);

    strip.appendChild(chip);
  }
  syncStrip();
}

export function clearImageAttachments(): void {
  attachments = [];
  const strip = stripEl();
  if (strip) strip.innerHTML = "";
  syncStrip();
}

export function imageAttachmentTokens(): string {
  return attachments.map((a) => `[image-${a.stem}]`).join(" ");
}

export function _imageAttachmentsForTest(): ImageAttachment[] {
  return attachments.map((a) => ({ ...a }));
}
