# Changelog

## Unreleased

- **Anthropic API version `2026-10-21`.** That version, the default from the Python `anthropic` 2.0.0, rejects
  `temperature`, `top_p` and `top_k` on every model and samples at temperature 1.0, so the agent cannot state its
  temperature in the request. On each Anthropic `Messages` call the SDK now records the `anthropic-version` the call
  used (`redberry.llm.provider_api_version`, and how it was learnt in `redberry.llm.provider_api_version_source`).
  On `2026-10-21` or later, with no temperature sent, it also records `redberry.llm.temperature_inferred = 1.0` and
  `redberry.llm.temperature_source = provider_fixed_default`, so Redberry can price the agent. This applies only to
  the `Anthropic` and `AsyncAnthropic` clients calling api.anthropic.com. Calls on `2023-06-01` must still send
  temperature 1.0.

## 0.2.0

- When the install cannot be found, the SDK now logs an error with a code and the fix (`RB-SDK-DISC-001`
  to `-003`), instead of a warning. A named agent on no known cloud with no endpoint set is a coded
  warning (`RB-SDK-DISC-004`); it was silent.
- The SDK recovers on its own: discovery is tried again every 5 minutes (`REDBERRY_DISCOVERY_RETRY_S`), and once
  it succeeds spans are sent to the install, with no restart. Until then nothing is sent to `localhost`.
- For an hour after recovering, spans carry `redberry.sdk.discovery_failure` (the code and how long it lasted), so
  the install can show it.
- Install hints and the trace envelope name this package, `redberry-sdk`.

## 0.1.0

First public release.

- `init(agent=...)`: records model and tool calls with OpenTelemetry and sends them to the Redberry install in the customer's cloud. On Google Cloud (Cloud Run), AWS (Lambda, ECS) and Azure (Functions, Container Apps) the install is found automatically and requests are authenticated with the platform's own identity; elsewhere an explicit endpoint can be set.
- `flush()` and `@flush_after` for serverless functions, which may be frozen after each request.
- `internal_call()` marks model calls an agent makes for itself.
- Instrumentation extras for model providers, agent frameworks and vector databases.
