"""``redberry setup-azure``: an Azure Blob container that stored runs or evidence are written to, with no key.

Nothing that writes holds a storage key: the storage account refuses shared-key access, and the writer
is a user-assigned managed identity with ``Storage Blob Data Contributor`` on its container only. This
runs (or, with ``--print``, prints) the ``az`` commands:

1. the resource group (created only if it is missing);
2. the storage account: StorageV2, minimum TLS 1.2, HTTPS only, public blob access off, shared-key
   access off (Entra ID only);
3. the container(s), through the management plane (``az storage container-rm``), which needs no data
   role and no key;
4. a container-level time-based immutability policy (``--retention``, default 7y): a stored blob can't
   be changed or deleted until it is that old. Unlocked unless ``--lock`` (locking is irreversible);
   Azure's shortest period is one day;
5. the user-assigned managed identity (``--identity``) and ``Storage Blob Data Contributor`` for it,
   scoped to the container (to each of the three with ``--evidence``);
6. for the target:

   * ``--vercel-team T [--vercel-project P]``: a federated identity credential on the identity, which
     trusts Vercel's team issuer ``https://oidc.vercel.com/<team>`` for the subject
     ``owner:<team>:project:<project>:environment:production`` and the audience
     ``https://vercel.com/<team>``. The project's
     production functions exchange their OIDC token for the identity's Entra ID token;
   * ``--function-app NAME``: the identity is assigned to the function app, which reaches it through
     its identity endpoint;
   * ``--evidence``: three containers for the bundle's VALP sink (``valp-evidence``, ``valp-payloads``,
     ``valp-index``), the immutability policy on the evidence container only, and blob soft delete and
     versioning off on the account (the sink rewrites the index and erases payloads, which must really
     go). The identity is then attached to the machine the bundle runs on (an Azure VM), or a service
     principal stands in for it elsewhere.

With ``--reader-for-me`` the signed-in user also gets ``Storage Blob Data Reader`` on the container(s),
to look at what was stored; nothing here needs it.

What it made is recorded in ``.redberry/azure.json`` (names and ids only, no secret), which ``redberry
connect --store azure-blob`` reads for the tenant and the identity's client id.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from redberry_cli._oidc import SetupError, oidc_audience, oidc_issuer, oidc_subject

RECORD = Path(".redberry") / "azure.json"
WRITER_ROLE = "Storage Blob Data Contributor"
READER_ROLE = "Storage Blob Data Reader"
DEFAULT_CONTAINER = "redberry-runs"
EVIDENCE_PREFIX = "valp"
DEFAULT_IDENTITY = "redberry-trace-writer"
DEFAULT_EVIDENCE_IDENTITY = "redberry-valp-sink"
DEFAULT_RETENTION = "7y"
#: The roles the bundle's three containers play (bundle.BUCKET_ROLES), and which of them is immutable.
EVIDENCE_ROLES = ("evidence", "payloads", "index")
IMMUTABLE_ROLES = ("evidence",)
MAX_RETENTION_DAYS = 146000
_ACCOUNT = re.compile(r"[a-z0-9]{3,24}")
_CONTAINER = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62}")
_CONTAINER_URL = re.compile(r"https://([a-z0-9]{3,24})\.blob\.core\.windows\.net/"
                            r"([a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62})")


def az_binary() -> str:
    """``$AZ``, else ``az`` on PATH, else the CLI in ``~/.azure-cli-venv`` (a pip install of azure-cli)."""
    if os.environ.get("AZ"):
        return os.environ["AZ"]
    found = shutil.which("az")
    if found:
        return found
    venv = Path.home() / ".azure-cli-venv" / "bin" / "az"
    return str(venv) if venv.exists() else "az"


def az(run, cmd: list[str], **kw):
    """Run ``cmd`` (written as ``["az", …]``) with the resolved az binary."""
    try:
        return run([az_binary(), *cmd[1:]], capture_output=True, text=True, **kw)
    except OSError:
        raise SetupError("the Azure CLI (az) is not installed: https://learn.microsoft.com/cli/azure/install-azure-cli,"
                         " then `az login`") from None


def container_url(value: str) -> str:
    """``https://<account>.blob.core.windows.net/<container>``: no SAS, no trailing slash."""
    url = (value or "").strip().rstrip("/")
    if "?" in url:
        raise SetupError("the container URL must not carry a SAS token (…?sv=…): the writer signs in with Entra ID")
    if not _CONTAINER_URL.fullmatch(url):
        raise SetupError(f"{value!r} is not a container URL: https://<account>.blob.core.windows.net/<container>")
    return url


