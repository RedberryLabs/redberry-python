# redberry-cli

The `redberry` command. It signs a machine in to Redberry for your company, and wires an agent's code on AWS
Lambda or Azure Functions so the agent sends its spans to your Redberry install. The agent itself uses
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

The API is `https://api.redberry.sbs`; `--api` or `REDBERRY_API_URL` selects another. `--json` prints a
command's result for scripts.

## What `connect --platform` does

`connect` reads the function on the cloud (`aws lambda get-function-configuration`, `az functionapp show`),
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

`--extract` writes the agent's manifest (`.redberry/manifest.json`) with `redberry-extract` when that tool is
installed. It is not part of this release; without it, write the manifest by hand.

## License

See [LICENSE](LICENSE) and [NOTICE](NOTICE).
