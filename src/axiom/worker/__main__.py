"""`python -m axiom.worker`: run one worker (one agent identity) until stopped.

Environment:
  AXIOM_API_URL            e.g. http://api:8000
  AXIOM_AGENT_KEY          e.g. DEV-01
  AXIOM_AGENT_TOKEN        the agent's API token
  AXIOM_GITHUB_TOKEN       fine-grained token for the target repository
  AXIOM_WORKSPACES_DIR     absolute path, identical on the host and in this container
  ANTHROPIC_API_KEY        passed to sandboxes (and only there)
  AXIOM_RUNTIME            sandbox (default) | fake
  AXIOM_FAKE_SCENARIO      with AXIOM_RUNTIME=fake or for sandbox plumbing tests
  AXIOM_PROJECT_KEY        optional: only claim tasks of this project
"""

import logging
import os
import signal
import sys
from pathlib import Path

from axiom.runtimes.base import Runtime
from axiom.runtimes.fake import FakeRuntime, Scenario
from axiom.runtimes.sandbox import SandboxRuntime
from axiom.worker.api_client import AxiomClient
from axiom.worker.harness import Worker, WorkerConfig
from axiom.worker.publish import GitHubClient, PullRequest, open_pull_request, parse_github_repo

REQUIRED = ("AXIOM_API_URL", "AXIOM_AGENT_KEY", "AXIOM_AGENT_TOKEN", "AXIOM_WORKSPACES_DIR")


def build_worker() -> Worker:
    env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if env.get("AXIOM_RUNTIME", "sandbox") == "sandbox" and not env.get("AXIOM_FAKE_SCENARIO"):
        missing += [n for n in ("AXIOM_GITHUB_TOKEN", "ANTHROPIC_API_KEY") if not env.get(n)]
    if missing:
        raise SystemExit(f"worker misconfigured, missing: {', '.join(missing)}")
    github_token = env.get("AXIOM_GITHUB_TOKEN")
    config = WorkerConfig(
        agent_key=env["AXIOM_AGENT_KEY"],
        workspaces_root=Path(env["AXIOM_WORKSPACES_DIR"]),
        github_token=github_token,
        lease_seconds=int(env.get("AXIOM_WORKER_LEASE_SECONDS", "120")),
        heartbeat_seconds=float(env.get("AXIOM_HEARTBEAT_SECONDS", "30")),
        poll_seconds=float(env.get("AXIOM_POLL_SECONDS", "10")),
        project_key=env.get("AXIOM_PROJECT_KEY") or None,
        keep_workspaces=env.get("AXIOM_KEEP_WORKSPACES") == "1",
        default_model=env.get("AXIOM_DEFAULT_MODEL") or "claude-opus-5-5",
        effort=env.get("AXIOM_EFFORT") or "high",
    )
    scenario = env.get("AXIOM_FAKE_SCENARIO")
    sandbox = SandboxRuntime(
        network=env.get("AXIOM_SANDBOX_NETWORK", "axiom_sandbox"),
        image=env.get("AXIOM_SANDBOX_IMAGE", "axiom-sandbox:latest"),
        fake_scenario=scenario if env.get("AXIOM_RUNTIME", "sandbox") == "sandbox" else None,
    )

    def runtime_factory() -> Runtime:
        if env.get("AXIOM_RUNTIME") == "fake":
            return FakeRuntime(scenario=Scenario(scenario or "success"))
        return SandboxRuntime(
            network=sandbox.network, image=sandbox.image, fake_scenario=sandbox.fake_scenario
        )

    def open_pr(repo_url: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        client = GitHubClient(token=github_token or "")
        return open_pull_request(client, repo_url, branch=branch, base=base, title=title, body=body)

    return Worker(
        config=config,
        api=AxiomClient(env["AXIOM_API_URL"], env["AXIOM_AGENT_TOKEN"]),
        runtime_factory=runtime_factory,
        test_runner=sandbox,
        open_pr=open_pr,
        # The in-process fake runtime (tests, demos) may use local file:// remotes.
        validate_repo=(lambda url: None)
        if env.get("AXIOM_RUNTIME") == "fake"
        else parse_github_repo,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = build_worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
