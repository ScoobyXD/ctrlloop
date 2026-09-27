# Product model access

Verified 2026-09-27 against official OpenAI documentation and the authorized AWS development credential.

The product's requested Luna model is pinned to **`gpt-6-luna`**, using the Responses API with `reasoning: {"effort": "high"}`. OpenAI's [GPT-6 Luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna) lists this exact model ID and snapshot and explicitly supports high reasoning. Jev remains a separate selection model, pinned to `jev-1.13.0`.

A real minimal access check used `POST https://api.openai.com/v1/responses`, `model: "gpt-6-luna"`, high reasoning, and `store: false`. It completed and returned `READY` with response model `gpt-6-luna`. Usage was 11 input tokens, 17 output tokens (10 reasoning), 28 total. Response ID: `resp_0ffea69a02cc8eba016ab98872557087d093d79b06d018021f`. This access check is separate from the application's actual user instruction generation and is not a canned app response.

The credential is stored as AWS Secrets Manager secret `mangorts/dev/openai-api-key` in the existing development account, region `us-west-1`. It has inference permission but lacks `api.model.read`; therefore listing `/v1/models` returned an insufficient-permissions error. The successful actual Responses call verifies this exact model's access without requiring a broader key.

Provider keys are injected into the server container by ECS Secrets Manager integration. They must not appear in frontend assets, source bundles, logs, commits, or browser responses. The isolated worker receives only its app delivery token and its existing Jev credential, not the product LLM key.
