"""Every configuration setting is read by the runtime (PRD-250 S-03).

A setting that is parsed and documented but never read misleads users into
believing it has an effect — ``[security] sandbox_mode = true`` once did
exactly that. This test walks every dataclass field reachable from
:class:`AgenthiccConfig` and requires a reader in ``src/agenthicc``: an
attribute load of the field name, or the name passed as a ``getattr``-style
string argument. Parsing a TOML key, declaring the field or listing it in the
config template does not count.
"""

from __future__ import annotations

import ast
import dataclasses
import sys
import typing
from pathlib import Path

import pytest

from agenthicc.config import AgenthiccConfig

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "agenthicc"

#: Fields read indirectly, with the reason a direct reader cannot be found.
#: Keep this empty unless the reason is real; never use it for unused settings.
INDIRECT_READERS: dict[str, str] = {}


def _config_fields() -> list[str]:
    """``Section.field`` for every dataclass field reachable from the config."""
    found: list[str] = []
    seen: set[type] = set()

    def annotation(cls: type, field: dataclasses.Field[object]) -> object:
        """Resolve one field annotation; names only imported for typing stay strings."""
        if not isinstance(field.type, str):
            return field.type
        module = sys.modules[cls.__module__]
        try:
            return eval(field.type, vars(module))
        except NameError:
            return field.type

    def walk(cls: type) -> None:
        if cls in seen:
            return
        seen.add(cls)
        for field in dataclasses.fields(cls):
            if field.name.startswith("_"):
                continue
            found.append(f"{cls.__name__}.{field.name}")
            resolved = annotation(cls, field)
            for candidate in typing.get_args(resolved) or (resolved,):
                if isinstance(candidate, type) and dataclasses.is_dataclass(candidate):
                    walk(candidate)

    walk(AgenthiccConfig)
    return found


def _read_names() -> set[str]:
    """Names loaded as attributes, or passed as a getattr-style string argument."""
    names: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "config_template.py":
            continue  # renders every field; rendering is not reading
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                names.add(node.attr)
            elif (
                isinstance(node, ast.Call)
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                names.add(node.args[1].value)
    return names


def test_every_setting_has_a_reader() -> None:
    readers = _read_names()
    unread = [
        qualified
        for qualified in _config_fields()
        if qualified.split(".", 1)[1] not in readers and qualified not in INDIRECT_READERS
    ]
    assert not unread, f"settings nothing reads (wire them or delete them): {unread}"


def test_the_inert_settings_found_by_prd_250_are_gone() -> None:
    fields = set(_config_fields())
    removed = {
        "SecuritySettings.sandbox_mode",
        "SecuritySettings.max_tool_cpu_seconds",
        "SecuritySettings.max_tool_memory_mb",
        "PluginSettings.auto_trust",
        "PluginSettings.auto_install",
        "PluginSettings.install_target",
        "PluginSettings.allowed_modules",
        "PluginSettings.timeout_seconds",
        "PluginSettings.trust_file",
        "PluginSettings.audit_file",
        "PluginSettings.disabled",
        "BehaviourSettings.verbose",
        "BehaviourSettings.confirm_exits",
        "ExecutionSettings.max_concurrent_intents",
        "ExecutionSettings.agent_pool_size",
    }
    assert not (fields & removed)
    assert not any(name.split(".")[0] in {"MemorySettings", "ApiSettings"} for name in fields)
    assert not any(name.startswith(("StorageSettings.", "StorageS3Settings.")) for name in fields)
