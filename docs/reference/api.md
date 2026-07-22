# API status

This page is retained as a migration marker because older releases and
documents described a FastAPI application at `agenthicc.api.server`.

There is no `src/agenthicc/api/` package in the current repository, no REST or
WebSocket endpoint implementation, and no declared API dependency extra. The
supported non-interactive interface is [`agenthicc --headless`](../guides/quickstart.md#headless-mode),
which reads stdin and writes JSON-lines.

The decision to implement a server API or remove the remaining compatibility
configuration is PRD-138 P0.2. Do not build integrations against the historical
endpoint descriptions.