def is_container_url(value: str | None) -> bool:
    return bool(value) and bool(_CONTAINER_URL.fullmatch(value.strip().rstrip("/")))


def retention_days(value: str) -> int:
    """``7y`` or ``30d`` as whole days (a year is 365.25 days, rounded up). Azure's immutability periods
    are whole days, from 1 to 146000."""
    m = re.fullmatch(r"([1-9][0-9]*)([dy])", value or "")
    if not m:
        if re.fullmatch(r"[1-9][0-9]*[smh]", value or ""):
            raise SetupError(f"--retention {value}: Azure's shortest immutability period is one day; use e.g. 1d "
                             "for a test container, 7y for production")
        raise SetupError("--retention: a number and a unit, d or y (e.g. 7y or 30d)")
    n, unit = int(m.group(1)), m.group(2)
    days = n if unit == "d" else math.ceil(n * 365.25)
    if days > MAX_RETENTION_DAYS:
        raise SetupError(f"--retention {value}: Azure's longest immutability period is {MAX_RETENTION_DAYS} days")
    return days


def _credential_name(team: str, project: str) -> str:
    """Federated credential names: 3-120 of letters, digits, '-' and '_', starting with a letter or digit."""
    name = re.sub(r"[^A-Za-z0-9_-]", "-", f"vercel-{team}-{project}").strip("-_")
    return name[:120]


