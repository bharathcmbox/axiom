"""What may be started, and the exact `docker run` for it. No I/O beyond checking paths."""

import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^axiom-(?:sbx|verify)-[a-z0-9][a-z0-9-]{0,80}$")
ALLOWED_ENV = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",  # a run token only, checked below
        "NO_PROXY",
        "UV_PYTHON",
        "AXIOM_FAKE_SCENARIO",
        "AXIOM_FAKE_REVIEW",
        "INTERNAL_DEPLOY_TOKEN",  # the injection drill's fake canary
    }
)
_FIELDS = {"name", "workspace", "output_dir", "env", "entrypoint"}
MAX_TIMEOUT_SECONDS = 6 * 3600


class InvalidSpec(Exception):
    pass


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    workspace: str
    output_dir: str
    env: tuple[str, ...] = ()
    entrypoint: tuple[str, ...] = ()  # empty: the image's own; otherwise ("sh", "-c", command)

    @classmethod
    def from_json(cls, data: Any) -> ContainerSpec:
        """Strict: an unknown field is an attempt to ask for something this doesn't offer."""
        if not isinstance(data, dict) or set(data) - _FIELDS:
            raise InvalidSpec("unexpected fields in the request")
        try:
            return cls(
                name=str(data["name"]),
                workspace=str(data["workspace"]),
                output_dir=str(data["output_dir"]),
                env=tuple(str(item) for item in data.get("env") or ()),
                entrypoint=tuple(str(item) for item in data.get("entrypoint") or ()),
            )
        except KeyError, TypeError:
            raise InvalidSpec("the request is missing a field") from None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workspace": self.workspace,
            "output_dir": self.output_dir,
            "env": list(self.env),
            "entrypoint": list(self.entrypoint),
        }


@dataclass(frozen=True)
class Policy:
    """Fixed when the launcher starts. Nothing here comes from a request."""

    image: str = "axiom-sandbox:latest"
    network: str = "axiom_sandbox"
    proxy_url: str = "http://egress-proxy:3128"
    memory: str = "4g"
    cpus: str = "2"
    docker: str = "docker"
    workspaces_root: Path | None = None


def _checked_dir(raw: str, leaf: str, root: Path | None) -> Path:
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise InvalidSpec(f"{leaf} path must be absolute")
    if path.name != leaf:
        raise InvalidSpec(f"{leaf} path must end in /{leaf}")
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise InvalidSpec(f"{leaf} directory does not exist") from None
    if not stat.S_ISDIR(mode) or path.resolve() != path:
        raise InvalidSpec(f"{leaf} path must be a real directory, with no symlinks")
    if root is not None and not path.is_relative_to(root.resolve()):
        raise InvalidSpec(f"{leaf} path is outside the workspaces root")
    return path


def validate(spec: ContainerSpec, policy: Policy) -> None:
    if not _NAME.match(spec.name):
        raise InvalidSpec("container name is not one of ours")
    workspace = _checked_dir(spec.workspace, "repo", policy.workspaces_root)
    output = _checked_dir(spec.output_dir, "out", policy.workspaces_root)
    if workspace.parent != output.parent:
        raise InvalidSpec("repo and out must belong to the same run")
    if policy.workspaces_root is not None and workspace.parent == policy.workspaces_root.resolve():
        raise InvalidSpec("repo and out must be inside a run's own directory")
    for item in spec.env:
        name, separator, value = item.partition("=")
        if not separator or name not in ALLOWED_ENV or "\n" in value:
            raise InvalidSpec(f"environment variable not allowed: {name}")
        if name == "ANTHROPIC_API_KEY" and not value.startswith("run_"):
            raise InvalidSpec("a sandbox may only be given a run token")
    entry = spec.entrypoint
    if entry and (len(entry) != 3 or entry[:2] != ("sh", "-c")):
        raise InvalidSpec("the only allowed entrypoint is sh -c")


def docker_args(spec: ContainerSpec, policy: Policy, *, any_entrypoint: bool = False) -> list[str]:
    """Every sandbox gets exactly these restrictions. `any_entrypoint` is for local tests only;
    the launcher validates a request first, so it never reaches here."""
    assert any_entrypoint or not spec.entrypoint or spec.entrypoint[:2] == ("sh", "-c")
    owner = Path(spec.workspace).stat()
    args = [
        policy.docker, "run", "--rm", "--name", spec.name,
        "--network", policy.network,
        "--user", f"{owner.st_uid}:{owner.st_gid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "512",
        "--memory", policy.memory,
        "--cpus", policy.cpus,
        "--read-only",
        "--tmpfs", "/tmp:rw,size=1g,mode=1777",
        # exec: uv installs tools under the cache here (`uv run --with`), and Docker's tmpfs
        # default is noexec. No loss: the workspace mount is executable anyway.
        "--tmpfs", "/home/agent:rw,exec,size=2g,mode=1777",
        "-e", "HOME=/home/agent",
        "-e", f"HTTPS_PROXY={policy.proxy_url}",
        "-e", f"HTTP_PROXY={policy.proxy_url}",
        "-e", "NO_PROXY=",
        "-v", f"{spec.workspace}:/workspace/repo:rw",
        "-v", f"{spec.output_dir}:/workspace/out:rw",
    ]  # fmt: skip
    for item in spec.env:
        args += ["-e", item]
    if spec.entrypoint:
        args += ["--entrypoint", spec.entrypoint[0], policy.image, *spec.entrypoint[1:]]
    else:
        args.append(policy.image)
    return args
