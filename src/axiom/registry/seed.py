"""`python -m axiom.registry.seed [config.toml]`: register the agents listed in a TOML file."""

import sys
from pathlib import Path

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.registry.agents import seed_agents
from axiom.state.db import connect

DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "agents.toml"


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    with connect(get_settings().database_url) as conn:
        created = seed_agents(conn, Actor(ActorKind.SYSTEM, "seed"), path)
    print("registered: " + ", ".join(a.key for a in created) if created else "nothing new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
