# redberry-sdk

Records an AI agent's model and tool calls with OpenTelemetry and sends them to the customer's own Redberry
install. Redberry uses these records to keep a tamper-evident evidence chain for each agent and to price its risk.

The SDK sends only to the Redberry install in your own cloud account (or to an endpoint you set). It does not
send anything to Redberry directly.

## Install

```bash
pip install redberry-sdk                    # core
pip install "redberry-sdk[providers]"       # plus instrumentation for model providers (OpenAI, Anthropic, ...)
pip install "redberry-sdk[all]"             # providers, agent frameworks and vector databases
```

Python 3.11 or later.

| Extra | Installs instrumentation for |
|---|---|
| `providers` | OpenAI, Anthropic, Amazon Bedrock, Cohere, Mistral, Ollama, Vertex AI, Google GenAI, Groq, Together, Replicate |
| `frameworks` | LangChain, LlamaIndex, CrewAI, Haystack, OpenAI Agents SDK |
| `vectordb` | Chroma, Pinecone, Qdrant, Weaviate |
| `openai-agents` | the OpenAI Agents SDK itself, for its `valp.*` integration |
| `hasher` | the optional per-run signing path |

## Quick start

```python
import redberry_sdk

redberry_sdk.init(agent="refund-agent")   # your agent's name, as you will approve it in the Redberry portal
```

Call `init()` once, before the agent makes its first model call. Model calls made through supported libraries are
then recorded automatically.

**Serverless functions** (AWS Lambda, Azure Functions, Cloud Run functions) can be frozen as soon as a request
returns. Flush before returning:

```python
@redberry_sdk.flush_after
def handler(event, context):
    ...
```

or call `redberry_sdk.flush()` yourself.

**Temperature.** Redberry prices an agent from its sampled behaviour, so the agent's model calls must run at
temperature 1.0. With the Anthropic API version `2026-10-21` (the default from the Python `anthropic` 2.0.0), send no
temperature: that version rejects it and samples at 1.0, and the SDK records that.

## Where records go

On these platforms the SDK finds your install automatically and authenticates with the platform's own identity;
no keys are needed:

| Platform | How the install is found | Authentication |
|---|---|---|
| Google Cloud (Cloud Run services, jobs, functions) | the install in the same project | the service account's ID token |
| AWS (Lambda, ECS) | `s3://redberry-<account>-<region>/ingest.json` | AWS Signature Version 4 with the role's credentials |
| Azure (Functions, Container Apps) | the Redberry API, by the managed identity | the managed identity's Entra ID token |

Elsewhere, set the ingest address explicitly.

| Environment variable | Meaning |
|---|---|
| `REDBERRY_INGEST_URL` | the install's ingest address (overrides discovery) |
| `REDBERRY_CLOUD` | `gcp`, `aws` or `azure`, when it cannot be detected |
| `REDBERRY_API_URL` | the Redberry API (Azure discovery) |
| `REDBERRY_AZURE_APP_ID_URI` | Redberry's application ID URI in Entra ID, if not the default |
| `REDBERRY_GCP_REGION` | the install's region on Google Cloud, when it cannot be detected |
| `REDBERRY_OTEL_ENDPOINT` | an OTLP/HTTP endpoint to send to instead (for testing) |
| `REDBERRY_DISCOVERY_RETRY_S` | how often, in seconds, finding the install is tried again after it failed (default 300) |
| `REDBERRY_AUTH_RETRY_S` | how long a refused batch is retried while access is being granted (default 300) |
| `REDBERRY_AGENT_ID` | the agent's name, if not passed to `init()` |
| `REDBERRY_ALLOW_MISSING_INSTRUMENTATION` | `1` to start even when a library's instrumentation is not installed |
| `REDBERRY_ALLOW_CONTENT_CAPTURE_OFF` | `1` to record metadata only, deliberately |
| `REDBERRY_MAX_REQUEST_SNAPSHOT_BYTES` | the largest request recorded verbatim |

Settings can also be placed in a `.env` file next to the agent. Settings from earlier versions of the SDK, under a
different prefix, are still accepted.

Records the SDK could not deliver are counted by `redberry_sdk.dropped_spans()`.

## When records cannot be delivered

The SDK never stops your agent. When it cannot find your install, it logs an error naming the cause and keeps
trying every 5 minutes; as soon as the cause is fixed, records are sent again, with no restart or redeploy.

| Code | Cause | Fix |
|---|---|---|
| `RB-SDK-DISC-001` | On Google Cloud, the metadata server reports no region (Compute Engine, GKE) | Set `REDBERRY_GCP_REGION` to the region of your install |
| `RB-SDK-DISC-002` | On AWS, the install's `ingest.json` could not be read | Check that the install ran in this account and region, and that this function was chosen as an agent (run the install again and select it) |
| `RB-SDK-DISC-003` | On Azure, the install could not be found | Check that the app has a managed identity and was chosen as an agent (run the install again and select it) |
| `RB-SDK-DISC-004` | The agent is named, but not on Google Cloud, AWS or Azure, and no endpoint is set | Set `REDBERRY_INGEST_URL` |

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
