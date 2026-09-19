from harness import ModelConfig, disable_thinking_deepseek

CONFIG = ModelConfig(
    name="v4flash",
    model="deepseek-v4-flash",
    provider="deepseek",
    api_key_env="DEEPSEEK_API_KEY",
    price_in=0.14,
    price_out=0.28,
    thinking_disable=disable_thinking_deepseek,
    json_mode=False,
)
