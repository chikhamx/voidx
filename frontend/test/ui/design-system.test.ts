import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const readProjectFile = (path: string) =>
  readFileSync(resolve(process.cwd(), path), "utf8");

const STYLESHEETS = [
  "tokens.css",
  "base.css",
  "layout.css",
  "chat.css",
  "composer.css",
  "components.css",
] as const;

const PROFILE_TOKENS = ["chat", "coding", "goal", "loop"] as const;

function themeBlock(tokens: string, selector: string): string {
  const start = tokens.indexOf(selector);
  const end = tokens.indexOf("\n}", start);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  return tokens.slice(start, end);
}

describe("design system entry", () => {
  it("loads layered styles through the Vite entry in strict dependency order", () => {
    const main = readProjectFile("src/main.ts");
    const html = readProjectFile("index.html");
    let previousIndex = -1;

    for (const stylesheet of STYLESHEETS) {
      const index = main.indexOf(`import "../css/${stylesheet}"`);
      expect(index).toBeGreaterThan(previousIndex);
      previousIndex = index;
    }
    expect(html).not.toContain("css/styles.css");
  });

  it("sets the initial theme before the Vite entry executes", () => {
    const html = readProjectFile("index.html");
    const themeBootstrap = html.indexOf('localStorage.getItem("voidx.theme")');
    const appEntry = html.indexOf('src="./src/main.ts"');

    expect(themeBootstrap).toBeGreaterThanOrEqual(0);
    expect(themeBootstrap).toBeLessThan(appEntry);
    expect(html).toContain('document.documentElement.dataset.theme = resolved');
    expect(html).toContain(': "dark"');
  });

  it("uses canonical canvas tokens across every stylesheet", () => {
    for (const file of readdirSync(resolve(process.cwd(), "css")).filter((name) => name.endsWith(".css"))) {
      expect(readProjectFile(`css/${file}`)).not.toContain("--vx-bg-base");
    }
  });

  it("defines all runtime profile colors for light, dark, and system-dark", () => {
    const tokens = readProjectFile("css/tokens.css");
    const blocks = [
      themeBlock(tokens, ':root[data-theme="light"]'),
      themeBlock(tokens, ':root[data-theme="dark"]'),
      themeBlock(tokens, ':root:not([data-theme])'),
    ];

    for (const block of blocks) {
      for (const profile of PROFILE_TOKENS) {
        expect(block).toContain(`--vx-mode-${profile}:`);
      }
    }
  });
});


describe("chat overflow constraints", () => {
  it("wraps regular tool summaries and only truncates command summaries", () => {
    const chat = readProjectFile("css/chat.css");
    const regularSummary = themeBlock(chat, ".tool-summary {");
    const commandSummary = themeBlock(chat, ".tool-summary-command {");
    const commandTarget = themeBlock(chat, ".tool-summary-command .tool-target {");

    expect(regularSummary).not.toContain("display: inline-flex");
    expect(regularSummary).toContain("overflow-wrap: anywhere");
    expect(commandSummary).toContain("display: inline-flex");
    expect(commandSummary).toContain("white-space: nowrap");
    expect(commandTarget).toContain("text-overflow: ellipsis");
  });
});


describe("request dialog overflow constraints", () => {
  it("wraps long clarify questions and choices within the dialog", () => {
    const components = readProjectFile("css/components.css");
    const form = themeBlock(components, ".request-dialog form {");
    const title = themeBlock(components, ".request-dialog h2 {");
    const actions = themeBlock(components, ".request-actions {");
    const choice = themeBlock(components, ".request-actions button {");

    expect(form).toContain("min-width: 0");
    expect(title).toContain("overflow-wrap: anywhere");
    expect(actions).toContain("min-width: 0");
    expect(choice).toContain("max-width: 100%");
    expect(choice).toContain("overflow-wrap: anywhere");
    expect(choice).toContain("white-space: normal");
  });
});


