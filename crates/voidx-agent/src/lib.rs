//! Agent orchestration engine — state machine, 5-agent system, run loop.
//!
//! Ported from `src/voidx/agent/graph.py` + related modules.

pub mod agents;
pub mod compaction;
pub mod error;
pub mod prompt;
pub mod run_loop;
pub mod state;
pub mod streaming;
pub mod subagent;

pub use agents::AgentDef;
pub use error::AgentError;
pub use state::AgentState;

use std::sync::{Arc, Mutex};
use tokio::sync::RwLock;
use voidx_config::Config;
use voidx_llm::ChatClient;
use voidx_memory::SessionStore;
use voidx_permission::PermissionEngine;
use voidx_tools::registry::ToolRegistry;

/// The main voidx agent — wires all subsystems together.
pub struct VoidXAgent {
    pub config: Arc<Config>,
    pub client: Arc<dyn ChatClient>,
    pub tools: Arc<RwLock<ToolRegistry>>,
    pub memory: Arc<Mutex<SessionStore>>,
    pub permission: PermissionEngine,
    pub agents: Vec<agents::AgentDef>,
    pub debug: bool,
}

impl VoidXAgent {
    pub fn new(
        config: Config,
        client: Arc<dyn ChatClient>,
        memory: Arc<Mutex<SessionStore>>,
        permission: PermissionEngine,
    ) -> Self {
        Self {
            config: Arc::new(config),
            client,
            tools: Arc::new(RwLock::new(ToolRegistry::new())),
            memory,
            permission,
            agents: agents::builtin_agents(),
            debug: true,
        }
    }
}
