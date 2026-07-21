export type PasteKind = "text" | "image";

export interface PasteEntry {
  id: number;
  kind: PasteKind;
  display: string;
  expanded: string;
}

let pasteEntries: PasteEntry[] = [];
let pasteNextId = 1;

export function computeTextPasteDisplay(pasteId: number, text: string): string {
  const lineCount = text.split("\n").length;
  if (lineCount > 1) {
    return `[Pasted text #${pasteId} +${lineCount - 1} lines]`;
  }
  return `[Pasted text #${pasteId} ${text.length} chars]`;
}

function registerPasteEntry(kind: PasteKind, display: string, expanded: string): string {
  pasteEntries.push({ id: pasteNextId, kind, display, expanded });
  pasteNextId += 1;
  return display;
}

export function registerTextPaste(text: string): string {
  return registerPasteEntry("text", computeTextPasteDisplay(pasteNextId, text), text);
}

export function expandPasteTokens(text: string): string {
  let result = text;
  for (const entry of pasteEntries) {
    if (!entry.display || !result.includes(entry.display)) continue;
    const replacement =
      entry.kind === "text"
        ? `<pasted>\n${entry.expanded}\n</pasted>`
        : entry.expanded;
    result = result.split(entry.display).join(replacement);
  }
  return result;
}

export function clearPasteEntries(): void {
  pasteEntries = [];
  pasteNextId = 1;
}

export function _pasteEntriesForTest(): PasteEntry[] {
  return pasteEntries.map((entry) => ({ ...entry }));
}
