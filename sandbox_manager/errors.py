"""Error codes follow the spec (명세서 p.35). Only the ones this layer can produce."""
from __future__ import annotations

INVALID_ARGUMENT = "INVALID_ARGUMENT"
RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
RUNTIME_START_FAILED = "RUNTIME_START_FAILED"
SESSION_TERMINATED = "SESSION_TERMINATED"


class SandboxManagerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
