import { beforeEach } from "vitest";

if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.removeAttribute("open");
  };
}

document.body.innerHTML = `
  <main class="vx-shell vx-workbench-shell">
    <header class="vx-titlebar">
      <div class="vx-titlebar-left">
        <span class="status-dot disconnected" id="status-dot" aria-label="Connection status"></span>
        <button type="button" class="vx-titlebar-tool" id="titlebar-sidebar-toggle" aria-label="Toggle sidebar">▯</button>
        <button type="button" class="vx-titlebar-tool" aria-label="Back" disabled>←</button>
        <button type="button" class="vx-titlebar-tool" aria-label="Forward" disabled>→</button>
      </div>
    </header>
    <div class="vx-body">
      <aside class="vx-sidebar" id="sidebar">
        <div class="vx-sidebar-header">
          <span class="vx-project-name">Project</span>
        </div>
        <nav class="vx-sidebar-nav" aria-label="Workspace navigation">
          <button type="button" class="vx-nav-item vx-new-chat" id="btn-new-chat">
            <span class="vx-sidebar-row-icon"><svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M10 4.5v11"/><path d="M4.5 10h11"/></svg></span>
            <span>新对话</span>
          </button>
          <label class="vx-nav-item vx-search-item">
            <span class="vx-sidebar-row-icon"><svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><circle cx="9" cy="9" r="5"/><path d="m12.8 12.8 3.2 3.2"/></svg></span>
            <span>搜索</span>
            <input type="text" class="vx-search" id="session-search" placeholder="搜索会话..." />
          </label>
          <button type="button" class="vx-nav-item vx-hidden-action" id="btn-integrations" hidden>插件</button>
        </nav>
        <div class="vx-sidebar-section">
          <div class="vx-sidebar-heading vx-project-heading">
            <span class="vx-sidebar-row-icon"><svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M3.5 6.5h5l1.4 1.7h6.6v7.3a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2Z"/><path d="M3.5 6.5V5a2 2 0 0 1 2-2h3.2l1.4 1.7h4.4a2 2 0 0 1 2 2v1.7"/></svg></span>
            <span class="vx-project-heading-label">项目</span>
            <button type="button" class="vx-project-add" id="btn-open-workspace" title="打开项目" aria-label="打开项目">+</button>
          </div>
          <div class="vx-session-list" id="session-list"></div>
        </div>
        <div class="vx-sidebar-footer">
          <button type="button" class="vx-nav-item" id="btn-settings">设置</button>
          <button type="button" class="vx-nav-item" id="btn-account">账户</button>
        </div>
      </aside>
      <div class="vx-sidebar-resizer" id="sidebar-resizer" role="separator" aria-orientation="vertical" aria-label="调整侧栏宽度"></div>
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
            <label class="vx-select-label" for="provider-select">
              <select id="provider-select" aria-label="Provider"></select>
            </label>
            <label class="vx-select-label" for="model-select">
              <select id="model-select" aria-label="Model"></select>
            </label>
            <button type="submit" class="btn-send" id="btn-send" aria-label="Send">↑</button>
          </div>
        </form>
        <div class="context-menu" id="context-menu" hidden>
          <div class="context-menu-title">添加</div>
          <button type="button" class="context-menu-item" data-action="file">
            <span class="context-menu-icon">⌘</span>
            <span>文件和文件夹</span>
            <span class="context-menu-detail">添加路径到上下文</span>
          </button>
          <button type="button" class="context-menu-item" data-action="model">
            <span class="context-menu-icon">◇</span>
            <span>供应商 / 模型</span>
            <span class="context-menu-detail">新增或配置模型</span>
          </button>
          <button type="button" class="context-menu-item" data-action="paste">
            <span class="context-menu-icon">▣</span>
            <span>剪贴板图片</span>
            <span class="context-menu-detail">粘贴图片上下文</span>
          </button>
          <div class="context-menu-title">插件</div>
          <button type="button" class="context-menu-item" data-action="skills">
            <span class="context-menu-icon">◆</span>
            <span>技能</span>
            <span class="context-menu-detail">管理 Skills</span>
          </button>
          <button type="button" class="context-menu-item" data-action="integrations">
            <span class="context-menu-icon">◎</span>
            <span>插件</span>
            <span class="context-menu-detail">MCP / Web / LSP</span>
          </button>
        </div>
        <div class="vx-context-row" id="context-row" hidden>
          <span id="context-workspace">voidx</span>
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
      <div class="vx-dock-strip" id="dock-strip" hidden>
        <span class="status-session" id="status-session"></span>
        <span id="strip-workspace">voidx</span>
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
  if (strip) strip.hidden = true;
  const contextRow = document.querySelector<HTMLElement>("#context-row");
  if (contextRow) contextRow.hidden = true;
});
