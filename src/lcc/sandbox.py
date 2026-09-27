"""What code from the target repository may see when the harness runs it (tests, shell commands, pip/npm installs).

Two layers that work everywhere:
- `child_env()` strips credentials from the environment, so a malicious test, setup.py or npm script cannot read
  AI_API_KEY, GITHUB_TOKEN, cloud keys or the SSH agent.
- A `git` shim first on PATH refuses state-changing git commands inside the task repository, whoever calls them
  (the model's shell command, or Python/Node code it runs). Read-only git and git on other repositories pass through.

Neither is a sandbox against a hostile repository: code still runs as your user and can read files in your home
directory. For untrusted repositories use `[run] sandbox = "docker"` (see `docker_prefix`), which runs tests in a
container with no network and no credentials.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION)", re.I)
SECRET_PREFIXES = ("AWS_", "AZURE_", "GOOGLE_", "GCP_", "GH_", "GITHUB_", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_",
                   "DASHSCOPE_", "HF_", "HUGGING", "LCC_API")
SECRET_EXACT = {"SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "NETRC", "DOCKER_CONFIG", "KUBECONFIG"}

GIT_READ_ONLY = ("status diff log show blame ls-files ls-tree rev-parse rev-list grep cat-file describe shortlog "
                 "help version var check-ignore name-rev for-each-ref merge-base count-objects init clone")

SHIM = r"""#!/bin/sh
# LCC harness git guard: state-changing git commands are refused inside the task repository.
REAL="__REAL_GIT__"
dir=""; sub=""; skip=""
for a in "$@"; do
  if [ -n "$skip" ]; then [ "$skip" = "C" ] && dir="$a"; skip=""; continue; fi
  case "$a" in
    -C) skip=C ;;
    -c|--git-dir|--work-tree|--namespace|--exec-path) skip=x ;;
    -*) ;;
    *) sub="$a"; break ;;
  esac
done
case " __READ_ONLY__ " in *" $sub "*) exec "$REAL" "$@" ;; esac
[ -z "$sub" ] && exec "$REAL" "$@"
if [ "$sub" = "config" ]; then
  case " $* " in *" --get"*|*" --list "*|*" -l "*) exec "$REAL" "$@" ;; esac
fi
if [ -n "$LCC_GUARD_ROOT" ]; then
  top=$("$REAL" -C "${dir:-.}" rev-parse --show-toplevel 2>/dev/null)
  if [ -n "$top" ] && [ "$top" = "$LCC_GUARD_ROOT" ]; then
    echo "git $sub: blocked by the LCC harness (it manages git in the task repository; use git diff/log/show to inspect)" >&2
    exit 1
  fi
fi
exec "$REAL" "$@"
"""


def is_secret(name: str) -> bool:
    upper = name.upper()
    return name in SECRET_EXACT or upper.startswith(SECRET_PREFIXES) or bool(SECRET_NAME.search(name))


def shim_dir() -> Path | None:
    """Directory holding the git guard (created once per user); None when git is not installed."""
    real = shutil.which("git", path=os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                                                    if p != str(_shim_path())))
    if not real:
        return None
    d = _shim_path()
    script = SHIM.replace("__REAL_GIT__", real).replace("__READ_ONLY__", GIT_READ_ONLY)
    target = d / "git"
    if not target.exists() or target.read_text(encoding="utf-8") != script:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".git.{os.getpid()}"
        tmp.write_text(script, encoding="utf-8")
        tmp.chmod(0o755)
        tmp.replace(target)
    return d


def _shim_path() -> Path:
    return Path(tempfile.gettempdir()) / f"lcc-git-guard-{os.getuid() if hasattr(os, 'getuid') else 'u'}"


def child_env(workspace: Path | None = None, extra_path: list[str] | None = None) -> dict[str, str]:
    """Environment for code from the target repository: no credentials; git guarded inside `workspace`."""
    env = {k: v for k, v in os.environ.items() if not is_secret(k)}
    env["GIT_TERMINAL_PROMPT"] = "0"  # never prompt for (or use cached) credentials
    front = list(extra_path or [])
    if workspace is not None:
        guard = shim_dir()
        if guard:
            front.insert(0, str(guard))
            env["LCC_GUARD_ROOT"] = str(Path(workspace).resolve())
    if front:
        env["PATH"] = os.pathsep.join(front + [env.get("PATH", "")])
    return env


DOCKER_IMAGE = "python:3.12-slim"
VENV_IN_CONTAINER = "/lcc-venv"


def docker_enabled() -> bool:
    return os.environ.get("LCC_SANDBOX") == "docker"


def docker_prefix(workspace: Path, *, network: bool = False) -> list[str]:
    """`docker run` prefix: only the workspace (and the task's venv volume) is visible, no credentials are passed,
    and there is no network unless `network` (used only to install dependencies)."""
    ws = str(Path(workspace).resolve())
    image = os.environ.get("LCC_SANDBOX_IMAGE") or DOCKER_IMAGE
    volume = os.environ.get("LCC_SANDBOX_VOLUME") or "lcc-venv-" + re.sub(r"[^\w.-]", "_", Path(ws).name)
    cmd = ["docker", "run", "--rm", "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
           "-v", f"{ws}:{ws}", "-v", f"{volume}:{VENV_IN_CONTAINER}", "-w", ws,
           "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "CI=1", "-e", f"PYTHONPATH={ws}",
           "-e", f"PATH={VENV_IN_CONTAINER}/bin:/usr/local/bin:/usr/bin:/bin"]
    if not network:
        cmd += ["--network", "none"]
    return cmd + [image]


def docker_prepare(repo: Path, extras: list[str], console=None) -> str:
    """Create the task's Linux venv inside a volume (network allowed only for this install step). Returns the
    interpreter path as seen inside the container."""
    import subprocess

    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        raise RuntimeError("sandbox = docker, but Docker is not available (start Docker or set sandbox = none)")
    target = f".[{','.join(extras)}]" if extras else "."
    reqs = " ".join(f"-r {r}" for r in ("requirements.txt", "requirements-dev.txt", "requirements-test.txt")
                    if (Path(repo) / r).is_file())
    script = (f"python -m venv {VENV_IN_CONTAINER} && P={VENV_IN_CONTAINER}/bin/pip && "
              f"($P install -q -e '{target}' || $P install -q -e . || true) && "
              f"({'$P install -q ' + reqs if reqs else 'true'} || true) && $P install -q pytest")
    proc = subprocess.run(docker_prefix(repo, network=True) + ["sh", "-c", script], text=True, capture_output=True,
                          timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"sandbox setup failed: {proc.stderr.strip()[-400:]}")
    return f"{VENV_IN_CONTAINER}/bin/python"
