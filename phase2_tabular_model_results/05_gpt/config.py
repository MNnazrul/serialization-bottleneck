from harness import ModelConfig

CONFIG = ModelConfig(
    name="gpt",
    model="gpt-4.1-mini",
    provider="openai",
    api_key_env="OPENAI_API_KEY",
    price_in=0.40,
    price_out=1.60,
    thinking_disable=None,  # GPT-4.1 Mini has no thinking mode
    json_mode=True,
)
