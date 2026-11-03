"""Phase 2 T2: API tokens: issue, authenticate, revoke; system actors cannot hold tokens."""

import pytest

from axiom.domain.roles import Actor, ActorKind
from axiom.errors import InvalidInput, NotFound
from axiom.events.log import list_events
from axiom.registry.tokens import (
    authenticate,
    hash_token,
    issue_token,
    list_tokens,
    main,
    revoke_token,
)
from axiom.state.db import Conn

CLI = Actor(ActorKind.SYSTEM, "token-cli")


def test_issue_and_authenticate(org: Conn) -> None:
    human = issue_token(org, CLI, "human:bharath", label="laptop")
    agent = issue_token(org, CLI, "agent:DEV-01")
    assert human.startswith("axm_") and len(human) > 40 and human != agent
    assert authenticate(org, human) == Actor(ActorKind.HUMAN, "bharath")
    assert authenticate(org, agent) == Actor(ActorKind.AGENT, "DEV-01")
    assert authenticate(org, "axm_wrong") is None
    assert authenticate(org, "not-even-prefixed") is None


def test_only_the_hash_is_stored(org: Conn) -> None:
    token = issue_token(org, CLI, "human:bharath")
    rows = org.execute("SELECT * FROM api_tokens").fetchall()
    assert [r["token_hash"] for r in rows] == [hash_token(token)]
    assert token not in str(rows)
    assert token not in str(list_events(org))


def test_issue_and_revoke_write_events(org: Conn) -> None:
    issue_token(org, CLI, "agent:DEV-02")
    [info] = list_tokens(org)
    revoke_token(org, CLI, info.id)
    types = [e["type"] for e in list_events(org) if e["type"].startswith("TOKEN_")]
    assert types == ["TOKEN_ISSUED", "TOKEN_REVOKED"]


def test_revoked_token_no_longer_authenticates(org: Conn) -> None:
    token = issue_token(org, CLI, "human:bharath")
    revoke_token(org, CLI, list_tokens(org)[0].id)
    assert authenticate(org, token) is None
    with pytest.raises(NotFound):
        revoke_token(org, CLI, list_tokens(org)[0].id)  # already revoked


@pytest.mark.parametrize(
    ("actor", "error"),
    [
        ("system:reaper", InvalidInput),
        ("human:bad name", InvalidInput),
        ("agent:DEV-99", NotFound),
        ("robot:x", ValueError),
    ],
)
def test_invalid_token_subjects_rejected(org: Conn, actor: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        issue_token(org, CLI, actor)
    assert list_tokens(org) == []


def test_cli_issue_list_revoke(
    org: Conn, db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AXIOM_DATABASE_URL", db_url)
    from axiom.config import get_settings

    get_settings.cache_clear()
    try:
        assert main(["issue", "agent:DEV-01", "--label", "worker-1"]) == 0
        token = capsys.readouterr().out.strip()
        assert authenticate(org, token) == Actor(ActorKind.AGENT, "DEV-01")
        main(["list"])
        assert "agent:DEV-01" in capsys.readouterr().out
        main(["revoke", str(list_tokens(org)[0].id)])
        assert authenticate(org, token) is None
    finally:
        get_settings.cache_clear()
