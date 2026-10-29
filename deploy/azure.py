"""Minimal Azure deployment for the hackathon demo (docs/11_deployment_azure.md), the same shape as
Fastapi-deployment: one image in ACR, one Container App in a Container Apps environment.

    python deploy/azure.py up        build the image in ACR (az acr build) and create/update the Container App
    python deploy/azure.py pause     stop serving: ingress off, scaled to zero (nothing runs, nothing is billed)
    python deploy/azure.py resume    serve again (same URL)
    python deploy/azure.py status    URL, state, replicas, image
    python deploy/azure.py logs      stream the container's console

Settings come from .env and go to the Container App as SECRETS (secretref env vars), never as plain values or in
the image. AZURE_DATABASE_URL (the hosted Postgres) becomes the app's DATABASE_URL. Values are never printed.
Scale: 0..1 replicas, so an idle app costs nothing; the first request after idle takes a few seconds to wake it.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RG = "rg-datalake-test-uksouth"
LOCATION = "uksouth"
ACR = "gaugelensacr8axdpk"
ENV = "gaugelens-env"
APP = "gaugelens"

# .env keys that only make sense on this PC (the hosted app gets its own values below)
LOCAL_ONLY = {"DATABASE_URL", "AZURE_DATABASE_URL", "EVIDENCE_DIR", "POSTGRES_PASSWORD", "DB_HOST_PORT", "CACHE_DIR"}
HOSTED = {"WORKER_IN_PROCESS": "true", "EVIDENCE_STORE": "blob"}


def _az_command() -> list[str]:
    """The Azure CLI without a shell. On Windows `az` is a .cmd file, and cmd.exe would mangle arguments with
    & or ^ in them (common in connection strings), so call the CLI's own Python directly."""
    if sys.platform == "win32":
        cli = Path(shutil.which("az") or "").resolve().parents[1] / "python.exe"
        if cli.is_file():
            return [str(cli), "-IBm", "azure.cli"]
        raise SystemExit("Azure CLI's python.exe not found next to az.cmd")
    return ["az"]


def az(*args: str, capture: bool = True, check: bool = True) -> str:
    cmd = [*_az_command(), *args, "--only-show-errors"]
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if check and result.returncode != 0:
        # never echo the command line: it may carry secret values
        raise SystemExit(f"az {args[0]} {args[1] if len(args) > 1 else ''} failed:\n{(result.stderr or '')[-1500:]}")
    return (result.stdout or "").strip()


def read_env() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            values[key.strip()] = value
    return values


def secret_name(key: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", key.lower()).strip("-")[:20].strip("-") or "s"


def settings() -> tuple[list[str], list[str]]:
    """(secrets 'name=value', env vars 'KEY=secretref:name' or 'KEY=value')."""
    env = read_env()
    if not env.get("AZURE_DATABASE_URL"):
        raise SystemExit("Add AZURE_DATABASE_URL (the hosted Postgres connection string) to .env first.")
    secrets, env_vars, used = [], [], set()
    for key, value in [*((k, v) for k, v in env.items() if k not in LOCAL_ONLY),
                       ("DATABASE_URL", env["AZURE_DATABASE_URL"])]:
        if key in HOSTED:
            continue
        name = secret_name(key)
        while name in used:                              # 20-character names can collide
            name = name[:18] + f"-{len(used) % 10}"
        used.add(name)
        secrets.append(f"{name}={value}")
        env_vars.append(f"{key}=secretref:{name}")
    env_vars += [f"{k}={v}" for k, v in HOSTED.items()]
    return secrets, env_vars


def up(args) -> None:
    tag = "v" + datetime.now().strftime("%Y%m%d%H%M")
    image = f"{ACR}.azurecr.io/{APP}:{tag}"
    if not args.skip_build:
        print(f"building {image} in ACR (uploads the source, not the image)...")
        az("acr", "build", "--registry", ACR, "--image", f"{APP}:{tag}", "--image", f"{APP}:latest",
           "--no-logs", str(ROOT), capture=True)
    else:
        image = f"{ACR}.azurecr.io/{APP}:latest"
    secrets, env_vars = settings()
    exists = az("containerapp", "show", "-n", APP, "-g", RG, "--query", "name", "-o", "tsv", check=False)
    if not exists:
        print("creating the Container App...")
        acr_password = az("acr", "credential", "show", "-n", ACR, "--query", "passwords[0].value", "-o", "tsv")
        az("containerapp", "create", "-n", APP, "-g", RG, "--environment", ENV, "--image", image,
           "--registry-server", f"{ACR}.azurecr.io", "--registry-username", ACR, "--registry-password", acr_password,
           "--target-port", "8000", "--ingress", "external", "--min-replicas", "0", "--max-replicas", "1",
           "--cpu", "1.0", "--memory", "2Gi", "--secrets", *secrets, "--env-vars", *env_vars, "-o", "none")
    else:
        print("updating secrets and image...")
        az("containerapp", "secret", "set", "-n", APP, "-g", RG, "--secrets", *secrets, "-o", "none")
        stale_env = set(json.loads(az("containerapp", "show", "-n", APP, "-g", RG, "--query",
                                      "properties.template.containers[0].env[].name", "-o", "json")) or [])             - {e.split("=", 1)[0] for e in env_vars}
        extra = ["--remove-env-vars", *sorted(stale_env)] if stale_env else []
        az("containerapp", "update", "-n", APP, "-g", RG, "--image", image, "--set-env-vars", *env_vars, *extra,
           "--revision-suffix", tag.replace("v", "r"), "-o", "none")
        stale_secrets = set(json.loads(az("containerapp", "secret", "list", "-n", APP, "-g", RG, "--query", "[].name",
                                          "-o", "json")) or []) - {x.split("=", 1)[0] for x in secrets}
        stale_secrets.discard(f"{ACR}azurecrio-{ACR}")                    # the registry password
        if stale_secrets:                               # settings no longer in .env leave the app too
            az("containerapp", "secret", "remove", "-n", APP, "-g", RG, "--secret-names", *sorted(stale_secrets), "-o", "none")
        if stale_env or stale_secrets:
            print(f"removed {len(stale_env)} old env vars and {len(stale_secrets)} old secrets")
    status(args)


def pause(args) -> None:
    az("containerapp", "ingress", "disable", "-n", APP, "-g", RG, "-o", "none")
    az("containerapp", "update", "-n", APP, "-g", RG, "--min-replicas", "0", "-o", "none")
    print("paused: ingress off, no replicas running. `python deploy/azure.py resume` brings it back.")


def resume(args) -> None:
    az("containerapp", "ingress", "enable", "-n", APP, "-g", RG, "--type", "external", "--target-port", "8000",
       "--transport", "auto", "-o", "none")
    status(args)


def status(args) -> None:
    out = az("containerapp", "show", "-n", APP, "-g", RG, "--query",
             "{fqdn:properties.configuration.ingress.fqdn, state:properties.runningStatus, "
             "image:properties.template.containers[0].image, min:properties.template.scale.minReplicas, "
             "max:properties.template.scale.maxReplicas}", "-o", "json")
    print(out)


def logs(args) -> None:
    subprocess.run([*_az_command(), "containerapp", "logs", "show", "-n", APP, "-g", RG, "--follow", "--tail", "100"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    u = sub.add_parser("up")
    u.add_argument("--skip-build", action="store_true", help="deploy the latest image already in ACR")
    for name in ("pause", "resume", "status", "logs"):
        sub.add_parser(name)
    args = parser.parse_args()
    {"up": up, "pause": pause, "resume": resume, "status": status, "logs": logs}[args.command](args)


if __name__ == "__main__":
    main()
