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
