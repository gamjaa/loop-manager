"""Manual intake failures distinguished from accepted dependency waits."""

class ManualWorkError(RuntimeError):
    pass


class ManualConflictError(ManualWorkError):
    pass
