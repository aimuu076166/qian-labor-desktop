# Desktop Repository Agent Rules

This repository is Desktop only; do not modify the Web product from here.

Before coding, read the approved desktop architecture spec and the current implementation plan.

Use synthetic fixtures only in tests and screenshots. Never commit real employee material or complete personal identifiers.

Preserve R01-R20 semantics and source traceability. Preserve the rule that insufficient data is not equivalent to no risk, and preserve all human-review gates.

No Docker, PostgreSQL, Redis, RQ, or Caddy dependency may be added to the desktop runtime.

No real API key or `PII_HASH_PEPPER` may enter React, HTML, SQLite, fixtures, screenshots, logs, packaged binaries, or Git.

Every feature change requires tests and a Pull Request.
