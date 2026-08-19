const THREAD_HISTORY_LIMIT = 50;

type ThreadNavigator = (threadId: string) => Promise<void>;

let entries: string[] = [];
let position = -1;
let navigator: ThreadNavigator | null = null;
let pendingNavigation: { from: number; target: number } | null = null;

function getButton(id: string): HTMLButtonElement | null {
  return document.querySelector<HTMLButtonElement>(`#${id}`);
}

function updateButtons(): void {
  const back = getButton("titlebar-history-back");
  const forward = getButton("titlebar-history-forward");
  if (back) back.disabled = position <= 0 || pendingNavigation !== null;
  if (forward) forward.disabled = position < 0 || position >= entries.length - 1 || pendingNavigation !== null;
}

function navigateBy(offset: -1 | 1): void {
  if (!navigator || pendingNavigation !== null) return;
  const target = position + offset;
  if (target < 0 || target >= entries.length) return;

  const request = { from: position, target };
  position = target;
  pendingNavigation = request;
  updateButtons();

  Promise.resolve(navigator(entries[target]))
    .catch(() => {
      if (pendingNavigation === request) position = request.from;
    })
    .finally(() => {
      if (pendingNavigation === request) pendingNavigation = null;
      updateButtons();
    });
}

export function initThreadNavigation(navigate: ThreadNavigator): void {
  navigator = navigate;
  const back = getButton("titlebar-history-back");
  const forward = getButton("titlebar-history-forward");

  if (back && back.dataset.navigationInitialized !== "true") {
    back.dataset.navigationInitialized = "true";
    back.addEventListener("click", () => navigateBy(-1));
  }
  if (forward && forward.dataset.navigationInitialized !== "true") {
    forward.dataset.navigationInitialized = "true";
    forward.addEventListener("click", () => navigateBy(1));
  }
  updateButtons();
}

export function recordThreadVisit(threadId: string): void {
  if (!threadId) return;
  if (pendingNavigation) {
    if (entries[pendingNavigation.target] === threadId) {
      pendingNavigation = null;
      updateButtons();
      return;
    }
    pendingNavigation = null;
  }
  if (entries[position] === threadId) {
    updateButtons();
    return;
  }

  entries = entries.slice(0, position + 1);
  entries.push(threadId);
  if (entries.length > THREAD_HISTORY_LIMIT) {
    entries.splice(0, entries.length - THREAD_HISTORY_LIMIT);
  }
  position = entries.length - 1;
  updateButtons();
}

export function _resetNavigationForTest(): void {
  entries = [];
  position = -1;
  navigator = null;
  pendingNavigation = null;
  updateButtons();
}
