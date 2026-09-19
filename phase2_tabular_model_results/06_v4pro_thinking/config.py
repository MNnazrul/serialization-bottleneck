from harness import ModelConfig

CONFIG = ModelConfig(
    name="v4pro_thinking",
    model="deepseek-v4-pro",
    provider="deepseek",
    api_key_env="DEEPSEEK_API_KEY",
    price_in=0.435,
    price_out=0.87,
    # PDF Section 2 specifies 2048, but the first full run hit finish_reason
    # "length" at exactly 2048 completion tokens on 191/588 (32.5%) of
    # queries -- all classified reasoning_truncated, none parse_failure --
    # which exceeds PDF 6.1's 15% investigate-before-proceeding threshold.
    # 6.1's own guidance for this exact symptom: "the max token limit needs
    # to be increased." Doubled for the retry of just those 191 queries.
    max_tokens=4096,
    json_mode=False,
    default_subset="subsample_v4pro_thinking.json",
)