describe("transcript viewport shell", () => {
  it("uses the same accessible wrapper and return-bottom button in production and tests", () => {
    const html = readProjectFile("index.html");
    const setup = readProjectFile("test/setup.ts");

    for (const source of [html, setup]) {
      expect(source).toContain('class="vx-transcript-viewport"');
      expect(source).toContain('id="transcript-return-bottom"');
      expect(source).toContain('class="vx-return-bottom"');
      expect(source).toContain('aria-label="回到底部"');
      expect(source).toMatch(/id="transcript-return-bottom"[^>]*hidden/);
      expect(source.indexOf('class="vx-transcript-viewport"')).toBeLessThan(
        source.indexOf('id="transcript"'),
      );
      expect(source.indexOf('id="transcript"')).toBeLessThan(
        source.indexOf('id="transcript-return-bottom"'),
      );
    }
  });

  it("styles the viewport and button with existing tokens and no smooth transcript scroll", () => {
    const chat = readProjectFile("css/chat.css");
    const layout = readProjectFile("css/layout.css");
    const viewport = themeBlock(chat, ".vx-transcript-viewport {");
    const button = themeBlock(chat, ".vx-return-bottom {");
    const hover = themeBlock(chat, ".vx-return-bottom:hover {");
    const hidden = themeBlock(chat, ".vx-return-bottom[hidden] {");

    expect(viewport).toContain("position: relative");
    expect(viewport).toContain("display: flex");
    expect(viewport).toContain("flex-direction: column");
    expect(viewport).toContain("min-height: 0");
    expect(button).toContain("background: var(--vx-bg-elevated)");
    expect(button).toContain("border: 1px solid var(--vx-border-strong)");
    expect(button).toContain("border-radius: var(--vx-radius-full)");
    expect(button).toContain("box-shadow: var(--vx-shadow-sm)");
    expect(button).toContain("color: var(--vx-text-primary)");
    expect(hover).toContain("background: var(--vx-bg-hover)");
    expect(hidden).toContain("display: none");
    expect(chat).not.toContain("scroll-behavior: smooth");
    expect(layout).toContain(".vx-main-canvas.empty .vx-transcript-viewport { display: none; }");
  });

  it("caches the return-bottom button in state", () => {
    const state = readProjectFile("src/services/state.ts");
    expect(state).toContain("export let transcriptReturnBottomEl: HTMLButtonElement;");
    expect(state).toContain(
      'transcriptReturnBottomEl = document.querySelector<HTMLButtonElement>("#transcript-return-bottom")!;',
    );
  });
});


describe("transcript scroll ownership", () => {
  it("routes every background transcript renderer through the follow helper", () => {
    const rendererFiles = [
      "src/utils/render.ts",
      "src/utils/render-thought-items.ts",
      "src/utils/render-tool-items.ts",
      "src/utils/render-file-changes.ts",
      "src/utils/render-notice-status.ts",
      "src/ui/prompt.ts",
    ];

    for (const file of rendererFiles) {
      const source = readProjectFile(file);
      expect(source, file).not.toMatch(/scrollTop\s*=/);
      expect(source, file).toContain("requestTranscriptFollowAfterMutation");
    }

    const stream = readProjectFile("src/utils/stream.ts");
    expect(stream).not.toMatch(/scrollTop\s*=/);
    expect(stream).toContain("requestTranscriptFollowAfterMutation");
    expect(readProjectFile("src/ui/terminal.ts")).toMatch(/scrollTop\s*=/);
  });


  it("keeps keyed snapshots out of viewport reset and transcript clear paths", () => {
    const main = readProjectFile("src/main.ts");
    const start = main.indexOf("function renderWorkspaceSnapshot(");
    const end = main.indexOf("\nexport function handleNotification(", start);
    const snapshotHandler = main.slice(start, end);

    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeGreaterThan(start);
    expect(snapshotHandler).not.toContain("resetTranscriptViewport()");
    expect(snapshotHandler).not.toContain("replaceChildren(");
    expect(snapshotHandler).toContain("renderTranscript(transcriptEl");
  });

  it("keeps the only direct main transcript scroll writer in synchronous pagination prepend", () => {
    const main = readProjectFile("src/main.ts");
    const directWrites = [...main.matchAll(/transcriptEl\.scrollTop\s*=/g)];
    const prependStart = main.indexOf("function loadEarlierTranscriptPage(");
    const prependEnd = main.indexOf("\nfunction handleTranscriptScroll(", prependStart);

    expect(directWrites).toHaveLength(1);
    expect(directWrites[0].index).toBeGreaterThan(prependStart);
    expect(directWrites[0].index).toBeLessThan(prependEnd);
  });
});
