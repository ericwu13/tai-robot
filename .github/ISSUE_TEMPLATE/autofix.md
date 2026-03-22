---
name: Auto-fix Request
about: Submit a bug or task for the AI agent to fix autonomously
labels: autofix
---

## Description
<!-- What's the bug or what needs to change? -->


## Acceptance Test
<!-- IMPORTANT: Write a test that FAILS on the current code and should PASS after the fix.
     This is how we verify the AI actually fixed the problem, not just made tests pass trivially. -->

```python
# tests/test_issue_NNN.py
def test_the_fix():
    # Setup
    ...
    # Act
    result = ...
    # Assert — this must FAIL on current code
    assert result == expected
```

## Context
<!-- Any extra info: which files are involved, how to reproduce, etc. -->

