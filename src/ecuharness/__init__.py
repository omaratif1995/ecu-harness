"""ECU Harness: an AI agent harness for AUTOSAR ECU configuration tools."""

__version__ = "0.2.0"


class ToolError(Exception):
    """An error meant for the calling agent: what went wrong and how to fix it."""

    def __init__(self, message, hint=None, extra=None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.extra = extra

    def to_dict(self):
        out = {"error": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.extra:
            out.update(self.extra)
        return out


class InternalError(Exception):
    """The harness could not prove its own result; nothing was written."""