@dataclass
class Plan:
    """What setup-azure makes; ``commands`` turns it into az commands."""

    resource_group: str
    location: str
    account: str
    containers: dict[str, str]                # role -> container name ("runs" for a Vercel / function store)
    identity: str
    retention_days: int | None
    lock: bool = False
    subscription: str | None = None
    target: str = "vercel"                    # vercel | function-app | evidence
    vercel_team: str | None = None
    vercel_project: str | None = None
    function_app: str | None = None
    app_resource_group: str | None = None
    sku: str | None = None
    reader_for_me: bool = False
    immutable: tuple[str, ...] = field(default=())

    def url(self, role: str) -> str:
        return f"https://{self.account}.blob.core.windows.net/{self.containers[role]}"

    def scope(self, subscription: str, role: str) -> str:
        return (f"/subscriptions/{subscription}/resourceGroups/{self.resource_group}/providers/"
                f"Microsoft.Storage/storageAccounts/{self.account}/blobServices/default/containers/"
                f"{self.containers[role]}")

    def _sub(self) -> list[str]:
        return ["--subscription", self.subscription] if self.subscription else []

    def identity_id(self, subscription: str) -> str:
        return (f"/subscriptions/{subscription}/resourceGroups/{self.resource_group}/providers/"
                f"Microsoft.ManagedIdentity/userAssignedIdentities/{self.identity}")

    # Each step: (why, command). ``v`` holds the values found on the way (subscription, principal_id,
    # identity_id, user_id, etag:<role>), real ones when running, shell variables when printing.
    def storage_steps(self) -> list[tuple[str, list[str]]]:
        g, a = self.resource_group, self.account
        steps = [("create the storage account (StorageV2, TLS 1.2, no public access, no shared keys)",
                  ["az", "storage", "account", "create", "--name", a, "--resource-group", g,
                   "--location", self.location, "--kind", "StorageV2", *(["--sku", self.sku] if self.sku else []),
                   "--min-tls-version", "TLS1_2", "--https-only", "true", "--allow-blob-public-access", "false",
                   "--allow-shared-key-access", "false", *self._sub()])]
        if self.target == "evidence":
            # The sink rewrites the index and erases payloads: a deleted blob must be gone, not kept as a
            # soft-deleted blob or an old version.
            steps.append(("turn blob soft delete and versioning off (erasure must really delete)",
                          ["az", "storage", "account", "blob-service-properties", "update", "--account-name", a,
                           "--resource-group", g, "--enable-delete-retention", "false", "--enable-versioning",
                           "false", *self._sub()]))
        for role, name in self.containers.items():
            steps.append((f"create the container {name}",
                          ["az", "storage", "container-rm", "create", "--storage-account", a, "--resource-group", g,
                           "--name", name, "--public-access", "off", *self._sub()]))
        for role in self.immutable:
            steps.append((f"keep every blob in {self.containers[role]} for {self.retention_days} days "
                          "(a time-based immutability policy" + (")" if self.lock else ", not locked)"),
                          ["az", "storage", "container", "immutability-policy", "create", "--account-name", a,
                           "--resource-group", g, "--container-name", self.containers[role],
                           "--period", str(self.retention_days), "--allow-protected-append-writes", "false",
                           *self._sub()]))
        return steps

    def etag_command(self, role: str) -> list[str]:
        return ["az", "storage", "container", "immutability-policy", "show", "--account-name", self.account,
                "--resource-group", self.resource_group, "--container-name", self.containers[role], *self._sub()]

    def lock_command(self, role: str, etag: str) -> list[str]:
        return ["az", "storage", "container", "immutability-policy", "lock", "--account-name", self.account,
                "--resource-group", self.resource_group, "--container-name", self.containers[role],
                "--if-match", etag, *self._sub()]

    def identity_command(self) -> list[str]:
        return ["az", "identity", "create", "--name", self.identity, "--resource-group", self.resource_group,
                "--location", self.location, *self._sub()]

    def access_steps(self, v: dict[str, str]) -> list[tuple[str, list[str]]]:
        steps = []
        for role, name in self.containers.items():
            steps.append((f"let the identity read and write blobs in {name}, and nothing else",
                          ["az", "role", "assignment", "create", "--assignee-object-id", v["principal_id"],
                           "--assignee-principal-type", "ServicePrincipal", "--role", WRITER_ROLE,
                           "--scope", self.scope(v["subscription"], role)]))
        if self.target == "vercel":
            steps.append(("trust the Vercel project's production deployments (OIDC federation)",
                          ["az", "identity", "federated-credential", "create",
                           "--name", _credential_name(self.vercel_team, self.vercel_project),
                           "--identity-name", self.identity, "--resource-group", self.resource_group,
                           "--issuer", oidc_issuer(self.vercel_team),
                           "--subject", oidc_subject(self.vercel_team, self.vercel_project),
                           "--audiences", oidc_audience(self.vercel_team), *self._sub()]))
        elif self.target == "function-app":
            steps.append((f"assign the identity to the function app {self.function_app}",
                          ["az", "functionapp", "identity", "assign", "--name", self.function_app,
                           "--resource-group", self.app_resource_group or self.resource_group,
                           "--identities", v["identity_id"], *self._sub()]))
        if self.reader_for_me:
            for role, name in self.containers.items():
                steps.append((f"let you (the signed-in user) read {name}",
                              ["az", "role", "assignment", "create", "--assignee-object-id", v["user_id"],
                               "--assignee-principal-type", "User", "--role", READER_ROLE,
                               "--scope", self.scope(v["subscription"], role)]))
        return steps

    def record(self, v: dict[str, str]) -> dict:
        rec = {"subscription_id": v["subscription"], "tenant_id": v["tenant"], "resource_group": self.resource_group,
               "location": self.location, "account": self.account, "target": self.target,
               "containers": {role: {"name": name, "url": self.url(role)} for role, name in self.containers.items()},
               "identity": {"name": self.identity, "id": v["identity_id"], "client_id": v["client_id"],
                            "principal_id": v["principal_id"]},
               "retention_days": self.retention_days, "immutable": list(self.immutable), "locked": self.lock}
        if "runs" in self.containers:
            rec["container_url"] = self.url("runs")
        if self.target == "vercel":
            rec.update({"vercel_team": self.vercel_team, "vercel_project": self.vercel_project,
                        "oidc_issuer": oidc_issuer(self.vercel_team), "oidc_audience": oidc_audience(self.vercel_team),
                        "oidc_subject": oidc_subject(self.vercel_team, self.vercel_project)})
        elif self.target == "function-app":
            rec["function_app"] = {"name": self.function_app,
                                   "resource_group": self.app_resource_group or self.resource_group}
        return rec

    def next_step(self, v: dict[str, str]) -> str:
        if self.target in ("vercel", "function-app"):
            return f"the container {self.url('runs')} is ready for its writer"
        return (f"the containers {self.url('evidence')}, {self.url('payloads')} and {self.url('index')} are ready "
                f"(identity {v['client_id']}, tenant {v['tenant']})")


