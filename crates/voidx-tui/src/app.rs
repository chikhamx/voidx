//! Application state and main event loop.

use crate::input::InputState;
use crate::ui;
use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use ratatui::backend::Backend;
use ratatui::Terminal;
use std::io;
use std::path::PathBuf;
use voidx_agent::run_loop;
use voidx_agent::VoidXAgent;
use voidx_llm::ChatMessage;

/// Interactive selection for /model flow.
enum SelectorMode {
    Provider,   // choose provider
    Model,      // choose model for selected provider
    ApiKey,     // entering API key
}

pub struct App {
    pub workspace: PathBuf,
    pub model: String,
    pub provider: String,
    pub messages: Vec<ChatMessage>,
    pub system_lines: Vec<String>,
    pub status: String,
    pub input: InputState,
    pub agent: Option<VoidXAgent>,
    pub running: bool,

    // ── Selector state for interactive /model ──────────────────────
    pub selector_active: bool,
    selector_mode: Option<SelectorMode>,
    selector_items: Vec<String>,
    selector_idx: usize,
    selector_provider: String,
    api_key_buffer: String,
}

impl App {
    pub fn new(workspace: PathBuf, model: &str, provider: &str) -> Self {
        Self {
            workspace,
            model: model.to_string(),
            provider: provider.to_string(),
            messages: Vec::new(),
            system_lines: Vec::new(),
            status: "Starting...".to_string(),
            input: InputState::new(),
            agent: None,
            running: true,
            selector_active: false,
            selector_mode: None,
            selector_items: Vec::new(),
            selector_idx: 0,
            selector_provider: String::new(),
            api_key_buffer: String::new(),
        }
    }

    pub fn set_agent(&mut self, agent: VoidXAgent) {
        self.agent = Some(agent);
    }

    pub fn set_status(&mut self, s: &str) {
        self.status = s.to_string();
    }

    pub fn push_message(&mut self, role: &str, content: &str) {
        self.system_lines.push(format!("[{}] {}", role, content));
    }

    pub fn push_chat(&mut self, role: &str, content: &str) {
        let msg = match role {
            "user" => ChatMessage::user(content),
            "assistant" => ChatMessage::assistant(content),
            _ => ChatMessage::system(content),
        };
        self.messages.push(msg);
    }

