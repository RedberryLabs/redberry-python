# Changelog

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
