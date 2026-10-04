# Example chat configurations

## Individual OpenRouter models

Upload any of these files through **Session Config**, or save it in **Config Library**. Each config has one direct OpenRouter route, uses the same memory and cost controls as the combined config below, and sets `retries: 0`. Requests use only the selected model; a failure returns an error without trying another model. Set `OPENROUTER_API_KEY` in `.env` and restart the Connector API if needed.

| Model | Config file | Reasoning effort |
| --- | --- | --- |
| gpt-oss-20b | [openrouter-gpt-oss-20b.json](openrouter-gpt-oss-20b.json) | Low |
| Gemma 3 27B | [openrouter-gemma-3-27b-it.json](openrouter-gemma-3-27b-it.json) | No override |
| Qwen3 30B A3B Instruct 2507 | [openrouter-qwen3-30b-a3b-instruct-2507.json](openrouter-qwen3-30b-a3b-instruct-2507.json) | No override |
| DeepSeek V4 Flash 0731 | [openrouter-deepseek-v4-flash-0731.json](openrouter-deepseek-v4-flash-0731.json) | Low |
| GPT-5 Nano | [openrouter-gpt-5-nano.json](openrouter-gpt-5-nano.json) | Minimal |
| Grok 4.20 | [openrouter-grok-4.20.json](openrouter-grok-4.20.json) | No override |

## Low-cost OpenRouter

Upload [low-cost-openrouter.json](low-cost-openrouter.json) through **Session Config**, then save it for the current chat or use it for a new chat. You can also save it through **Config Library** for reuse. Set `OPENROUTER_API_KEY` in the Connector's `.env` and restart the API if needed.

The probabilistic selector uses all six models from the supplied tables. Gemma receives 20% of initial selections; the original five-model mix is scaled to the remaining 80%, with rounded weights:

| Model | Initial selection | Reasoning effort |
| --- | ---: | --- |
| `openai/gpt-oss-20b` | 40% | Low |
| `google/gemma-3-27b-it` | 20% | No override; no reasoning control in the catalog |
| `qwen/qwen3-30b-a3b-instruct-2507` | 20% | No override; non-thinking model |
| `deepseek/deepseek-v4-flash-0731` | 12% | Low |
| `openai/gpt-5-nano` | 7% | Minimal |
| `x-ai/grok-4.20` | 1% | No override; reasoning off by default in the catalog |

These weights are a cost-conscious starting mix, not a calculation from the tables' percentages. Historical average spend includes differences in request size and reasoning use; the DeepSeek row appears to have only one observation because its minimum, maximum, average, and sum are equal. The mix favors gpt-oss's low listed token rates, includes both Gemma and Qwen without a reasoning override, and retains occasional access to the remaining models. GPT-5 Nano and Qwen were already present, so the second table adds Gemma without duplicating those choices.

Model IDs and reasoning options were verified against [OpenRouter's public model catalog](https://openrouter.ai/api/v1/models) on 2026-09-27. The catalog listed the following rates in USD per million tokens; provider routing, caching, and later pricing changes can affect actual charges:

| Model | Input / million | Output / million |
| --- | ---: | ---: |
| gpt-oss-20b | $0.018 | $0.09 |
| Gemma 3 27B | $0.08 | $0.45 |
| Qwen3 30B A3B Instruct 2507 | $0.10 | $0.30 |
| DeepSeek V4 Flash 0731 | $0.021 | $0.32 |
| GPT-5 Nano | $0.05 | $0.40 |
| Grok 4.20 | $1.25 | $2.50 |

### Cost controls and routing

- Each choice inherits `retries: 0`, so a failed candidate is attempted once before trying the others. The Connector selects the first candidate randomly, then tries remaining choices in file order. Grok is last among fallback candidates unless it was randomly selected first.
- Past Memory remains enabled and can recall user conversations across saved sessions. A four-message context window and four memory entries bound prompt history. System-session and completion memory are disabled in these examples; enable their source checkboxes when needed.
- `agent_final_retries: 1` allows one additional attempt for an incomplete agent final response instead of the default five. Agent steps and tool calls can still generate additional requests.
- The concise system prompt and low/minimal reasoning settings aim to reduce output usage. Set optional per-model `max_input_tokens` and `max_output_tokens` in the configuration editor to bound input and generated output. These example files leave those limits unset. There is no dollar-budget field; weights control initial selection frequency, not spending percentages or final fallback frequency.

## OpenRouter free-tier probability example

Use [openrouter-free-tier-probabilistic.json](openrouter-free-tier-probabilistic.json) to rotate across the 16 supplied `:free` model IDs. Each choice has equal weight (`probability: 6.25`), totaling 100 across all 16 models, and no retries; on failure, the router moves through the remaining choices in file order. Free-tier model availability and limits are controlled by OpenRouter and may change.