# ── running ─────────────────────────────────────────────────────────────────────────────────

#: How long the role and identity steps wait for an identity that was just created. Entra ID replicates a
#: new service principal within a minute or two; until then, assignments naming it fail.
RETRY_DELAYS_S = (5, 5, 10, 10, 15, 15, 20, 20)


def _not_yet_visible(cmd: list[str], r) -> bool:
    err = (getattr(r, "stderr", "") or "").lower()
    if cmd[1:3] == ["role", "assignment"]:
        return "principalnotfound" in err or "does not exist in the directory" in err
    if cmd[1:3] in (["identity", "federated-credential"], ["functionapp", "identity"]):
        return "userassignedidentities" in err or ("identity" in err and "not found" in err)
    return False


def _already_exists(r) -> bool:
    err = (getattr(r, "stderr", "") or "").lower()
    return "already exists" in err or "alreadyexists" in err or "roleassignmentexists" in err


def _locked_already(r) -> bool:
    err = (getattr(r, "stderr", "") or "").lower()
    return "locked" in err and "immutab" in err


def _json(r) -> dict:
    try:
        return json.loads(getattr(r, "stdout", "") or "{}")
    except ValueError:
        return {}


#: Resource providers each setup needs. A new subscription has most of them unregistered, and the first
#: resource of an unregistered kind fails (MissingSubscriptionRegistration), so they are registered first.
PROVIDERS = ("Microsoft.Storage", "Microsoft.ManagedIdentity", "Microsoft.Web")


def ensure_providers(run, names: tuple[str, ...], sub: list[str], out) -> None:
    """Register any of ``names`` not yet registered on the subscription, and wait for them."""
    for name in names:
        r = az(run, ["az", "provider", "show", "--namespace", name, *sub, "--query", "registrationState", "-o", "tsv"])
        if (getattr(r, "stdout", "") or "").strip() == "Registered":
            continue
        out(f"register the resource provider {name} on the subscription (one-time) ...")
        r = az(run, ["az", "provider", "register", "--namespace", name, "--wait", *sub, "-o", "none"])
        if r.returncode != 0:
            raise SetupError(f"az provider register --namespace {name} failed: "
                             f"{(getattr(r, 'stderr', '') or '').strip()}")


def _run_step(why: str, cmd: list[str], run, out, sleep, allow_locked: bool = False):
    out(f"{why} ...")
    r = az(run, cmd)
    for delay in RETRY_DELAYS_S:
        if r.returncode == 0 or not _not_yet_visible(cmd, r):
            break
        out(f"  the identity is not visible yet; retrying in {delay}s")
        sleep(delay)
        r = az(run, cmd)
    if r.returncode != 0:
        if _already_exists(r):
            out("  already there")
            return r
        if allow_locked and _locked_already(r):
            out("  already there, and locked")
            return r
        raise SetupError(f"{' '.join(cmd[:4])} failed: {(getattr(r, 'stderr', '') or '').strip()}")
    return r


