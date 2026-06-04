//! Type contracts for agent components.
//!
//! Ported from `src/voidx/agent/graph/contracts.py`.
//! These traits define the shared surface that agent components depend on,
//! decoupling the graph from concrete implementations.

use crate::state::{AgentState, InteractionMode};
use async_trait::async_trait;
use voidx_config::Config;
use voidx_llm::{ChatClient, ChatMessage};
use voidx_memory::SessionStore;
use voidx_permission::PermissionEngine;
use voidx_tools::base::ToolContext;
use voidx_tools::registry::ToolRegistry;

/// Core host trait — the shared surface required by agent components.
///
/// This replaces the Python `GraphComponentHost` protocol, making the
/// dependency graph explicit without coupling to a concrete struct.
#[async_trait]
pub trait AgentHost: Send + Sync {
    /// The active configuration.
    fn config(&self) -> &Config;

    /// The LLM client.
    fn client(&self) -> &dyn ChatClient;

    /// The tool registry.
    fn tools(&self) -> &ToolRegistry;

    /// The permission engine.
    fn permission(&self) -> &PermissionEngine;

    /// The session store.
    fn session(&self) -> &SessionStore;

    /// Current interaction mode.
    fn interaction_mode(&self) -> InteractionMode;

    /// Whether debug mode is active.
    fn debug(&self) -> bool;

    /// Execute a single tool call and return the result.
    async fn execute_tool(
        &self,
        tool_name: &str,
        args: serde_json::Value,
        ctx: &ToolContext,
    ) -> Result<String, crate::error::AgentError>;

    /// Run compaction on the given messages.
    async fn compact_messages(
        &self,
        messages: &[ChatMessage],
        threshold_pct: f64,
    ) -> Result<String, crate::error::AgentError>;

    /// Persist runtime state.
    async fn persist_runtime_state(&self, state: &AgentState) -> Result<(), crate::error::AgentError>;

    /// Restore runtime state.
    async fn restore_runtime_state(&self) -> Result<AgentState, crate::error::AgentError>;

    /// Persist a transcript snapshot.
    async fn persist_transcript(&self, messages: &[ChatMessage]) -> Result<(), crate::error::AgentError>;
}

/// Trait for components that can process a step in the agent loop.
#[async_trait]
pub trait StepProcessor: Send + Sync {
    /// Process one step: given the current state, return updated messages.
    async fn process(
        &self,
        host: &dyn AgentHost,
        state: &mut AgentState,
    ) -> Result<StepOutcome, crate::error::AgentError>;
}

/// Outcome of a step processor.
#[derive(Debug)]
pub enum StepOutcome {
    /// Continue the loop with these new messages.
    Continue(Vec<ChatMessage>),
    /// The agent is done; here's the final message.
    Done(ChatMessage),
    /// The agent hit a limit.
    LimitReached(String),
}

/// Trait for tool authorization strategies.
pub trait ToolAuthorizer: Send + Sync {
    /// Authorize a tool call. Returns true if allowed.
    fn authorize(
        &self,
        tool_name: &str,
        args: &serde_json::Value,
        agent_name: &str,
        interaction_mode: InteractionMode,
    ) -> Authorization;

    /// Batch-authorize multiple tool calls.
    fn authorize_batch(
        &self,
        calls: &[(String, serde_json::Value)],
        agent_name: &str,
        interaction_mode: InteractionMode,
    ) -> Vec<Authorization> {
        calls
            .iter()
            .map(|(name, args)| self.authorize(name, args, agent_name, interaction_mode))
            .collect()
    }
}

/// Result of tool authorization.
#[derive(Debug, Clone)]
pub enum Authorization {
    Allowed,
    Denied(String),
    NeedsApproval(String),
}

/// Trait for message sanitization before replay to the LLM.
pub trait MessageSanitizer: Send + Sync {
    /// Sanitize a tool result message.
    fn sanitize_tool_result(
        &self,
        content: &str,
        workspace: Option<&str>,
        max_chars: usize,
    ) -> String;

    /// Sanitize messages for replay (remove reasoning blocks, fix adjacency).
    fn sanitize_for_replay(&self, messages: &[ChatMessage]) -> Vec<ChatMessage>;
}

/// Default implementation that delegates to tool_messages module.
pub struct DefaultMessageSanitizer;

impl MessageSanitizer for DefaultMessageSanitizer {
    fn sanitize_tool_result(
        &self,
        content: &str,
        workspace: Option<&str>,
        max_chars: usize,
    ) -> String {
        crate::tool_messages::sanitize_tool_message_content(content, workspace, max_chars)
    }

    fn sanitize_for_replay(&self, messages: &[ChatMessage]) -> Vec<ChatMessage> {
        crate::streaming::sanitize_messages_for_replay(messages)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_authorization_allowed() {
        assert!(matches!(Authorization::Allowed, Authorization::Allowed));
    }

    #[test]
    fn test_authorization_denied() {
        let d = Authorization::Denied("nope".to_string());
        assert!(matches!(d, Authorization::Denied(_)));
    }
}
