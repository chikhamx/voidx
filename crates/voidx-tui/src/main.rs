//! voidx TUI — pure Rust terminal interface powered by ratatui.
//!
//! Replaces the Python PureTui + Rich stack.

mod app;
mod input;
mod ui;

use app::App;
use clap::Parser;
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::ExecutableCommand;
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use std::io::{self, stdout};
use std::path::PathBuf;
use std::sync::Arc;
use voidx_agent::VoidXAgent;
use voidx_llm::create_client;
use voidx_memory::SessionStore;
use voidx_permission::PermissionEngine;

/// voidx — a coding agent in your terminal.
#[derive(Parser, Debug)]
#[command(name = "voidx", version)]
struct Cli {
    /// Working directory
    #[arg(short = 'w', long, default_value = ".")]
    workspace: PathBuf,

    /// Model name (e.g. claude-haiku-4-5)
    #[arg(short = 'm', long, default_value = "claude-haiku-4-5")]
    model: String,

    /// Provider (e.g. anthropic, deepseek, openai)
    #[arg(short = 'p', long, default_value = "anthropic")]
    provider: String,

    /// API key (or set ANTHROPIC_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY env var)
    #[arg(long)]
    api_key: Option<String>,

    /// Resume a session by ID
    #[arg(short = 'r', long)]
    resume: Option<String>,

    /// Force new session
    #[arg(short = 'n', long)]
    new_session: bool,
}

#[tokio::main]
async fn main() -> io::Result<()> {
    tracing_subscriber::fmt().init();

    let cli = Cli::parse();

    // Resolve API key
    let api_key = resolve_key(&cli);

    // Setup terminal
    enable_raw_mode()?;
    stdout().execute(EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout());
    let mut terminal = Terminal::new(backend)?;

    // Build app state
    let mut app = App::new(cli.workspace.clone(), &cli.model, &cli.provider);

    // Show startup
    app.push_message("system", &format!(
        "voidx · {} / {}\nWorkspace: {}\nType /help for commands, Ctrl+D to quit.",
        cli.provider, cli.model,
        cli.workspace.display(),
    ));

    // Initialize Rust agent
    app.set_status("Initializing...");
    match init_agent(&cli, &api_key) {
        Ok(agent) => {
            app.set_agent(agent);
            app.set_status("Ready");
        }
        Err(e) => {
            app.set_status(&format!("No agent: {e} — use /model to configure"));
        }
    }

    // ── Main input loop ──────────────────────────────────────────────
    let result = app.run(&mut terminal).await;

    // Cleanup
    disable_raw_mode()?;
    stdout().execute(LeaveAlternateScreen)?;

    result
}

fn init_agent(cli: &Cli, api_key: &str) -> Result<VoidXAgent, String> {
    let config = voidx_config::Config {
        workspace: cli.workspace.clone(),
        model: voidx_config::ModelConfig {
            provider: cli.provider.clone(),
            model: cli.model.clone(),
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
    };

    let client = create_client(&config.model, api_key).map_err(|e| e.to_string())?;

    let db_path = config.workspace.join(".voidx/sessions.db");
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let store = SessionStore::open(&db_path).map_err(|e| e.to_string())?;
    store.migrate().map_err(|e| e.to_string())?;

    let permission = PermissionEngine::new(
        config.sandbox_mode,
        config.sandbox_workspace_write,
        config.approval_policy,
        vec![],
    );

    Ok(VoidXAgent::new(
        config,
        client,
        Arc::new(std::sync::Mutex::new(store)),
        permission,
    ))
}

fn resolve_key(cli: &Cli) -> String {
    if let Some(ref key) = cli.api_key {
        return key.clone();
    }
    let envs = [
        ("ANTHROPIC_API_KEY", &cli.provider == "anthropic"),
        ("DEEPSEEK_API_KEY", &cli.provider == "deepseek"),
        ("OPENAI_API_KEY", &cli.provider == "openai"),
    ];
    for (var, matches) in &envs {
        if *matches || cli.provider == var.trim_end_matches("_API_KEY").to_lowercase() {
            if let Ok(val) = std::env::var(var) {
                return val;
            }
        }
    }
    String::new()
}
