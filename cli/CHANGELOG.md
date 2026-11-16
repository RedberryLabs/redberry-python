# Changelog

## Unreleased

- **`connect --platform vercel`**, for a Next.js app on the Vercel AI SDK, rewritten for the ingest exporter. It
  writes `instrumentation.ts` with `@vercel/otel`'s `registerOTel` and `redberryExporter(exporterOptionsFrom({}))`
  from `@redberrylabs/otel` (^0.6.0), adds the dependencies, sets `temperature` to 1 on the routes' model calls,
  and sets the `REDBERRY_*` variables the install printed for `--vercel-team` (`--env-file`, or the environment)
  on the project with `vercel env add`; `--no-env` prints them. No signing key, store or Redberry API call. An
  `instrumentation.ts` written for the earlier in-process hasher is replaced.

## 0.1.0 (unreleased)

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
