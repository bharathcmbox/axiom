"""The sandbox launcher: the only code with the Docker socket (ADR-016).

Takes a validated request and starts the same locked-down container every time. A worker can ask for
a sandbox on a workspace. It can't ask for anything else, because there is no field to ask with.
"""