    pub async fn run<B: Backend>(&mut self, terminal: &mut Terminal<B>) -> io::Result<()> {
        while self.running {
            terminal.draw(|f| ui::render(f, self))?;

            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Release {
                    continue;
                }

                // ── Selector mode: arrow keys + enter ────────────────
                if self.selector_active {
                    match key.code {
                        KeyCode::Up => {
                            if self.selector_idx > 0 {
                                self.selector_idx -= 1;
                            }
                        }
                        KeyCode::Down => {
                            if self.selector_idx + 1 < self.selector_items.len() {
                                self.selector_idx += 1;
                            }
                        }
                        KeyCode::Enter => {
                            self.selector_confirm().await;
                        }
                        KeyCode::Esc => {
                            self.selector_cancel();
                        }
                        KeyCode::Backspace => {
                            if matches!(self.selector_mode, Some(SelectorMode::ApiKey)) {
                                self.api_key_buffer.pop();
                            }
                        }
                        KeyCode::Char(c) => {
                            if matches!(self.selector_mode, Some(SelectorMode::ApiKey)) {
                                self.api_key_buffer.push(c);
                            }
                        }
                        _ => {}
                    }
                    continue;
                }

                // ── Normal mode ──────────────────────────────────────
                match key.code {
                    KeyCode::Char('d') if key.modifiers.contains(event::KeyModifiers::CONTROL) => {
                        self.running = false;
                    }
                    KeyCode::Enter => {
                        let text = self.input.take();
                        if text.trim().is_empty() {
                            continue;
                        }
                        self.handle_input(&text).await;
                    }
                    KeyCode::Char(c) => {
                        self.input.push(c);
                    }
                    KeyCode::Backspace => {
                        self.input.backspace();
                    }
                    KeyCode::Esc => {
                        if self.input.text().starts_with('/') {
                            self.input.clear();
                        }
                    }
                    _ => {}
                }
            }
        }
        Ok(())
    }

    async fn handle_input(&mut self, text: &str) {
        let text = text.trim().to_string();

        if text.starts_with('/') {
            self.handle_slash(&text).await;
            return;
        }

        self.push_chat("user", &text);

        let has_agent = self.agent.is_some();
        if has_agent {
            self.set_status("Thinking...");
            let agent = self.agent.take().unwrap();
            let mut state = voidx_agent::state::AgentState::new(&text);
            match run_loop::run(&agent, &mut state, "tui-session").await {
                Ok(result) => {
                    let reply: String = result
                        .messages
                        .iter()
                        .rev()
                        .filter_map(|m| match m {
                            ChatMessage::Assistant { content, .. } => Some(content.clone()),
                            _ => None,
                        })
                        .collect::<Vec<_>>()
                        .join("\n");

                    self.push_chat("assistant", &reply);
                    self.set_status(&format!(
                        "Done — {} steps, {} msgs",
                        result.steps,
                        result.messages.len(),
                    ));
                }
                Err(e) => {
                    self.push_chat("assistant", &format!("Error: {e}"));
                    self.set_status("Error");
                }
            }
            self.agent = Some(agent);
        } else {
            self.push_chat(
                "assistant",
                "No agent configured. Use /model to set up a provider and model.",
            );
        }
    }

    async fn handle_slash(&mut self, text: &str) {
        let parts: Vec<&str> = text.split_whitespace().collect();
        let cmd = parts.first().map(|s| s.to_lowercase()).unwrap_or_default();

        match cmd.as_str() {
            "/help" => {
                self.push_message("system",
                    "/model   — interactively select provider & model\n/models  — list all available models\n/mode auto|plan — set interaction mode\n/quit    — exit");
            }
            "/models" => {
                let providers = voidx_llm::catalog::providers();
                let mut out = String::from("Available models:\n");
                for p in providers {
                    let models = voidx_llm::catalog::list_models(p);
                    out.push_str(&format!("\n{p}:\n"));
                    for m in models.iter().take(8) {
                        out.push_str(&format!("  {m}\n"));
                    }
                }
                self.push_message("system", &out);
            }
            "/model" => {
                // Start interactive provider selection
                self.selector_mode = Some(SelectorMode::Provider);
                self.selector_items = voidx_llm::catalog::providers()
                    .iter()
                    .map(|s| s.to_string())
                    .collect();
                self.selector_idx = 0;
                self.selector_active = true;
                self.set_status("Select provider: ↑↓ to navigate, Enter to choose, Esc to cancel");
            }
            "/mode" => {
                if parts.len() >= 2 {
                    self.push_message("system", &format!("Mode: {}", parts[1]));
                } else {
                    self.push_message("system", "Usage: /mode <auto|plan>");
                }
            }
            "/quit" => self.running = false,
            _ => {
                self.push_message("system", &format!("Unknown: {cmd}. /help for commands."));
            }
        }
    }

    // ── Selector methods ───────────────────────────────────────────────

    async fn selector_confirm(&mut self) {
        match self.selector_mode.take() {
            Some(SelectorMode::Provider) => {
                if let Some(provider) = self.selector_items.get(self.selector_idx) {
                    self.selector_provider = provider.clone();
                    // Show models for this provider
                    self.selector_mode = Some(SelectorMode::Model);
                    self.selector_items = voidx_llm::catalog::list_models(provider)
                        .iter()
                        .map(|s| s.to_string())
                        .collect();
                    self.selector_idx = 0;
                    self.set_status(&format!(
                        "Provider: {} — now select a model",
                        self.selector_provider
                    ));
                }
            }
            Some(SelectorMode::Model) => {
                if let Some(model) = self.selector_items.get(self.selector_idx) {
                    self.provider = self.selector_provider.clone();
                    self.model = model.clone();
                    // Prompt for API key
                    self.selector_mode = Some(SelectorMode::ApiKey);
                    self.api_key_buffer.clear();
                    self.set_status(&format!(
                        "Selected {}/{} — enter API key (or press Enter to use env var):",
                        self.provider, self.model
                    ));
                }
            }
            Some(SelectorMode::ApiKey) => {
                let key = if self.api_key_buffer.trim().is_empty() {
                    // Try env var
                    let env_var = format!("{}_API_KEY", self.provider.to_uppercase());
                    std::env::var(&env_var).unwrap_or_default()
                } else {
                    self.api_key_buffer.trim().to_string()
                };

                if key.is_empty() {
                    self.push_message(
                        "system",
                        &format!(
                            "No API key. Set {}_API_KEY env var or enter key manually.",
                            self.provider.to_uppercase()
                        ),
                    );
                } else {
                    self.push_message(
                        "system",
                        &format!("Switched to {} / {}", self.provider, self.model),
                    );
                    self.rebuild_agent(&key);
                }

                self.selector_active = false;
                self.api_key_buffer.clear();
            }
            None => {}
        }
    }

    fn selector_cancel(&mut self) {
        self.selector_active = false;
        self.selector_mode = None;
        self.selector_items.clear();
        self.selector_idx = 0;
        self.api_key_buffer.clear();
        self.set_status("Ready");
    }

    fn rebuild_agent(&mut self, api_key: &str) {
        use voidx_llm::create_client;
        use voidx_memory::SessionStore;
        use voidx_permission::PermissionEngine;
        use std::sync::{Arc, Mutex};

        let config = voidx_config::Config {
            workspace: self.workspace.clone(),
            model: voidx_config::ModelConfig {
                provider: self.provider.clone(),
                model: self.model.clone(),
                protocol: None,
                base_url: None,
                temperature: 0.7,
                max_tokens: 8192,
                reasoning_effort: None,
            },
            sandbox_mode: voidx_config::SandboxMode::WorkspaceWrite,
            sandbox_workspace_write: false,
            approval_policy: voidx_config::ApprovalPolicy::Untrusted,
            approval_reviewer: voidx_config::ApprovalReviewer::User,
            permission_mode: voidx_config::PermissionMode::Default,
            sandbox_extra_paths: Vec::new(),
            code_ide: voidx_config::CodeIde::Auto,
        };

        match create_client(&config.model, api_key) {
            Ok(client) => {
                let db_path = config.workspace.join(".voidx/sessions.db");
                if let Some(parent) = db_path.parent() {
                    std::fs::create_dir_all(parent).ok();
                }
                match SessionStore::open(&db_path) {
                    Ok(store) => {
                        store.migrate().ok();
                        let permission = PermissionEngine::new(
                            config.sandbox_mode,
                            config.sandbox_workspace_write,
                            config.approval_policy,
                            vec![],
                        );
                        self.agent = Some(VoidXAgent::new(
                            config,
                            client,
                            Arc::new(Mutex::new(store)),
                            permission,
                        ));
                        self.set_status(&format!("Ready — {} / {}", self.provider, self.model));
                    }
                    Err(e) => {
                        self.set_status(&format!("Store error: {e}"));
                    }
                }
            }
            Err(e) => {
                self.set_status(&format!("API error: {e}"));
            }
        }
    }

    /// Get the selector display info for rendering.
    pub fn selector_info(&self) -> Option<(&[String], usize, String)> {
        if !self.selector_active {
            return None;
        }
        let title = match self.selector_mode {
            Some(SelectorMode::Provider) => "Choose Provider".to_string(),
            Some(SelectorMode::Model) => format!("Models for {}", self.selector_provider),
            Some(SelectorMode::ApiKey) => "Enter API Key".to_string(),
            None => String::new(),
        };
        Some((&self.selector_items, self.selector_idx, title))
    }

    pub fn api_key_input(&self) -> Option<&str> {
        if matches!(self.selector_mode, Some(SelectorMode::ApiKey)) {
            Some(&self.api_key_buffer)
        } else {
            None
        }
    }
}
