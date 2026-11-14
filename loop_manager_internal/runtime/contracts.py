"""실행 잠금의 명시적인 경합 오류."""


class ProcessLockBusyError(RuntimeError):
    """다른 프로세스가 실행 또는 Git 자원을 사용하고 있다."""
