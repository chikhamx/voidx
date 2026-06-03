"""Quick test — call RustAgent directly, no TUI."""
import os
import sys

# Ensure we can import voidx_core from current dir
sys.path.insert(0, os.path.dirname(__file__))

import voidx_core

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY or DEEPSEEK_API_KEY")
        sys.exit(1)

    provider = "anthropic" if "ANTHROPIC" in os.environ else "deepseek"
    # Use cheap/fast models for testing
    model = "claude-haiku-4-5" if provider == "anthropic" else "deepseek-v4-flash"

    print(f"Provider: {provider}, Model: {model}")

    # Build config
    model_cfg = voidx_core.ModelConfig(provider=provider, model=model)
    cfg = voidx_core.Config(workspace=".", model=model_cfg)

    # Create agent
    agent = voidx_core.RustAgent(cfg, api_key)
    agent.initialize()
    print("Agent initialized.")

    # Run a simple query
    user_input = "List the files in the current directory and summarize what this project is."
    print(f"\nUser: {user_input}")
    print("-" * 40)

    result = agent.run(user_input, "test-session")
    print(result.output)
    print("-" * 40)
    print(f"Steps: {result.steps}, Messages: {result.message_count}")

if __name__ == "__main__":
    main()
