# Changelog

## Unreleased (0.3.0)

- New `redberry upgrade`. Without an id it lists your company's Redberry installs: where each is, the release it
  last reported, and whether a newer one is out ("update available"), with the command that upgrades it. With an
  id (`redberry upgrade <id>`) it upgrades that install: it fetches the cloud's install script from the Redberry
  portal and runs it here with `--upgrade`, through `gcloud`, `aws` or `az` as signed in on this machine. The
  script keeps every choice the install was made with and needs no install token. It asks first (`--yes` does
  not); an install that is up to date is left alone unless `--force`; a stopped or revoked install is not
  upgraded. On AWS it checks that the `aws` CLI is signed in to the install's account first. `--json` prints the
  list.
- `connect --platform vercel` says when model calls are in more than one App Router route: the app is recorded
  and priced as one agent, and agents priced separately need separate Vercel projects. `--json` carries the
  same text as `routes_note` (null for a single route).

## 0.2.0 (2026-11-17)

- `connect --extract` installs the Redberry extractor when `redberry-extract` is not on PATH. It needs a signed-in
  session (`redberry login`) and [uv](https://docs.astral.sh/uv/). The company's Redberry Extractor Licence is
  shown and accepted once per company (and again for a new licence version); `--yes` does not accept it, and
  without a terminal (or with `--json`) the new `--accept-extractor-licence` flag does. The CLI then fetches a
  short-lived download token and installs `redberry-extractor[llm]` with uv into its own environment in
  `~/.redberry/extractor/<version>/` (`$REDBERRY_HOME` overrides `~/.redberry`); the token reaches uv only in its
  environment, never on the command line, in a file or in an error message. Later runs reuse that install
  without a new token. A `redberry-extract` already on PATH is still used as it is.

## 0.1.0 (2026-11-17)

- First release of `redberry-cli`, the `redberry` command: `login`, `whoami`, `logout` and
  `connect`.
- `connect --platform aws-lambda` and `connect --platform azure-functions` wire a Node.js or Python function to
  send its spans to the customer's Redberry install: `redberry_sdk.init(agent=…)` and `redberry_sdk.flush_after`
  (`redberry-sdk[providers]` from PyPI) in Python, `init({ agent })` from `@redberrylabs/otel/init` (^0.5.0) in
  Node.js. No key or setting is written to the cloud. Code wired by an earlier in-process hasher setup is moved to
  this form.
- `connect` without `--platform` points to the Redberry portal's install command, which sets up agents on a cloud
  install.
- `connect --extract` runs `redberry-extract` only when it is installed; it is not part of this release.
- **`connect --platform vercel`**, for a Next.js app on the Vercel AI SDK, rewritten for the ingest exporter. It
  writes `instrumentation.ts` with `@vercel/otel`'s `registerOTel` and `redberryExporter(exporterOptionsFrom({}))`
  from `@redberrylabs/otel` (^0.6.0), adds the dependencies, sets `temperature` to 1 on the routes' model calls,
  and sets the `REDBERRY_*` variables the install printed for `--vercel-team` (`--env-file`, or the environment)
  on the project with `vercel env add`; `--no-env` prints them. No signing key, store or Redberry API call. An
  `instrumentation.ts` written for the earlier in-process hasher is replaced.
