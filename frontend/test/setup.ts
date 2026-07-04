import { beforeEach } from "vitest";

document.body.innerHTML = `
  <main class="vx-shell vx-workbench-shell">
    <header class="vx-titlebar">
      <div class="vx-titlebar-left">
        <span class="status-dot disconnected" id="status-dot" aria-label="Connection status"></span>
      </div>
    </header>
    <div class="vx-body">
      <aside class="vx-sidebar" id="sidebar">
        <div class="vx-sidebar-header">
          <span class="vx-project-name">Project</span>
        </div>
        <nav class="vx-sidebar-nav" aria-label="Workspace navigation">
          <button type="button" class="vx-nav-item vx-new-chat" id="btn-new-chat">新对话</button>
          <label class="vx-nav-item vx-search-item">
            <span>搜索</span>
            <input type="text" class="vx-search" id="session-search" placeholder="搜索会话..." />
          </label>
          <button type="button" class="vx-nav-item">已安排</button>
          <button type="button" class="vx-nav-item" id="btn-integrations">插件</button>
        </nav>
        <div class="vx-sidebar-section">
          <div class="vx-sidebar-heading">项目</div>
          <div class="vx-project-list" id="project-list">
            <button type="button" class="vx-project-item active" data-project-name="voidx">voidx</button>
          </div>
        </div>
        <div class="vx-sidebar-section vx-sidebar-history">
          <div class="vx-sidebar-heading">历史会话</div>
          <div class="vx-session-list" id="session-list"></div>
        </div>
        <div class="vx-sidebar-footer">
          <button type="button" class="vx-nav-item" id="btn-settings">设置</button>
          <button type="button" class="vx-nav-item" id="btn-account">账户</button>
        </div>
      </aside>
      <section class="vx-main">
        <div class="vx-main-canvas">
        <section class="vx-empty-state" id="empty-state" aria-live="polite">
          <h1>我们应该在 voidx 中构建什么？</h1>
        </section>
        <div class="transcript" id="transcript" aria-live="polite"></div>
        <form class="composer" id="composer">
          <div class="slash-menu" id="slash-menu"></div>
          <textarea id="input" rows="3"></textarea>
          <div class="vx-composer-actions">
            <button type="button" class="vx-attach-btn" id="btn-attach" aria-label="Add context">+</button>
            <span class="vx-permission-pill" id="permission-pill">完全访问</span>
            <label class="vx-select-label" for="provider-select">
              <span>Provider</span>
              <select id="provider-select" aria-label="Provider"></select>
            </label>
            <label class="vx-select-label" for="model-select">
              <span>Model</span>
              <select id="model-select" aria-label="Model"></select>
            </label>
            <button type="submit" class="btn-send" id="btn-send" aria-label="Send">↑</button>
            <button type="button" class="btn-cancel" id="btn-cancel" aria-label="Cancel" disabled hidden>■</button>
          </div>
        </form>
        <div class="context-menu" id="context-menu" hidden>
          <div class="context-menu-item" data-action="paste">📋 Paste image from clipboard</div>
          <div class="context-menu-item disabled" data-action="file">📁 Add file/folder context</div>
          <div class="context-menu-item" data-action="web">🌐 Add web context</div>
        </div>
        <div class="vx-context-row" id="context-row">
          <span id="context-workspace">voidx</span>
          <span id="context-permission">完全访问</span>
          <span id="context-provider-model"></span>
        </div>
        </div>
      </section>
    </div>
    <aside class="vx-dock collapsed" id="dock">
      <div class="vx-dock-tabs">
        <button class="vx-dock-tab active" data-tab="todo" aria-selected="true">Todo</button>
        <button class="vx-dock-tab" data-tab="terminal" aria-selected="false">Terminal</button>
        <button class="vx-dock-tab" data-tab="diff" aria-selected="false">Diff</button>
        <button class="vx-dock-tab" data-tab="status" aria-selected="false">Status</button>
        <button class="vx-dock-toggle" id="dock-toggle" aria-label="Toggle bottom panel">▾</button>
      </div>
      <div class="vx-dock-strip" id="dock-strip">
        <span class="status-session" id="status-session"></span>
        <span id="strip-workspace">voidx</span>
        <span id="strip-permission">完全访问</span>
        <span id="strip-provider-model"></span>
      </div>
      <div class="vx-dock-content" id="dock-content">
        <div class="vx-dock-pane" data-pane="todo">
          <section class="todo-panel" id="todo-panel" aria-label="Task progress"></section>
        </div>
        <div class="vx-dock-pane" data-pane="terminal" hidden>
          <div class="vx-terminal" id="terminal-pane"></div>
        </div>
        <div class="vx-dock-pane" data-pane="diff" hidden>
          <div class="vx-diff-review" id="diff-pane"></div>
        </div>
        <div class="vx-dock-pane" data-pane="status" hidden>
          <dl class="vx-status-grid" id="status-panel">
            <div><dt>Connection</dt><dd id="status-connection">disconnected</dd></div>
            <div><dt>Session</dt><dd id="status-session-detail"></dd></div>
            <div><dt>Workspace</dt><dd id="status-workspace-detail">voidx</dd></div>
            <div><dt>Model</dt><dd id="status-provider-model"></dd></div>
            <div><dt>Permission</dt><dd id="status-permission">完全访问</dd></div>
            <div><dt>Running</dt><dd id="status-running">idle</dd></div>
          </dl>
        </div>
      </div>
    </aside>
    <!-- Integrations panel -->
    <dialog id="integrations-dialog" class="settings-dialog integrations-dialog">
      <form id="integrations-form" method="dialog">
        <header class="settings-header">
        <div>
          <h2>插件</h2>
          <p>管理 MCP、Web Search、Skills 和 LSP</p>
        </div>
        <button type="button" id="integrations-close" aria-label="Close integrations">×</button>
        </header>
        <div id="integrations-content" class="settings-content"></div>
      </form>
    </dialog>
    <dialog id="settings-dialog" class="settings-dialog">
      <form id="settings-form" method="dialog">
        <header class="settings-header">
          <div>
            <h2>设置</h2>
            <p id="settings-summary">模型、权限、偏好、IDE 和高级</p>
          </div>
          <button type="button" id="settings-close" aria-label="Close settings">×</button>
        </header>
        <nav class="settings-tabs" id="settings-tabs">
          <button type="button" data-tab="model" class="settings-tab active">模型</button>
          <button type="button" data-tab="permissions" class="settings-tab">权限</button>
          <button type="button" data-tab="preferences" class="settings-tab">偏好</button>
          <button type="button" data-tab="code" class="settings-tab">代码</button>
          <button type="button" data-tab="advanced" class="settings-tab">高级</button>
        </nav>
        <div id="settings-content" class="settings-content"></div>
        <footer class="settings-footer">
          <span id="settings-error" class="settings-error" role="alert"></span>
          <button type="button" id="settings-save" class="settings-save">保存</button>
        </footer>
      </form>
    </dialog>
    <dialog id="request-dialog" class="request-dialog">
      <form id="request-form" method="dialog">
        <h2 id="request-title"></h2>
        <div id="request-details"></div>
        <div id="request-controls"></div>
      </form>
    </dialog>
  </main>
`;

beforeEach(() => {
  const transcript = document.querySelector("#transcript");
  if (transcript) transcript.innerHTML = "";
  const todo = document.querySelector("#todo-panel");
  if (todo) todo.innerHTML = "";
  const slash = document.querySelector("#slash-menu");
  if (slash) slash.innerHTML = "";
  const input = document.querySelector<HTMLTextAreaElement>("#input");
  if (input) input.value = "";
  const empty = document.querySelector<HTMLElement>("#empty-state");
  if (empty) empty.hidden = false;
  const dock = document.querySelector("#dock");
  if (dock) dock.className = "vx-dock collapsed";
  const strip = document.querySelector<HTMLElement>("#dock-strip");
  if (strip) strip.hidden = false;
});
