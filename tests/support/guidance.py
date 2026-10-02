

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
