# Connect an agent to Redberry

This guide connects one AI agent to the customer's Redberry install, so that the agent's model and tool calls are
recorded and priced. It is written to be followed step by step, by a person or by a coding agent working in the
agent's repository.

## Before you start

- **A Redberry install exists** in the customer's cloud account (Google Cloud, AWS or Azure). It is set up from
  the Redberry portal's install command. Without an install there is nowhere to send records; stop and ask.
- **The agent's name.** One to 64 printable characters, the name the customer approves in the Redberry portal.
  Ask for it; do not invent one.
- **Where the agent runs.** It decides the path below.

Rules while connecting an agent:

- Do not deploy. Change the code, show the changes, and leave deploying to the person.
- Model calls must run at **temperature 1.0**. Set it to 1 where the code sets another value. With the Anthropic API
  version `2026-10-21` (the default from the Python `anthropic` 2.0.0), send no temperature at all.
- Never set `REDBERRY_ALLOW_MISSING_INSTRUMENTATION=1` or `REDBERRY_ALLOW_CONTENT_CAPTURE_OFF=1` to get past an
  error. Install the missing instrumentation instead.
- No secret is needed anywhere in this guide. If a step seems to need one, stop and ask.

## 1. Choose the path

| The agent runs on | Path |
|---|---|
| AWS Lambda or Azure Functions | A: the `redberry` command wires it |
| Vercel (Next.js with the Vercel AI SDK) | B: the `redberry` command wires it |
| Anything else, in Python (Cloud Run, ECS, Container Apps, a VM, a container) | C: add `redberry-sdk` |
| Anything else, in Node.js | D: add `@redberrylabs/otel` |

## A. AWS Lambda or Azure Functions

```sh
uv tool install redberry-cli        # or: pipx install redberry-cli
redberry connect --platform aws-lambda --function <function-name> [--region <region>] --name <agent-name>
redberry connect --platform azure-functions --app <app-name> --resource-group <group> --name <agent-name>
```

Run it in the repository that holds the function's code (or pass `--repo`). It reads the function on the cloud, so
the AWS CLI or Azure CLI must be signed in. It shows a diff and asks before writing; it never deploys. The function
must have been chosen as an agent when the install was set up (on Azure, it needs a managed identity). Then deploy
the function as usual.

## B. Vercel

The install prints the Vercel settings (`REDBERRY_CLOUD`, `REDBERRY_INGEST_URL` and the cloud's federation names)
when it is run with `--vercel-team`. Ask for that output and save it to a file, then, in the app's folder:

```sh
uv tool install redberry-cli
redberry connect --platform vercel --name <agent-name> --env-file <install-output-file>
```

It writes `instrumentation.ts`, adds the dependencies and sets the variables on the project's production
environment (or prints them with `--no-env`). Turn on the project's OIDC federation (Settings > Security > Secure
backend access, team issuer). One Vercel project is one agent: to price two agents separately, deploy them as two
projects.

## C. Python, anywhere else

```sh
pip install "redberry-sdk[providers]"     # [all] adds agent frameworks and vector databases
```

At start-up, once, before the first model call:

```python
import redberry_sdk

redberry_sdk.init(agent="<agent-name>")
```

For a function that can be frozen when a request returns, flush before returning:

```python
@redberry_sdk.flush_after
def handler(event, context):
    ...
```

On Google Cloud, AWS and Azure the SDK finds the install itself. Elsewhere, set `REDBERRY_INGEST_URL` to the
install's ingest address, and ask for it if you do not have it.

## D. Node.js, anywhere else

```sh
npm install @redberrylabs/otel @opentelemetry/api @opentelemetry/sdk-trace-base @opentelemetry/context-async-hooks @opentelemetry/resources
```

At start-up, once:

```ts
import { init } from '@redberrylabs/otel/init';

init({ agent: '<agent-name>' });
```

With the Vercel AI SDK, turn on its telemetry (`experimental_telemetry: { isEnabled: true }`, or
`registerTelemetry` with `@ai-sdk/otel` on AI SDK 7).

## 2. Check it works

After the agent is deployed and has made a model call:

- **No error from the SDK at start-up.** `MissingInstrumentationError` names a library without instrumentation:
  install the extra it names (Python: `redberry-sdk[providers]` or `[all]`).
- **No `RB-SDK-DISC-…` code in the logs.** Each code says its cause and fix: the install not found in this account
  or region, the function not chosen as an agent, or no ingest address set.
- **Nothing dropped.** Python: `redberry_sdk.dropped_spans()` is 0. Node.js: the exporter's `droppedSpans()` is 0.

The SDK never stops the agent. When it cannot deliver, it logs why and keeps trying, and sends as soon as the cause
is fixed, with no redeploy.

## Reference

- Python SDK: https://pypi.org/project/redberry-sdk/
- The `redberry` command: https://pypi.org/project/redberry-cli/
- Node.js SDK: https://www.npmjs.com/package/@redberrylabs/otel
