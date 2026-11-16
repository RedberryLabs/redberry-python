# Changelog

## 0.1.0 (unreleased)

- First release of `redberry-cli`, the `redberry` command: `login`, `whoami`, `logout`, `connect`, `setup-aws`
  and `setup-azure`.
- `connect --platform aws-lambda` and `connect --platform azure-functions` wire a Node.js or Python function to
  send its spans to the customer's Redberry install: `redberry_sdk.init(agent=…)` and `redberry_sdk.flush_after`
  (`redberry-sdk[providers]` from PyPI) in Python, `init({ agent })` from `@redberrylabs/otel/init` (^0.5.0) in
  Node.js. No key or setting is written to the cloud. Code wired by an earlier in-process hasher setup is moved to
  this form.
- `connect` without `--platform` points to the Redberry portal's install command, which sets up agents on a cloud
  install.
- `connect --extract` runs `redberry-extract` only when it is installed; it is not part of this release.