def _sh(arg: str) -> str:
    """Shell-quote ``arg``, leaving ``${VAR}`` to expand."""
    parts = re.split(r"(\$\{[A-Z_]+\})", arg)
    if len(parts) == 1:
        return shlex.quote(arg)
    return "".join(f'"{p}"' if re.fullmatch(r"\$\{[A-Z_]+\}", p) else shlex.quote(p) if p else "" for p in parts)


def _line(cmd: list[str]) -> str:
    return " ".join(_sh(c) for c in cmd)


def _read_vercel_project(repo: Path) -> str | None:
    try:
        return json.loads((repo / ".vercel" / "project.json").read_text(encoding="utf-8")).get("projectName")
    except (OSError, ValueError):
        return None


def plan_from_args(a) -> Plan:
    targets = [t for t, on in (("vercel", bool(a.vercel_team)), ("function-app", bool(a.function_app)),
                               ("evidence", bool(a.evidence))) if on]
    if len(targets) != 1:
        raise SetupError("say what writes to the storage, one of: --vercel-team <team> (a Vercel project), "
                         "--function-app <name> (an Azure function app) or --evidence (the bundle's VALP sink)")
    target = targets[0]
    if not _ACCOUNT.fullmatch(a.account or ""):
        raise SetupError("--account: the storage account's name, 3-24 lowercase letters and digits "
                         "(globally unique)")
    if target == "evidence":
        if a.container:
            raise SetupError("--evidence makes the three containers valp-evidence, valp-payloads and valp-index: "
                             "drop --container")
        containers = {role: f"{EVIDENCE_PREFIX}-{role}" for role in EVIDENCE_ROLES}
        immutable = IMMUTABLE_ROLES
    else:
        containers = {"runs": a.container or DEFAULT_CONTAINER}
        immutable = ("runs",)
    for name in containers.values():
        if not _CONTAINER.fullmatch(name):
            raise SetupError(f"container name {name!r}: 3-63 lowercase letters, digits and single hyphens")
    days = retention_days(a.retention or DEFAULT_RETENTION)
    vercel_project = None
    if target == "vercel":
        vercel_project = a.vercel_project or _read_vercel_project(Path(a.repo or ".").resolve())
        if not vercel_project:
            raise SetupError("name the Vercel project: --vercel-project <name> (no .vercel/project.json here)")
    identity = a.identity or (DEFAULT_EVIDENCE_IDENTITY if target == "evidence" else DEFAULT_IDENTITY)
    return Plan(resource_group=a.resource_group, location=a.location, account=a.account, containers=containers,
                identity=identity, retention_days=days, lock=a.lock, subscription=a.subscription, target=target,
                vercel_team=a.vercel_team, vercel_project=vercel_project, function_app=a.function_app,
                app_resource_group=a.app_resource_group, sku=a.sku, reader_for_me=a.reader_for_me,
                immutable=immutable)


