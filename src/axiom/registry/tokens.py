"""API tokens. A token authenticates one actor (`human:<name>` or `agent:<KEY>`).

`system:` actors cannot hold tokens: system commands (promote, time_out) only ever come from
inside the control plane. Tokens are random 256-bit values; only their SHA-256 is stored.

CLI (run where the database is reachable, e.g. `docker compose exec api ...`):
    python -m axiom.registry.tokens issue human:bharath [--label laptop]
    python -m axiom.registry.tokens issue agent:DEV-01
    python -m axiom.registry.tokens list
    python -m axiom.registry.tokens revoke <id>
"""

import argparse
import hashlib
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime

from axiom.config import get_settings
from axiom.domain.roles import Actor, ActorKind
from axiom.errors import InvalidInput, NotFound
from axiom.events.log import append_event
from axiom.registry.agents import get_agent
from axiom.state.db import Conn, connect

TOKEN_PREFIX = "axm_"
_HUMAN = re.compile(r"^[A-Za-z0-9._-]+$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class TokenInfo:
    id: int
    actor: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None


def issue_token(conn: Conn, issuer: Actor, actor: str, *, label: str | None = None) -> str:
    """Create a token for `actor` and return it. It cannot be retrieved again later."""
    parsed = Actor.parse(actor)  # raises ValueError on malformed input
    agent_id = None
    if parsed.kind == ActorKind.SYSTEM:
        raise InvalidInput("system actors cannot hold API tokens")
    if parsed.kind == ActorKind.HUMAN and not _HUMAN.match(parsed.name):
        raise InvalidInput(f"invalid human name {parsed.name!r}")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with conn.transaction():
        if parsed.kind == ActorKind.AGENT:
            agent_id = get_agent(conn, parsed.name).id  # NotFound if unknown
        row = conn.execute(
            """INSERT INTO api_tokens (token_hash, actor, agent_id, label)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (hash_token(token), str(parsed), agent_id, label),
        ).fetchone()
        assert row is not None
        append_event(
            conn,
            actor=issuer,
            type="TOKEN_ISSUED",
            agent_id=agent_id,
            payload={"token_id": row["id"], "actor": str(parsed), "label": label},
        )
    return token


def authenticate(conn: Conn, token: str) -> Actor | None:
    """The actor for a valid, unrevoked token, or None."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = conn.execute(
        "SELECT actor FROM api_tokens WHERE token_hash = %s AND revoked_at IS NULL",
        (hash_token(token),),
    ).fetchone()
    return Actor.parse(row["actor"]) if row else None


def revoke_token(conn: Conn, issuer: Actor, token_id: int) -> None:
    with conn.transaction():
        row = conn.execute(
            """UPDATE api_tokens SET revoked_at = now()
               WHERE id = %s AND revoked_at IS NULL RETURNING actor, agent_id""",
            (token_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"no active token with id {token_id}")
        append_event(
            conn,
            actor=issuer,
            type="TOKEN_REVOKED",
            agent_id=row["agent_id"],
            payload={"token_id": token_id, "actor": row["actor"]},
        )


def list_tokens(conn: Conn) -> list[TokenInfo]:
    rows = conn.execute(
        "SELECT id, actor, label, created_at, revoked_at FROM api_tokens ORDER BY id"
    )
    return [TokenInfo(**r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m axiom.registry.tokens")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_issue = sub.add_parser("issue", help="issue a token (printed once)")
    p_issue.add_argument("actor", help="human:<name> or agent:<AGENT-KEY>")
    p_issue.add_argument("--label")
    sub.add_parser("list")
    p_revoke = sub.add_parser("revoke")
    p_revoke.add_argument("id", type=int)
    args = parser.parse_args(argv)
    cli = Actor(ActorKind.SYSTEM, "token-cli")
    with connect(get_settings().database_url) as conn:
        if args.cmd == "issue":
            print(issue_token(conn, cli, args.actor, label=args.label))
        elif args.cmd == "list":
            for t in list_tokens(conn):
                state = f"revoked {t.revoked_at:%Y-%m-%d}" if t.revoked_at else "active"
                print(f"{t.id:>4}  {t.actor:<24} {state:<20} {t.label or ''}")
        else:
            revoke_token(conn, cli, args.id)
            print(f"revoked {args.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
