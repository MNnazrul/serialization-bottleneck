from harness import ModelConfig

CONFIG = ModelConfig(
    name="llama_scout",
    model="meta-llama/llama-4-scout",
    provider="openrouter",
    api_key_env="OPENROUTER_API_KEY",
    price_in=0.10,
    price_out=0.30,
    # No thinking mode to disable -- Llama 4 Scout has none.
    thinking_disable=None,
    # JSON mode is the fix for content-level chain-of-thought overrunning
    # the 512-token cap on hard-tier tables (see the geometry Phase 2 docs'
    # observed contrast between JSON-mode and non-JSON-mode completion
    # lengths). The question text is unchanged either way.
    json_mode=True,
)