def _print(plan: Plan, out) -> int:
    sub = ["--subscription", plan.subscription] if plan.subscription else []
    g = plan.resource_group
    out("# redberry setup-azure: run these (bash), signed in with `az login`.")
    out(f"SUBSCRIPTION_ID=$(az account show {_line(sub)} --query id -o tsv)".replace("  ", " "))
    out(f"TENANT_ID=$(az account show {_line(sub)} --query tenantId -o tsv)".replace("  ", " "))
    out("# the resource group (az group create keeps an existing group in the same location)")
    out(_line(["az", "group", "create", "--name", g, "--location", plan.location, *sub]))
    for why, cmd in plan.storage_steps():
        out(f"# {why}")
        out(_line(cmd))
    if plan.lock:
        for role in plan.immutable:
            out(f"# lock the policy on {plan.containers[role]} (IRREVERSIBLE: nothing can shorten it or "
                "delete the container's blobs before they expire)")
            out(f"ETAG=$({_line(plan.etag_command(role))} --query etag -o tsv)")
            out(_line(plan.lock_command(role, "${ETAG}")))
    out("# the managed identity the writer runs as")
    out(_line(plan.identity_command()))
    show = ["az", "identity", "show", "--name", plan.identity, "--resource-group", g, *sub]
    out(f"PRINCIPAL_ID=$({_line(show)} --query principalId -o tsv)")
    out(f"CLIENT_ID=$({_line(show)} --query clientId -o tsv)")
    out(f"IDENTITY_ID=$({_line(show)} --query id -o tsv)")
    v = {"subscription": "${SUBSCRIPTION_ID}", "principal_id": "${PRINCIPAL_ID}", "identity_id": "${IDENTITY_ID}",
         "user_id": "${USER_ID}", "client_id": "${CLIENT_ID}", "tenant": "${TENANT_ID}"}
    if plan.reader_for_me:
        out("USER_ID=$(az ad signed-in-user show --query id -o tsv)")
    out("# (a role assignment naming a new identity can fail for a minute or two until Entra ID has it: "
        "run it again)")
    for why, cmd in plan.access_steps(v):
        out(f"# {why}")
        out(_line(cmd))
    out(f"# then: {plan.next_step({'client_id': '$CLIENT_ID', 'tenant': '$TENANT_ID'})}")
    return 0


