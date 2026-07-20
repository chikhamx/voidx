/** @ file and # skill reference helpers for the composer. */

export interface FileCandidate {
  rel_path: string;
  kind: string;
  size: number;
}

export interface SkillCandidate {
  name: string;
  scope: string;
  description: string;
  mode: string;
}

export interface McpCandidate {
  name: string;
  description: string;
  mode: string;
}

export type RefTrigger = "@" | "#";

export interface RefToken {
  trigger: RefTrigger;
  start: number;
  end: number;
  query: string;
  quoted: boolean;
}

export type RefCandidate =
  | { type: "file"; file: FileCandidate }
  | { type: "skill"; skill: SkillCandidate }
  | { type: "mcp"; mcp: McpCandidate };

function findTriggerToken(
  text: string,
  cursor: number,
  trigger: RefTrigger,
): RefToken | null {
  let start = text.lastIndexOf(trigger, cursor - 1);
  while (start !== -1) {
    if (start === 0 || /\s/.test(text[start - 1])) break;
    start = text.lastIndexOf(trigger, start - 1);
  }
  if (start === -1) return null;
  if (trigger === "@") {
    if (text[start + 1] === '"') {
      const closing = text.indexOf('"', start + 2);
      if (closing !== -1 && closing < cursor) return null;
      return { trigger, start, end: cursor, query: text.slice(start + 2, cursor), quoted: true };
    }
  } else if (text[start + 1] === "#") {
    return null;
  }
  const query = text.slice(start + 1, cursor);
  if (/\s/.test(query)) return null;
  return { trigger, start, end: cursor, query, quoted: false };
}

export function findRefToken(text: string, cursor: number): RefToken | null {
  const pos = Math.max(0, Math.min(cursor, text.length));
  if (pos === 0) return null;
  const at = findTriggerToken(text, pos, "@");
  const hash = findTriggerToken(text, pos, "#");
  if (at && hash) return at.start > hash.start ? at : hash;
  return at ?? hash;
}

export function fileInsertionText(file: FileCandidate): string {
  if (file.kind === "dir") return `@${file.rel_path}`;
  if (/\s/.test(file.rel_path)) return `@"${file.rel_path}" `;
  return `@${file.rel_path} `;
}

export function skillInsertionText(skill: SkillCandidate): string {
  return `$${skill.name} `;
}

export function mcpInsertionText(mcp: McpCandidate): string {
  return `$${mcp.name} `;
}

export function refInsertionText(candidate: RefCandidate): string {
  if (candidate.type === "file") return fileInsertionText(candidate.file);
  if (candidate.type === "mcp") return mcpInsertionText(candidate.mcp);
  return skillInsertionText(candidate.skill);
}


export function renderRefMenu(
  candidates: RefCandidate[],
  selectedIndex: number,
  onSelect?: (candidate: RefCandidate) => void,
): HTMLElement {
  const menu = document.createElement("div");
  menu.className = "ref-menu";
  candidates.forEach((candidate, index) => {
    const item = document.createElement("div");
    item.className = "ref-item slash-item";
    if (index === selectedIndex) item.classList.add("selected");
    if (typeof onSelect === "function") {
      item.addEventListener("click", () => onSelect(candidate));
    }
    const nameEl = document.createElement("span");
    nameEl.className = "ref-name slash-command";
    const metaEl = document.createElement("span");
    metaEl.className = "ref-meta slash-meta";
    const descEl = document.createElement("span");
    descEl.className = "ref-desc slash-desc";
    if (candidate.type === "file") {
      nameEl.textContent = candidate.file.rel_path;
      metaEl.textContent = candidate.file.kind;
    } else if (candidate.type === "mcp") {
      nameEl.textContent = `#${candidate.mcp.name}`;
      metaEl.textContent = `mcp · ${candidate.mcp.mode}`;
      descEl.textContent = candidate.mcp.description;
    } else {
      nameEl.textContent = `#${candidate.skill.name}`;
      metaEl.textContent = candidate.skill.scope;
      descEl.textContent = candidate.skill.description;
    }
    item.append(nameEl, metaEl, descEl);
    menu.append(item);
  });
  return menu;
}
