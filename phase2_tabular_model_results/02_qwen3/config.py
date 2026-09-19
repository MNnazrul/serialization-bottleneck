from harness import ModelConfig, disable_thinking_openrouter

CONFIG = ModelConfig(
    name="qwen3",
    model="qwen/qwen3-32b",
    provider="openrouter",
    api_key_env="OPENROUTER_API_KEY",
    price_in=0.08,
    price_out=0.28,
    thinking_disable=disable_thinking_openrouter,
    # The reasoning-disable flag isn't reliably honored by every upstream
    # provider OpenRouter routes to; Qwen3's chat template does honor this
    # token regardless of route, so both are sent (mirrors the geometry
    # domain's Phase 2 harness, which found the API flag alone insufficient).
    prompt_suffix=" /no_think",
    json_mode=False,
)
