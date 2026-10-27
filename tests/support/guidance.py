import subprocess
from pathlib import Path

from tests.support.git import git_test_environment


PATCH = """diff --git a/AGENTS.md b/AGENTS.md
--- a/AGENTS.md
+++ b/AGENTS.md
@@ -1 +1,2 @@
 # Agents
+Approved rule
"""


def guidance_plan(*, decision: str = "pending", revision: str = "", patch: str = "") -> str:
    return f"""---
id: G-020
title: Guidance
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: critical
priority: high
---

## Purpose

Test.

## Decision Log

### Agent Guidance Proposal: worker-rule
Target: `AGENTS.md`
Scope: `project`
Reason: The current plan needs this rule.
Impact: The Worker cannot finish safely without it.
Patch:
```diff
{PATCH}```

### Agent Guidance User Decision: worker-rule
Decision: `{decision}`
Revision: `{revision}`
Patch:
```diff
{patch}```
"""


def git_result(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=git_test_environment(),
    )
