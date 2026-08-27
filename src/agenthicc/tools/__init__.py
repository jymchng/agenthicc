"""Agenthicc tool layer — lazy public exports.

The package is imported while a session is being assembled, before the first
agent turn necessarily needs a tool.  Keep the public compatibility surface,
but defer tool executor, hook, sandbox, and optional integration imports until
the requested symbol is actually used.
"""

__all__ = [
    "NetworkGuard",
    "ResourceLimits",
    "Tool",
    "ToolBase",
    "ToolLike",
    "ToolResult",
    "ToolResultEnvelope",
    "arg_bool",
    "arg_float",
    "arg_int",
    "arg_str",
    "WorkspaceView",
    "WorkspaceScope",
    "WorkspaceAccessMode",
    "WorkspaceAccessPolicy",
    "WorkspaceAccessRequest",
    "WorkspaceAccessResult",
    "WorkspacePathStatus",
    "ResolvedWorkspacePath",
    "current_workspace_access",
    "set_current_workspace_access",
    "reset_current_workspace_access",
    "AgenthiccToolExecutor",
    "ApprovalDecision",
    "ToolCallContext",
    "ToolErrorKind",
    "ToolExecutor",
    "ToolMetadata",
    "ToolSandbox",
    "AfterToolHookDecision",
    "BeforeToolHookDecision",
    "ErrorToolHookDecision",
    "HookRegistry",
    "HookRunner",
    "LifecycleHook",
    "ToolHook",
]

_LAZY_EXPORTS = {
    "Tool": ("agenthicc.tools.base", "Tool"),
    "ToolBase": ("agenthicc.tools.base", "ToolBase"),
    "ToolLike": ("agenthicc.tools.base", "ToolLike"),
    "ToolResult": ("agenthicc.tools.base", "ToolResult"),
    "ToolResultEnvelope": ("agenthicc.tools.base", "ToolResultEnvelope"),
    "arg_bool": ("agenthicc.tools.base", "arg_bool"),
    "arg_float": ("agenthicc.tools.base", "arg_float"),
    "arg_int": ("agenthicc.tools.base", "arg_int"),
    "arg_str": ("agenthicc.tools.base", "arg_str"),
    "ToolCallContext": ("agenthicc.tools.context", "ToolCallContext"),
    "AgenthiccToolExecutor": ("agenthicc.tools.executor", "AgenthiccToolExecutor"),
    "ApprovalDecision": ("agenthicc.tools.executor", "ApprovalDecision"),
    "ToolErrorKind": ("agenthicc.tools.executor", "ToolErrorKind"),
    "ToolExecutor": ("agenthicc.tools.executor", "ToolExecutor"),
    "ToolMetadata": ("agenthicc.tools.executor", "ToolMetadata"),
    "AfterToolHookDecision": ("agenthicc.tools.hooks", "AfterToolHookDecision"),
    "BeforeToolHookDecision": ("agenthicc.tools.hooks", "BeforeToolHookDecision"),
    "ErrorToolHookDecision": ("agenthicc.tools.hooks", "ErrorToolHookDecision"),
    "HookRegistry": ("agenthicc.tools.hooks", "HookRegistry"),
    "HookRunner": ("agenthicc.tools.hooks", "HookRunner"),
    "LifecycleHook": ("agenthicc.tools.hooks", "LifecycleHook"),
    "ToolHook": ("agenthicc.tools.hooks", "ToolHook"),
    "NetworkGuard": ("agenthicc.tools.sandbox", "NetworkGuard"),
    "ResourceLimits": ("agenthicc.tools.sandbox", "ResourceLimits"),
    "ResolvedWorkspacePath": ("agenthicc.tools.sandbox", "ResolvedWorkspacePath"),
    "ToolSandbox": ("agenthicc.tools.sandbox", "ToolSandbox"),
    "WorkspaceAccessMode": ("agenthicc.tools.sandbox", "WorkspaceAccessMode"),
    "WorkspaceAccessPolicy": ("agenthicc.tools.sandbox", "WorkspaceAccessPolicy"),
    "WorkspaceAccessRequest": ("agenthicc.tools.sandbox", "WorkspaceAccessRequest"),
    "WorkspaceAccessResult": ("agenthicc.tools.sandbox", "WorkspaceAccessResult"),
    "WorkspacePathStatus": ("agenthicc.tools.sandbox", "WorkspacePathStatus"),
    "WorkspaceScope": ("agenthicc.tools.sandbox", "WorkspaceScope"),
    "WorkspaceView": ("agenthicc.tools.sandbox", "WorkspaceView"),
    "current_workspace_access": ("agenthicc.tools.workspace_access", "current_workspace_access"),
    "set_current_workspace_access": (
        "agenthicc.tools.workspace_access",
        "set_current_workspace_access",
    ),
    "reset_current_workspace_access": (
        "agenthicc.tools.workspace_access",
        "reset_current_workspace_access",
    ),
    "AgenthiccMcpTool": ("agenthicc.tools.mcp", "AgenthiccMcpTool"),
    "McpConfigurationError": ("agenthicc.tools.mcp", "McpConfigurationError"),
    "McpServerConfig": ("agenthicc.tools.mcp", "McpServerConfig"),
    "McpToolBridge": ("agenthicc.tools.mcp", "McpToolBridge"),
    "McpToolCallError": ("agenthicc.tools.mcp", "McpToolCallError"),
    "McpToolRegistry": ("agenthicc.tools.mcp", "McpToolRegistry"),
    "McpToolSchema": ("agenthicc.tools.mcp", "McpToolSchema"),
    "McpCatalogSnapshot": ("agenthicc.tools.mcp_manager", "McpCatalogSnapshot"),
    "McpRequiredServerError": ("agenthicc.tools.mcp_manager", "McpRequiredServerError"),
    "McpServerState": ("agenthicc.tools.mcp_manager", "McpServerState"),
    "McpServerStatus": ("agenthicc.tools.mcp_manager", "McpServerStatus"),
    "McpSessionManager": ("agenthicc.tools.mcp_manager", "McpSessionManager"),
    "McpStaleCatalogError": ("agenthicc.tools.mcp_manager", "McpStaleCatalogError"),
}
__all__ += list(_LAZY_EXPORTS)


def __getattr__(name: str) -> object:
    """Load one public tool symbol only when it is requested."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
