"""외부 보고 전에 비밀값을 가리고 실패 진단의 길이를 제한한다."""
import re


MAX_DIAGNOSTIC = 1800



_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")



_CREDENTIAL = re.compile(
    r"(?ix)(\b(?:api[_-]?key|access[_-]?token|telegram[_-]?token|secret|password)"
    r"\b[\"']?\s*[:=]\s*)(?:\"(?:\\[\s\S]|[^\"\\])*(?:\"|$)|'"
    r"(?:\\[\s\S]|[^'\\])*(?:'|$)|[^\s,;]+)"
)



_BEARER = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")



_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|\d{8,10}:[A-Za-z0-9_-]{35})\b")



def redact_secrets(value: str) -> str:
    redacted = _CREDENTIAL.sub(r"\1<REDACTED>", value)
    redacted = _BEARER.sub(r"\1<REDACTED>", redacted)
    return _TOKEN.sub("<REDACTED>", redacted)



def bounded_diagnostic(value: str) -> str:
    """Preserve the useful tail without inserting an unbounded raw log."""
    clean = _ANSI.sub("", value).replace("\r\n", "\n").replace("\r", "\n").strip()
    return redact_secrets(clean)[-MAX_DIAGNOSTIC:]
