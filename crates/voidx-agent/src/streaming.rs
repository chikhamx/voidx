//! Streaming renderer — processes StreamEvents into user-visible output.
//!
//! Ported from `src/voidx/agent/graph_components/streaming.rs`.

use voidx_llm::streaming::StreamEvent;

/// Accumulates stream events into a structured result.
#[derive(Debug, Default)]
pub struct StreamAccumulator {
    pub text: String,
    pub tool_call_ids: Vec<String>,
    pub tool_call_names: Vec<String>,
    pub tool_call_args: Vec<String>,
    pub thinking: String,
}

impl StreamAccumulator {
    pub fn new() -> Self {
        Self::default()
    }

    /// Process a single stream event.
    pub fn feed(&mut self, event: &StreamEvent) {
        match event {
            StreamEvent::TextDelta(delta) => {
                self.text.push_str(delta);
            }
            StreamEvent::ToolCallStart { id, name } => {
                self.tool_call_ids.push(id.clone());
                self.tool_call_names.push(name.clone());
                self.tool_call_args.push(String::new());
            }
            StreamEvent::ToolCallDelta { args_delta, .. } => {
                if let Some(last) = self.tool_call_args.last_mut() {
                    last.push_str(args_delta);
                }
            }
            StreamEvent::Thinking(thought) => {
                self.thinking.push_str(thought);
            }
            StreamEvent::MessageComplete => {}
            StreamEvent::Usage(_) => {}
        }
    }

    /// Check if the response includes any tool calls.
    pub fn has_tool_calls(&self) -> bool {
        !self.tool_call_names.is_empty()
    }
}
