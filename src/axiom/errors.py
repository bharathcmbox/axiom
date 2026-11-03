"""Domain errors. Only api/errors.py maps these to HTTP status codes."""


class AxiomError(Exception):
    """Base class for expected, user-facing domain errors."""


class NotFound(AxiomError):
    pass


class AlreadyExists(AxiomError):
    pass


class InvalidInput(AxiomError):
    pass
