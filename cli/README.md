# redberry-cli

The `redberry` command. It signs a machine in to Redberry for your company, and wires an agent's code on AWS
Lambda, Azure Functions or Vercel so the agent sends its spans to your Redberry install. The agent itself uses
[`redberry-sdk`](https://pypi.org/project/redberry-sdk/) (Python) or
[`@redberrylabs/otel`](https://www.npmjs.com/package/@redberrylabs/otel) (Node.js).

## Install

```sh
uv tool install redberry-cli
```

or `pipx install redberry-cli`. Python 3.11 or later.

## Commands

| Command | What it does |
|---|---|
| `redberry login` | Signs this machine in for your company with a device code that anyone signed in to the Redberry portal for the company approves. The session is kept in `~/.config/redberry/credentials.json` (mode 0600) for 30 days. |
| `redberry whoami` | The company this machine is signed in for. |
| `redberry logout` | Revokes the session at Redberry and forgets it here. |
| `redberry connect` | Without `--platform`: agents on a cloud install are set up from the Redberry portal's install command. |
| `redberry connect --platform aws-lambda` | Wires a Node.js or Python Lambda function (below). |
| `redberry connect --platform azure-functions` | Wires a Node.js (v4) or Python (v2) Azure function app (below). |
| `redberry connect --platform vercel` | Wires a Next.js app on the Vercel AI SDK, and sets the project's `REDBERRY_*` variables (below). |
| `redberry upgrade` | Your company's Redberry installs and whether each runs the latest release; `redberry upgrade <id>` upgrades one (below). |

The API is `https://api.redberry.sbs`; `--api` or `REDBERRY_API_URL` selects another. Every command that calls it
first prints which environment it is using on stderr (`Redberry prod`, `Redberry dev` or `Redberry custom`). `--json` prints a
command's result for scripts.

## What `connect --platform` does

For AWS Lambda and Azure Functions, `connect` reads the function on the cloud (`aws lambda get-function-configuration`, `az functionapp show`),
finds the handler and the model calls it reaches in the code in `--repo` (default: the current folder), shows
the changes as a diff and asks before writing them (`--yes` applies them without asking). It never deploys
and sets nothing on the cloud: there is no key or setting to add, because the SDK finds your install from
where it runs.

**AWS Lambda** (`--function NAME [--region R]`): one agent per function, named by `--name` (default: the
function's name).

- Python: adds `redberry-sdk[providers]` to `requirements.txt`, `redberry_sdk.init(agent="…")` at module load,
  and `@redberry_sdk.flush_after` on the handler, so its spans are sent before Lambda freezes the process.
- Node.js: adds `@redberrylabs/otel` (^0.5.0) and its OpenTelemetry peers to `package.json`, and
  `init({ agent })` from `@redberrylabs/otel/init` at the top of the handler's file, with a small wrapper that
  flushes the spans before the handler returns.
- The function's own role needs no policy; your install grants it. It must run in the account and region of
  your install.

**Azure Functions** (`--app NAME --resource-group RG [--subscription S]`): the app is one agent, named by
`--name` (default: the app's name). `--name agent=function,agent=function2` picks the functions.

- Python: `redberry-sdk[providers]`, `redberry_sdk.init(agent="…")` in `function_app.py`, and
  `@redberry_sdk.flush_after` under the trigger decorators of each agent function (not on `async def`
  functions, whose spans are sent in the background).
- Node.js: `@redberrylabs/otel` (^0.5.0) and its peers, and a set-up module (`src/redberry.ts` or `.js`) that
  calls `init({ agent })` and registers the AI SDK's OpenTelemetry integration, imported first by each agent's
  function file.
- The app needs a managed identity that was chosen as an agent when your install was set up.

On both platforms `connect` sets `temperature` to 1 on each model call it finds (pricing needs exactly 1), and
writes what it found to `.redberry/aws-lambda.json` or `.redberry/azure-functions.json` (no secrets). Code an
earlier version of `connect` wired with the in-process hasher (`redberry_sdk.hasher`, `withRedberry`) is moved
to the form above when you run `connect` again.

**Vercel** (a Next.js app using the Vercel AI SDK, run in the app's folder): the app is one agent, named by
`--name` (default: the Vercel project's name from `.vercel/project.json`, else the package's).

- Writes `instrumentation.ts` (`src/instrumentation.ts` in a `src/app` layout): `@vercel/otel`'s
  `registerOTel` with `redberryExporter(exporterOptionsFrom({}))` from `@redberrylabs/otel` as the trace
  exporter, the agent's name as the resource attribute `redberry.agent.name`, and the AI SDK's OpenTelemetry
  integration. An `instrumentation.ts` of your own is left as it is, and the code to merge is printed.
- Adds `@redberrylabs/otel` (^0.6.0), `@vercel/otel`, `@ai-sdk/otel` and the OpenTelemetry peers to
  `package.json`, and sets `temperature` to 1 on each model call the app's routes reach.
- The exporter reads `REDBERRY_*` variables: the install prints them when it is run with `--vercel-team`
  (`REDBERRY_CLOUD`, `REDBERRY_INGEST_URL` and the cloud's federation names). Save that output to a file and
  pass `--env-file FILE`, or export the variables before running `connect`. `connect` checks the set is complete
  for the cloud and, when the app is linked (`vercel link`) and the Vercel CLI is installed, sets them on the
  project's production environment with `vercel env add`. `--no-env` prints them instead. None of them is a
  secret: the deployment's Vercel OIDC token is exchanged for a short-lived credential.
- Turn on the project's OIDC federation (Settings > Security > Secure backend access, team issuer).

An app connected by an earlier version with the in-process hasher gets the new `instrumentation.ts`; remove the
old variables it set (`REDBERRY_SIGNING_KEY`, `REDBERRY_PROJECT_TOKEN` and the others) from the project.

## `connect --extract`: the agent's manifest

`--extract` writes the agent's manifest (`.redberry/manifest.json`) with `redberry-extract` and the LLM key already
in your environment (`ANTHROPIC_API_KEY`, else `OPENAI_API_KEY`). The agent's code is sent only to that key's
provider. Without `--extract`, `connect` asks first.

A `redberry-extract` on PATH is used as it is. Otherwise `connect` installs it:

- It needs `redberry login` (the extractor is licensed to your company) and
  [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `pipx install uv`).
- The Redberry Extractor Licence is shown and accepted once per company, and again when a new version is
  published. `--yes` does not accept it; in a script or with `--json`, pass `--accept-extractor-licence`.
- It is installed with uv into its own environment in `~/.redberry/extractor/<version>/` (`$REDBERRY_HOME`
  overrides `~/.redberry`), from Redberry's package index with a short-lived download token that is passed to uv
  in its environment only. Later runs reuse that install.

## `upgrade`: take Redberry's latest release

`redberry upgrade` lists your company's installs, the release each last reported, and whether a newer one is
out. `redberry upgrade <id>` upgrades one: it fetches the cloud's install script from the Redberry portal and runs
it on this machine with `--upgrade`, through the cloud's CLI as you are signed in to it (`gcloud`, `aws` or `az`;
on AWS, signed in to the install's account). The script keeps every choice the install was made with, asks
nothing and needs no install token, then reports the new release to Redberry. It is the same command the portal
shows, which you can also paste into the cloud's own shell.

It asks before it runs (`--yes` skips the question). An install that is already up to date is left alone unless
you pass `--force`. A stopped or revoked install is not upgraded: install it again from the portal.

## License

See [LICENSE](LICENSE) and [NOTICE](NOTICE).
