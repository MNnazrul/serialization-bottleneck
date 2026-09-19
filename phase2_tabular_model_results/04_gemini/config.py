from harness import ModelConfig, disable_thinking_gemini

CONFIG = ModelConfig(
    name="gemini",
    model="gemini-2.5-flash-lite",
    provider="google",
    api_key_env="GEMINI_API_KEY",
    price_in=0.10,
    price_out=0.40,
    thinking_disable=disable_thinking_gemini,
    json_mode=True,
)
