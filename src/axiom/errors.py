"""Domain errors. Only api/errors.py maps these to HTTP status codes."""


class AxiomError(Exception):
    """Base class for expected, user-facing domain errors."""


class NotFound(AxiomError):
    pass


class AlreadyExists(AxiomError):
    pass


class InvalidInput(AxiomError):
    pass


class Forbidden(AxiomError):
    """The actor may not perform this action."""


class DependenciesNotMet(AxiomError):
    pass


class TooLittleBudget(AxiomError):
    """A task's budget can't pay for a run and waiting won't help. Caught where it's raised."""


class AssignmentRejected(AxiomError):
    """The agent cannot take this task (disabled, wrong role, at capacity)."""


class NotExecutionOwner(AxiomError):
    """An agent tried to act on an execution it does not own."""


class LeaseExpired(AxiomError):
    """The execution's lease has expired; the reaper will time it out."""


class LeaseNotExpired(AxiomError):
    """time_out was requested for an execution whose lease is still live."""


class NoActiveExecution(AxiomError):
    """The task has no active execution (e.g. heartbeat on a task that is not executing)."""