def cmd_setup_azure(a, out=print, run=None, sleep=None) -> int:
    run = run or subprocess.run
    sleep = sleep or time.sleep
    plan = plan_from_args(a)
    repo = Path(getattr(a, "repo", ".") or ".").resolve()
    if a.print:
        return _print(plan, out)

    sub = ["--subscription", plan.subscription] if plan.subscription else []
    r = az(run, ["az", "account", "show", *sub, "-o", "json"])
    account = _json(r)
    if r.returncode != 0 or not account.get("id"):
        raise SetupError(f"az is not signed in: run `az login` ({(getattr(r, 'stderr', '') or '').strip()})")
    v = {"subscription": account["id"], "tenant": account.get("tenantId", "")}
    out(f"Azure subscription {account.get('name') or account['id']} ({account['id']})")
    ensure_providers(run, PROVIDERS, sub, out)

    r = az(run, ["az", "group", "exists", "--name", plan.resource_group, *sub])
    if (getattr(r, "stdout", "") or "").strip().lower() != "true":
        _run_step(f"create the resource group {plan.resource_group}",
                  ["az", "group", "create", "--name", plan.resource_group, "--location", plan.location, *sub],
                  run, out, sleep)
    for why, cmd in plan.storage_steps():
        _run_step(why, cmd, run, out, sleep, allow_locked=True)
    if plan.lock:
        for role in plan.immutable:
            etag = _json(_run_step(f"read the policy on {plan.containers[role]}", [*plan.etag_command(role), "-o",
                                                                                   "json"], run, out, sleep))
            if (etag.get("state") or "").lower() == "locked":
                out("  already locked")
                continue
            _run_step(f"lock the policy on {plan.containers[role]} (irreversible)",
                      plan.lock_command(role, etag.get("etag") or "*"), run, out, sleep)
    ident = _json(_run_step(f"create the managed identity {plan.identity}", [*plan.identity_command(), "-o", "json"],
                            run, out, sleep))
    if not ident.get("principalId") or not ident.get("clientId"):
        ident = _json(az(run, ["az", "identity", "show", "--name", plan.identity, "--resource-group",
                               plan.resource_group, *sub, "-o", "json"]))
    if not ident.get("principalId"):
        raise SetupError(f"could not read the identity {plan.identity} (az identity show)")
    v.update({"principal_id": ident["principalId"], "client_id": ident["clientId"],
              "identity_id": ident.get("id") or plan.identity_id(v["subscription"])})
    if plan.reader_for_me:
        r = az(run, ["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"])
        v["user_id"] = (getattr(r, "stdout", "") or "").strip()
        if r.returncode != 0 or not v["user_id"]:
            raise SetupError("--reader-for-me: cannot read the signed-in user (az ad signed-in-user show); "
                             "a service principal login has no user to grant")
    for why, cmd in plan.access_steps(v):
        _run_step(why, cmd, run, out, sleep)

    record = plan.record(v)
    (repo / RECORD).parent.mkdir(parents=True, exist_ok=True)
    (repo / RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    urls = ", ".join(plan.url(role) for role in plan.containers)
    out(f"Ready: {urls}. The identity {plan.identity} (client id {v['client_id']}) may read and write blobs there, "
        "and nothing else; the account accepts no shared keys.")
    if plan.target == "vercel":
        out(f"Only {oidc_subject(plan.vercel_team, plan.vercel_project)} can act as it.")
    elif plan.target == "evidence":
        out("Give the identity to the machine the bundle runs on (an Azure VM):  az vm identity assign --name <vm> "
            f"--resource-group <vm's group> --identities {v['identity_id']}")
        out("Off Azure, use a service principal with Storage Blob Data Contributor on the three containers instead, "
            "and set AZURE_CLIENT_ID, AZURE_TENANT_ID and AZURE_CLIENT_SECRET in the bundle's .env.")
    for role in plan.immutable:
        if plan.lock:
            out(f"Blobs in {plan.containers[role]} are kept for {plan.retention_days} days; the policy is locked.")
        else:
            out(f"Blobs in {plan.containers[role]} are kept for {plan.retention_days} days. The policy is not locked: "
                "once the setup is proven, lock it (irreversible) with  redberry setup-azure … --lock, or  "
                + _line(plan.lock_command(role, "<etag from immutability-policy show>")))
    out(f"Recorded in {RECORD}. Next: {plan.next_step(v)}")
    return 0


# ── what connect reads ──────────────────────────────────────────────────────────────────────


def load_record(*dirs: Path) -> dict:
    """The first ``.redberry/azure.json`` in ``dirs``, or {}."""
    for d in dirs:
        try:
            return json.loads((d / RECORD).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return {}


def record_for(record: dict, url: str) -> bool:
    """Whether setup-azure's ``record`` made the container at ``url``."""
    return any((c or {}).get("url") == url for c in (record.get("containers") or {}).values())


def identity_settings(a, url: str, record: dict, run=None, need_tenant: bool = True) -> dict[str, str]:
    """The writer's tenant and client id ({tenant, client_id}): ``--azure-tenant`` / ``--azure-client-id``,
    else setup-azure's record of that container. The tenant falls back to ``az account show``; a function
    app, which reaches its identity through the platform, needs no tenant."""
    tenant, client = getattr(a, "azure_tenant", None), getattr(a, "azure_client_id", None)
    if record_for(record, url):
        tenant = tenant or record.get("tenant_id")
        client = client or (record.get("identity") or {}).get("client_id")
    if not client:
        raise SetupError(f"--store azure-blob: no {RECORD} for {url}; run `redberry setup-azure` here, or pass "
                         "--azure-client-id (the managed identity's client id)"
                         + (" and --azure-tenant" if need_tenant else ""))
    if need_tenant and not tenant and run is not None:
        r = az(run, ["az", "account", "show", "--query", "tenantId", "-o", "tsv"])
        tenant = (getattr(r, "stdout", "") or "").strip() if r.returncode == 0 else None
    if need_tenant and not tenant:
        raise SetupError("--store azure-blob: pass --azure-tenant (the Entra ID tenant id)")
    return {"tenant": tenant or "", "client_id": client}


__all__ = ["RECORD", "Plan", "SetupError", "az", "az_binary", "cmd_setup_azure", "container_url",
           "identity_settings", "is_container_url", "load_record", "plan_from_args", "retention_days"]
