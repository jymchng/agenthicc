# API status

This page is retained as a migration marker: older releases and older documents
described a FastAPI application importable as `agenthicc.api.server`.

**That package does not exist in this repository.** Concretely:

| Claimed surface | Status |
|---|---|
| `src/agenthicc/api/` package | **absent** — the directory is not in the tree |
| `agenthicc.api.server` module | **absent** — not importable |
| `create_app` ASGI factory | **absent** |
| REST endpoints | **absent** |
| WebSocket transport | **absent** |
| An `api` dependency extra in `pyproject.toml` | **absent** |

The supported non-interactive interface is headless mode
([quickstart](../guides/quickstart.md#headless-mode)), which reads stdin and
writes JSON-lines. For programmatic control of a live session, use the
client-neutral `session` CLI group and its local HTTP/SSE projection described
in the [CLI reference](cli.md#two-session-surfaces) and
[Storage](storage.md#client-neutral-session-projection).

Whether to implement a server API or to remove the remaining compatibility
configuration is tracked as PRD-138 P0.2. Do not build integrations against the
historical endpoint descriptions.

!!! info "How to re-check this page"
    ```bash
    ls src/agenthicc/api            # expect: No such file or directory
    python -c "import agenthicc.api"  # expect: ModuleNotFoundError
    ```
