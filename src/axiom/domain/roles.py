"""Agent roles, runtimes and actor identities."""

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ENGINEERING_MANAGER = "engineering_manager"
    ARCHITECT = "architect"
    DEVELOPER = "developer"
    QA = "qa"
    SECURITY_REVIEWER = "security_reviewer"
    CODE_REVIEWER = "code_reviewer"


class Runtime(StrEnum):
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


# The model an agent with no model of its own runs on (ADR-009: work is reviewed on a model other
# than the one that wrote it). Workers use these too, so the registry's check counts what runs.
DEFAULT_MODEL: dict[Role, str] = {
    Role.DEVELOPER: "claude-opus-5-5",
    Role.QA: "claude-opus-5-5",
    Role.CODE_REVIEWER: "claude-fable-5-2",
    Role.SECURITY_REVIEWER: "claude-sonnet-5-5",
}


class ActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


@dataclass(frozen=True)
class Actor:
    """Who issued a command, e.g. `human:bharath`, `agent:DEV-01`, `system:reaper`."""

    kind: ActorKind
    name: str

    @classmethod
    def parse(cls, raw: str) -> Actor:
        kind, sep, name = raw.partition(":")
        if not sep or not name or kind not in ActorKind.__members__.values():
            raise ValueError(
                f"actor must look like 'human:<name>', 'agent:<KEY>' or 'system:<name>': {raw!r}"
            )
        return cls(ActorKind(kind), name)

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"
