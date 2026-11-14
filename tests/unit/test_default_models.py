"""Phase 5 T1: the default models keep ADR-009's rule, and the two reviewers differ."""

from axiom.domain.roles import DEFAULT_MODEL, Role
from axiom.runtimes.estimate import PRICES


def test_reviewers_default_to_models_no_maker_uses() -> None:
    makers = {DEFAULT_MODEL[Role.DEVELOPER], DEFAULT_MODEL[Role.QA]}
    assert DEFAULT_MODEL[Role.CODE_REVIEWER] not in makers
    assert DEFAULT_MODEL[Role.SECURITY_REVIEWER] not in makers


def test_the_two_reviewers_default_to_different_models() -> None:
    assert DEFAULT_MODEL[Role.CODE_REVIEWER] != DEFAULT_MODEL[Role.SECURITY_REVIEWER]


def test_every_default_model_has_a_price() -> None:
    """An unpriced model would be estimated as the dearest, overstating every run."""
    assert set(DEFAULT_MODEL.values()) <= set(PRICES)
