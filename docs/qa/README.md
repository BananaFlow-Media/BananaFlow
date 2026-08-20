# Manual QA

This directory contains manual verification checklists that complement automated tests.

## Rules for QA documents

- State what surface is being verified and what automated tests already cover.
- Use disposable fixtures for destructive checks.
- Never require real credentials to be pasted into a report.
- When a checklist is executed, record the BananaFlow version/commit, OS/build and date in the verification evidence (issue/PR/release notes), not by rewriting the checklist to imply permanent verification.
- A checklist may remain reusable across releases; claims such as “nobody has tested this yet” belong in issue/release evidence, not in the evergreen checklist.

Current manual matrices include the Metadata Explorer and YouTube Doctor visual checks. Existing checklist files may remain at their historical paths for link compatibility until moved in a dedicated cleanup.
