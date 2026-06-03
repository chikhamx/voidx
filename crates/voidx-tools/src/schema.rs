//! JSON Schema generation from Pydantic-like input models.
//!
//! Uses `schemars` to auto-derive JSON Schema for tool parameters.

use schemars::JsonSchema;
/// Convert a type that implements `JsonSchema` into a JSON Schema dict.
/// This is the Rust equivalent of Python's `model_to_json_schema()`.
pub fn model_to_json_schema<T: JsonSchema>() -> serde_json::Value {
    let schema = schemars::schema_for!(T);
    let value = serde_json::to_value(&schema).unwrap_or_default();

    // Strip top-level keys that LLM tool schemas don't need
    let mut clean = serde_json::json!({
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": false,
    });

    if let Some(props) = value.get("properties").cloned() {
        clean["properties"] = props;
    }
    if let Some(required) = value.get("required").cloned() {
        clean["required"] = required;
    }

    clean
}
