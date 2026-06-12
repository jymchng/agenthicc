# Communication Tools Reference

`CommunicationTools` is the sole write path for agents in Agenthicc. All nine
async methods emit kernel events and return a plain `dict`. Agents must never
mutate `AppState` directly.

```python
from agenthicc.runtime.comm_tools import CommunicationTools
from agenthicc.kernel import AppState, EventProcessor
from agenthicc.runtime.pool import AgentPool

proc = EventProcessor(AppState.create(), persist=False)
pool = AgentPool()
tools = CommunicationTools(processor=proc, pool=pool)
```

---

## agent_spawn

Spawn a new agent and register it in the pool.

```python
async def agent_spawn(
    self,
    agent_type: str,
    config: dict[str, Any] | None = None,
    parent_agent_id: str | None = None,
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `agent_type` | str | yes | Agent class identifier (e.g. `"worker"`, `"planner"`) |
| `config` | dict or None | no | Agent-specific config passed in the spawn payload |
| `parent_agent_id` | str or None | no | ID of the spawning agent (for lineage tracking) |

### Return keys

| Key | Type | Description |
|---|---|---|
| `agent_id` | str | UUID hex of the new agent |
| `agent_type` | str | Same as the `agent_type` parameter |

### Events emitted

- `AgentSpawnRequest` with `{agent_id, agent_type, parent_agent_id, config, metadata}`

### Example

```python
result = await tools.agent_spawn(
    agent_type="worker",
    config={"model": "claude-3-5-haiku", "max_tokens": 4096},
    parent_agent_id="planner-001",
)
worker_id = result["agent_id"]   # "a1b2c3d4..."
```

---

## agent_send_message

Send a message to another agent via the event log and optionally via the message bus.

```python
async def agent_send_message(
    self,
    to_agent_id: str,
    message: Any,
    from_agent_id: str | None = None,
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `to_agent_id` | str | yes | Target agent ID |
| `message` | Any | yes | Message payload; non-dict values are wrapped as `{"text": str(message)}` |
| `from_agent_id` | str or None | no | Sending agent ID |

### Return keys

| Key | Type | Description |
|---|---|---|
| `message_id` | str | Event ID (or bus message ID if bus is configured) |
| `to_agent_id` | str | Same as parameter |
| `delivered` | bool | `True` if delivered via message bus; `False` if log-only |

### Events emitted

- `AgentMessageSent` with `{from_agent_id, to_agent_id, message, delivered}`

### Example

```python
result = await tools.agent_send_message(
    to_agent_id=worker_id,
    message={"task_id": "t001", "description": "write tests"},
    from_agent_id="planner-001",
)
print(result["delivered"])   # True if bus is configured, else False
```

---

## task_create

Create a pending task and its corresponding workflow node.

```python
async def task_create(
    self,
    description: str,
    workflow_id: str,
    dependencies: list[str] | None = None,
    node_id: str | None = None,
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `description` | str | yes | Human-readable task description |
| `workflow_id` | str | yes | Target workflow ID (must already exist in state) |
| `dependencies` | list[str] or None | no | List of `node_id` strings that must be `complete` before this node runs |
| `node_id` | str or None | no | Override the auto-generated node ID |

### Return keys

| Key | Type | Description |
|---|---|---|
| `task_id` | str | UUID hex of the new task |
| `node_id` | str | UUID hex of the new workflow node |
| `workflow_id` | str | Same as parameter |
| `status` | str | Always `"pending"` |

### Events emitted

1. `WorkflowNodeAdded` with `{workflow_id, node_id, task_id, label, dependencies}`
2. `TaskCreated` with `{task_id, workflow_id, node_id, description}`

### Example

```python
node_a = await tools.task_create("Write tests", workflow_id="wf-001")
node_b = await tools.task_create(
    "Run tests",
    workflow_id="wf-001",
    dependencies=[node_a["node_id"]],   # B depends on A completing
)
```

---

## task_assign

Assign an existing task to a specific agent.

```python
async def task_assign(self, task_id: str, agent_id: str) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `task_id` | str | yes | ID of the task to assign |
| `agent_id` | str | yes | ID of the agent to assign it to |

### Return keys

| Key | Type | Description |
|---|---|---|
| `task_id` | str | Same as parameter |
| `agent_id` | str | Same as parameter |
| `assigned` | bool | Always `True` |

### Events emitted

- `TaskAssigned` with `{task_id, agent_id}`

### Example

```python
result = await tools.task_assign(task_id="t001", agent_id=worker_id)
assert result["assigned"] is True
```

---

## workflow_modify

Add or remove a node in an existing workflow DAG.

```python
async def workflow_modify(
    self,
    workflow_id: str,
    action: str,
    node_id: str,
    label: str | None = None,
    dependencies: list[str] | None = None,
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `workflow_id` | str | yes | Target workflow (must exist in state) |
| `action` | str | yes | `"add_node"` or `"remove_node"` |
| `node_id` | str | yes | Node ID to add or remove |
| `label` | str or None | `add_node` only | Human-readable node label |
| `dependencies` | list[str] or None | `add_node` only | Dependency node IDs |

### Return keys

| Key | Type | Description |
|---|---|---|
| `workflow_id` | str | Same as parameter |
| `action` | str | Same as parameter |
| `node_id` | str | Same as parameter |
| `applied` | bool | Always `True` on success |

### Events emitted

- `add_node`: `WorkflowNodeAdded` with `{workflow_id, node_id, task_id, label, dependencies}`
- `remove_node`: `WorkflowNodeRemoved` with `{workflow_id, node_id}`

### Errors

| Exception | Cause |
|---|---|
| `ValueError: unknown workflow` | `workflow_id` not in current state |
| `ValueError: cycle` | `add_node` would create a cycle in the DAG |
| `ValueError: cannot remove node: status is running` | Cannot remove a running/complete node |
| `ValueError: unsupported workflow action` | `action` is not `add_node` or `remove_node` |

### Example

```python
# Add a review step that depends on two existing nodes
result = await tools.workflow_modify(
    workflow_id="wf-001",
    action="add_node",
    node_id="node-review",
    label="Review all changes",
    dependencies=["node-test", "node-refactor"],
)

# Remove an optional node that is no longer needed
await tools.workflow_modify(
    workflow_id="wf-001",
    action="remove_node",
    node_id="node-optional",
)
```

---

## application_log

Append a structured log entry to the event log.

```python
async def application_log(
    self,
    level: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `level` | str | yes | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (case-insensitive) |
| `message` | str | yes | Log message |
| `data` | dict or None | no | Structured data attached to the log entry |

### Return keys

| Key | Type | Description |
|---|---|---|
| `log_id` | str | Event ID of the `ApplicationLog` event |
| `level` | str | Normalised (uppercase) level |
| `accepted` | bool | Always `True` |

### Events emitted

- `ApplicationLog` with `{level, message, data}`

### Errors

| Exception | Cause |
|---|---|
| `ValueError: invalid log level` | `level` not in `{DEBUG, INFO, WARNING, ERROR, CRITICAL}` |

### Example

```python
result = await tools.application_log(
    level="INFO",
    message="Refactoring complete",
    data={"files_modified": 4, "task_id": "t001"},
)
log_id = result["log_id"]
```

---

## application_ui_update

Push a UI update event for the TUI adapter to render.

```python
async def application_ui_update(
    self,
    content: Any,
    ui_type: str = "message",
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `content` | Any | yes | Content to display (string, dict, etc.) |
| `ui_type` | str | no | Type hint for the TUI renderer; default `"message"` |

### Return keys

| Key | Type | Description |
|---|---|---|
| `update_id` | str | Event ID of the `UIUpdate` event |
| `ui_type` | str | Same as parameter |
| `queued` | bool | Always `True` |

### Events emitted

- `UIUpdate` with `{ui_type, content}`

### Example

```python
await tools.application_ui_update(
    content="All 4 tasks complete. Coverage: 87%",
    ui_type="message",
)
```

---

## tool_define

Register a dynamically defined tool after a compile check.

```python
async def tool_define(
    self,
    name: str,
    description: str,
    source_code: str,
    parameters_schema: dict[str, Any],
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `name` | str | yes | Valid Python identifier for the tool |
| `description` | str | yes | Human-readable description |
| `source_code` | str | yes | Python source code; must compile without `SyntaxError` |
| `parameters_schema` | dict | yes | JSON Schema for the tool's parameters |

### Return keys

| Key | Type | Description |
|---|---|---|
| `tool_id` | str | UUID hex of the new tool registration |
| `name` | str | Same as parameter |
| `registered` | bool | Always `True` |

### Events emitted

- `ToolRegistered` with `{tool_id, name, description, parameters_schema, source_code, is_builtin: False}`

### Errors

| Exception | Cause |
|---|---|
| `ValueError: not a valid identifier` | `name` contains spaces or special characters |
| `ValueError: source code does not compile` | `SyntaxError` in `source_code` |

### Example

```python
result = await tools.tool_define(
    name="fetch_pr_comments",
    description="Fetch comments from a GitHub pull request",
    source_code="""
async def fetch_pr_comments(pr_number: int) -> list:
    import httpx
    r = httpx.get(f"https://api.github.com/repos/org/repo/pulls/{pr_number}/comments")
    return r.json()
""",
    parameters_schema={
        "type": "object",
        "properties": {"pr_number": {"type": "integer", "description": "PR number"}},
        "required": ["pr_number"],
    },
)
tool_id = result["tool_id"]
```

---

## hook_register

Register a lifecycle hook handler by dotted import path.

```python
async def hook_register(
    self,
    entity_type: str,
    stage: str,
    handler_dotpath: str,
) -> dict[str, Any]
```

### Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `entity_type` | str | yes | Tool name or `"*"` for all tools |
| `stage` | str | yes | `"pre_execute"`, `"post_execute"`, or `"on_error"` |
| `handler_dotpath` | str | yes | Python dotted import path, e.g. `"myapp.hooks.AuditHook"` |

### Return keys

| Key | Type | Description |
|---|---|---|
| `hook_id` | str | UUID hex of the new hook registration |
| `entity_type` | str | Same as parameter |
| `stage` | str | Same as parameter |
| `registered` | bool | Always `True` |

### Events emitted

- `HookRegistered` with `{hook_id, entity_type, stage, handler_dotpath}`

### Example

```python
result = await tools.hook_register(
    entity_type="file_write",
    stage="pre_execute",
    handler_dotpath="myapp.hooks.FileWriteAuditHook",
)
hook_id = result["hook_id"]
```

---

## scope_restrict

```python
async def scope_restrict(
    agent_id: str,
    allowed_tools: list[str] | None = None,
    denied_tools: list[str] | None = None,
    max_tool_call_budget: int | None = None,
) -> dict:
    # Returns: {"ok": bool, "agent_id": str}
```

Downscope a running agent. Can only restrict — a child agent can never gain
capabilities its parent doesn't have. Emits `AgentScopeUpdated` event.

| Parameter | Type | Description |
|-----------|------|-------------|
| `agent_id` | `str` | Target agent to restrict |
| `allowed_tools` | `list[str] \| None` | Restrict to only these tool name patterns (fnmatch) |
| `denied_tools` | `list[str] \| None` | Explicitly deny these patterns |
| `max_tool_call_budget` | `int \| None` | New (lower) budget |
