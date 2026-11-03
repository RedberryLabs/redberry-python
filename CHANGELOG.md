# Changelog

## 0.1.0

First public release.

- `init(agent=...)`: records model and tool calls with OpenTelemetry and sends them to the Redberry install in the customer's cloud. On Google Cloud (Cloud Run), AWS (Lambda, ECS) and Azure (Functions, Container Apps) the install is found automatically and requests are authenticated with the platform's own identity; elsewhere an explicit endpoint can be set.
- `flush()` and `@flush_after` for serverless functions, which may be frozen after each request.
- `internal_call()` marks model calls an agent makes for itself.
- Instrumentation extras for model providers, agent frameworks and vector databases.
