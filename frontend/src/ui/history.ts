/** Composer input history — mirrors TUI input.py semantics. */

export const HISTORY_LIMIT = 1000;

let entries: string[] = [];
let browseIndex: number | null = null;
let draft = "";

export function pushHistory(text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  if (entries[entries.length - 1] !== trimmed) {
    entries.push(trimmed);
    if (entries.length > HISTORY_LIMIT) {
      entries.splice(0, entries.length - HISTORY_LIMIT);
    }
  }
  resetHistoryNavigation();
}

export function resetHistoryNavigation(): void {
  browseIndex = null;
  draft = "";
}

export function historyPrev(currentText: string): string | null {
  if (entries.length === 0) return null;
  if (browseIndex === null) {
    draft = currentText;
    browseIndex = entries.length - 1;
    return entries[browseIndex];
  }
  if (browseIndex === 0) return null;
  browseIndex -= 1;
  return entries[browseIndex];
}

export function historyNext(): string | null {
  if (browseIndex === null) return null;
  if (browseIndex >= entries.length - 1) {
    const restored = draft;
    resetHistoryNavigation();
    return restored;
  }
  browseIndex += 1;
  return entries[browseIndex];
}

export function _resetHistoryForTest(): void {
  entries = [];
  resetHistoryNavigation();
}

export function isHistoryBrowsing(): boolean {
  return browseIndex !== null;
}
