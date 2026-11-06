"""`python -m axiom.worker`: run one worker (one agent identity) until stopped.

Environment:
  AXIOM_API_URL            e.g. http://api:8000
  AXIOM_AGENT_KEY          e.g. DEV-01
  AXIOM_AGENT_TOKEN        the agent's API token
  AXIOM_GITHUB_TOKEN       fine-grained token for the target repository
  AXIOM_WORKSPACES_DIR     absolute path, identical on the host and in this container
  AXIOM_MODEL_PROXY_URL    how this worker and its sandboxes reach the model proxy
  AXIOM_MODEL_PROXY_ADMIN_TOKEN   opens runs on the proxy. The Anthropic key isn't here (ADR-015)
  AXIOM_RUNTIME            sandbox (default) | fake
  AXIOM_FAKE_SCENARIO      with AXIOM_RUNTIME=fake or for sandbox plumbing tests
  AXIOM_PROJECT_KEY        optional: only claim tasks of this project
  AXIOM_FAKE_EDITS_FILE    with AXIOM_RUNTIME=fake: JSON with a prepared patch and report
  AXIOM_WORKER_KIND        develop (default) | review
  AXIOM_FAKE_REVIEW        reviewer plumbing tests: pass | fail | ... (no API cost)
"""

import logging
import os
import signal
import sys
from pathlib import Path

from axiom.modelproxy.client import ModelProxyClient
from axiom.runtimes.base import Runtime
from axiom.runtimes.fake import FakeRuntime, Scenario, load_scripted_developer
from axiom.runtimes.fake_review import FakeReviewer, ReviewScenario
from axiom.runtimes.review import ReviewRuntime
from axiom.runtimes.sandbox import SandboxRuntime
from axiom.worker.api_client import AxiomClient
from axiom.worker.harness import Worker, WorkerConfig
from axiom.worker.publish import (
    GitHubClient,
    PullRequest,
    comment_on_pull_request,
    open_pull_request,
    parse_github_repo,
)
from axiom.worker.review_harness import ReviewWorker, ReviewWorkerConfig

PROXY_TOKEN = "AXIOM_MODEL_PROXY_ADMIN_TOKEN"


def model_proxy() -> ModelProxyClient | None:
    token = os.environ.get(PROXY_TOKEN)
    if not token:
        return None  # fake runtimes; real ones refuse to start without it, above
    url = os.environ.get("AXIOM_MODEL_PROXY_URL") or "http://model-proxy:8080"
    return ModelProxyClient(url, token)


REQUIRED = ("AXIOM_API_URL", "AXIOM_AGENT_KEY", "AXIOM_AGENT_TOKEN", "AXIOM_WORKSPACES_DIR")


def build_worker() -> Worker:
    env = os.environ
    missing = [name for name in REQUIRED if not env.get(name)]
    if env.get("AXIOM_RUNTIME", "sandbox") == "sandbox" and not env.get("AXIOM_FAKE_SCENARIO"):
        missing += [n for n in ("AXIOM_GITHUB_TOKEN", PROXY_TOKEN) if not env.get(n)]
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
            if env.get("AXIOM_FAKE_EDITS_FILE"):
                return load_scripted_developer(Path(env["AXIOM_FAKE_EDITS_FILE"]))
            return FakeRuntime(scenario=Scenario(scenario or "success"))
        return SandboxRuntime(
            network=sandbox.network,
            image=sandbox.image,
            fake_scenario=sandbox.fake_scenario,
            drill_canary=env.get("AXIOM_DRILL_CANARY") or None,
            model_proxy=model_proxy(),
        )

    def open_pr(repo_url: str, branch: str, base: str, title: str, body: str) -> PullRequest:
        client = GitHubClient(token=github_token or "")
        return open_pull_request(client, repo_url, branch=branch, base=base, title=title, body=body)

    def comment_pr(repo_url: str, number: int, body: str) -> None:
        comment_on_pull_request(GitHubClient(token=github_token or ""), repo_url, number, body)

    return Worker(
        config=config,
        api=AxiomClient(env["AXIOM_API_URL"], env["AXIOM_AGENT_TOKEN"]),
        runtime_factory=runtime_factory,
        test_runner=sandbox,
        open_pr=open_pr,
        comment_pr=comment_pr,
        model_proxy=model_proxy(),
        # The in-process fake runtime (tests, demos) may use local file:// remotes.
        validate_repo=(lambda url: None)
        if env.get("AXIOM_RUNTIME") == "fake"
        else parse_github_repo,
    )


def build_review_worker() -> ReviewWorker:
    env = os.environ
    missing = [n for n in REQUIRED if not env.get(n)]
    if env.get("AXIOM_RUNTIME", "sandbox") == "sandbox" and not env.get("AXIOM_FAKE_REVIEW"):
        missing += [n for n in ("AXIOM_GITHUB_TOKEN", PROXY_TOKEN) if not env.get(n)]
    if missing:
        raise SystemExit(f"reviewer misconfigured, missing: {', '.join(missing)}")
    config = ReviewWorkerConfig(
        agent_key=env["AXIOM_AGENT_KEY"],
        workspaces_root=Path(env["AXIOM_WORKSPACES_DIR"]),
        github_token=env.get("AXIOM_GITHUB_TOKEN"),
        lease_seconds=int(env.get("AXIOM_REVIEW_LEASE_SECONDS", "300")),
        heartbeat_seconds=float(env.get("AXIOM_HEARTBEAT_SECONDS", "30")),
        poll_seconds=float(env.get("AXIOM_POLL_SECONDS", "10")),
        default_model=env.get("AXIOM_REVIEW_MODEL") or "claude-fable-5-1",
        effort=env.get("AXIOM_EFFORT") or "high",
        keep_workspaces=env.get("AXIOM_KEEP_WORKSPACES") == "1",
    )
    fake = env.get("AXIOM_FAKE_REVIEW")

    def runtime_factory() -> ReviewRuntime:
        if env.get("AXIOM_RUNTIME") == "fake":
            return FakeReviewer(scenario=ReviewScenario(fake or "pass"))
        return SandboxRuntime(
            network=env.get("AXIOM_SANDBOX_NETWORK", "axiom_sandbox"),
            image=env.get("AXIOM_SANDBOX_IMAGE", "axiom-sandbox:latest"),
            fake_review=fake or None,
            drill_canary=env.get("AXIOM_DRILL_CANARY") or None,
            model_proxy=model_proxy(),
        )

    return ReviewWorker(
        config=config,
        api=AxiomClient(env["AXIOM_API_URL"], env["AXIOM_AGENT_TOKEN"]),
        runtime_factory=runtime_factory,
        model_proxy=model_proxy(),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker: Worker | ReviewWorker
    if os.environ.get("AXIOM_WORKER_KIND", "develop") == "review":
        worker = build_review_worker()
    else:
        worker = build_worker()
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    signal.signal(signal.SIGINT, lambda *_: worker.stop())
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
