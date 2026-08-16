interface DiffLine {
  kind: string;
  text: string;
}

interface DiffHunk {
  index: number;
  old_start: number;
  old_count: number;
  new_start: number;
  new_count: number;
  lines?: DiffLine[];
  decision?: string;
}

interface DiffFile {
  path: string;
  added: number;
  removed: number;
  hunks?: DiffHunk[];
}

interface DiffSnapshot {
  files?: DiffFile[];
}

interface DiffSummary {
  total_hunks: number;
  approved: number;
  rejected: number;
  pending: number;
}

type HunkDecisionCb = (
  reviewId: string,
  filePath: string,
  hunkIndex: number,
  decision: string,
) => void;
type ApplyDiffCb = (reviewId: string) => void;
type GenerateDiffCb = () => void;

let hunkDecisionCb: HunkDecisionCb | null = null;
let applyDiffCb: ApplyDiffCb | null = null;
let generateDiffCb: GenerateDiffCb | null = null;

export function renderDiffReview(reviewId: string, snapshot: DiffSnapshot): void {
  const pane = document.querySelector<HTMLElement>("#diff-pane");
  if (!pane) return;

  pane.replaceChildren();

  if (!snapshot.files || snapshot.files.length === 0) {
    const empty = document.createElement("div");
    empty.className = "vx-diff-empty";
    empty.textContent = "No changes to review";
    pane.append(empty);
    return;
  }

  const summaryEl = document.createElement("div");
  summaryEl.className = "vx-diff-summary";
  pane.append(summaryEl);

  for (const file of snapshot.files) {
    const fileEl = document.createElement("div");
    fileEl.className = "vx-diff-file";
    fileEl.dataset.filePath = file.path;

    const header = document.createElement("div");
    header.className = "vx-diff-file-header";
    const path = document.createElement("span");
    path.className = "vx-diff-file-path";
    path.textContent = file.path;
    const stats = document.createElement("span");
    stats.className = "vx-diff-file-stats";
    stats.textContent = `+${file.added} -${file.removed}`;
    header.append(path, stats);
    fileEl.append(header);

    for (const hunk of file.hunks || []) {
      fileEl.append(renderHunk(reviewId, file.path, hunk));
    }

    pane.append(fileEl);
  }

  const applyBtn = document.createElement("button");
  applyBtn.className = "vx-diff-apply";
  applyBtn.textContent = "Apply Approved";
  applyBtn.addEventListener("click", () => {
    if (applyDiffCb) applyDiffCb(reviewId);
  });
  pane.append(applyBtn);

  updateSummary(pane, {
    total_hunks: countHunks(snapshot),
    approved: 0,
    rejected: 0,
    pending: countHunks(snapshot),
  });
}

function renderHunk(
  reviewId: string,
  filePath: string,
  hunk: DiffHunk,
): HTMLElement {
  const hunkEl = document.createElement("div");
  hunkEl.className = "vx-diff-hunk";
  hunkEl.dataset.hunkIndex = String(hunk.index);
  hunkEl.dataset.filePath = filePath;
  hunkEl.classList.add(`decision-${hunk.decision || "pending"}`);

  const hunkHeader = document.createElement("div");
  hunkHeader.className = "vx-diff-hunk-header";
  hunkHeader.textContent = `@@ -${hunk.old_start},${hunk.old_count} +${hunk.new_start},${hunk.new_count} @@`;
  hunkEl.append(hunkHeader);

  for (const line of hunk.lines || []) {
    const lineEl = document.createElement("div");
    const cssKind = line.kind === "remove" ? "del" : line.kind;
    lineEl.className = `diff-line diff-line-${cssKind}`;
    lineEl.textContent = line.text;
    hunkEl.append(lineEl);
  }

  const actions = document.createElement("div");
  actions.className = "vx-diff-hunk-actions";

  const approveBtn = document.createElement("button");
  approveBtn.className = "vx-diff-btn";
  approveBtn.dataset.decision = "approved";
  approveBtn.textContent = "Approve";
  approveBtn.addEventListener("click", () => {
    if (hunkDecisionCb)
      hunkDecisionCb(reviewId, filePath, hunk.index, "approved");
  });

  const rejectBtn = document.createElement("button");
  rejectBtn.className = "vx-diff-btn";
  rejectBtn.dataset.decision = "rejected";
  rejectBtn.textContent = "Reject";
  rejectBtn.addEventListener("click", () => {
    if (hunkDecisionCb)
      hunkDecisionCb(reviewId, filePath, hunk.index, "rejected");
  });

  actions.append(approveBtn, rejectBtn);
  hunkEl.append(actions);

  return hunkEl;
}

export function setHunkDecision(
  filePath: string,
  hunkIndex: number,
  decision: string,
  summary?: DiffSummary,
): void {
  const pane = document.querySelector<HTMLElement>("#diff-pane");
  if (!pane) return;

  const hunk = pane.querySelector<HTMLElement>(
    `.vx-diff-hunk[data-file-path="${filePath}"][data-hunk-index="${hunkIndex}"]`,
  );
  if (hunk) {
    hunk.classList.remove(
      "decision-pending",
      "decision-approved",
      "decision-rejected",
    );
    hunk.classList.add(`decision-${decision}`);
  }

  if (summary) {
    updateSummary(pane, summary);
  }
}

function updateSummary(pane: HTMLElement, summary: DiffSummary): void {
  const summaryEl = pane.querySelector<HTMLElement>(".vx-diff-summary");
  if (!summaryEl) return;

  const parts: string[] = [];
  if (summary.approved > 0)
    parts.push(`${summary.approved}/${summary.total_hunks} approved`);
  if (summary.rejected > 0)
    parts.push(`${summary.rejected}/${summary.total_hunks} rejected`);
  if (summary.pending > 0)
    parts.push(`${summary.pending}/${summary.total_hunks} pending`);
  if (parts.length === 0)
    parts.push(`${summary.total_hunks}/${summary.total_hunks} approved`);

  summaryEl.textContent = parts.join(" · ");
}

function countHunks(snapshot: DiffSnapshot): number {
  let count = 0;
  for (const file of snapshot.files || []) {
    count += (file.hunks || []).length;
  }
  return count;
}

export function onHunkDecision(callback: HunkDecisionCb): void {
  hunkDecisionCb = callback;
}

export function onApplyDiff(callback: ApplyDiffCb): void {
  applyDiffCb = callback;
}

export function onGenerateDiff(callback: GenerateDiffCb): void {
  generateDiffCb = callback;
}

export function showDiffEmpty(): void {
  const pane = document.querySelector<HTMLElement>("#diff-pane");
  if (!pane) return;

  pane.replaceChildren();

  const btn = document.createElement("button");
  btn.className = "vx-diff-generate";
  btn.textContent = "Generate Diff";
  btn.addEventListener("click", () => {
    if (generateDiffCb) generateDiffCb();
  });
  pane.append(btn);
}

export function _resetForTest(): void {
  hunkDecisionCb = null;
  applyDiffCb = null;
  generateDiffCb = null;
}
