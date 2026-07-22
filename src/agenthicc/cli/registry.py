"""Decorator-based CLI command registry (PRD-79).

Usage
-----
    from agenthicc.cli.registry import command, group
    from agenthicc.cli.context import CLIContext

    @group("sessions", help="Manage saved sessions")
    def _(): ...

    @command("sessions", "list")
    def sessions_list(ctx: CLIContext) -> None:
        '''List sessions for the current directory.'''
        ...

    @command("sessions", "show")
    async def sessions_show(ctx: CLIContext, session_id: str) -> None:
        '''Show detail for one session.'''
        ...

CLIContext is injected by annotation type, not by parameter name — any name works.
Positional args, --flags, and --options are inferred from the function signature.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import inspect
import pkgutil
import sys
import typing
import warnings
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# ── entry dataclass ────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    path: tuple[str, ...]
    help: str
    handler: Callable
    is_async: bool
    source: str = "builtin"  # "builtin" | "user" | "project"


# ── flat registries ────────────────────────────────────────────────────────────

_REGISTRY: dict[tuple[str, ...], _Entry] = {}
_GROUPS: dict[tuple[str, ...], str] = {}

# ContextVar lets @command() read the source tag at decoration time even
# when commands are loaded dynamically from user/project directories.
_LOADING_SOURCE: ContextVar[str] = ContextVar("_LOADING_SOURCE", default="builtin")


# ── public decorators ─────────────────────────────────────────────────────────


def command(*path: str, help: str = "") -> Callable[[Callable], Callable]:
    """Register a leaf command handler at *path*.

    Signature inference rules (applied at argparse-build time):
      param: str            (no default)  → positional argument
      flag:  bool = False                 → --flag  (store_true)
      opt:   str  = "value"               → --opt VALUE
      ann:   CLIContext                   → injected at call time; never argparse arg
    """

    def decorator(fn: Callable) -> Callable:
        doc = help or (inspect.getdoc(fn) or "").splitlines()[0]
        _REGISTRY[path] = _Entry(
            path=path,
            help=doc,
            handler=fn,
            is_async=asyncio.iscoroutinefunction(fn),
            source=_LOADING_SOURCE.get(),
        )
        return fn

    return decorator


def group(*path: str, help: str = "") -> Callable:
    """Declare a command group (branch node with no handler of its own)."""
    _GROUPS[path] = help

    def decorator(fn: Callable | None = None) -> Callable | None:
        return fn

    return decorator


# ── argparse wiring ────────────────────────────────────────────────────────────


def _add_params(parser: argparse.ArgumentParser, fn: Callable) -> None:
    """Add argparse arguments inferred from the function signature."""
    from agenthicc.cli.context import CLIContext  # noqa: PLC0415

    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    for name, param in sig.parameters.items():
        ann = hints.get(name, inspect.Parameter.empty)
        default = param.default
        empty = inspect.Parameter.empty
        if ann is CLIContext:
            continue  # injected — skip
        if ann is bool:
            parser.add_argument(
                f"--{name.replace('_', '-')}",
                action="store_true",
                default=default if default is not empty else False,
            )
        elif default is empty:
            parser.add_argument(name, metavar=name.upper())  # positional
        else:
            parser.add_argument(
                f"--{name.replace('_', '-')}",
                default=default,
                type=ann if ann in (int, float) else str,
            )


def _as_tree() -> dict:
    """Build a nested dict from the flat _REGISTRY and _GROUPS."""
    tree: dict = {}

    def _ensure(node: dict, parts: tuple[str, ...]) -> dict:
        cur = node
        for part in parts:
            cur = cur.setdefault(
                part, {"help": "", "entry": None, "children": {}, "source": "builtin"}
            )
            cur = cur["children"]
        return cur

    for path, help_text in _GROUPS.items():
        node = tree
        for i, part in enumerate(path):
            slot = node.setdefault(
                part, {"help": "", "entry": None, "children": {}, "source": "builtin"}
            )
            if i == len(path) - 1:
                slot["help"] = help_text
            node = slot["children"]

    for path, entry in _REGISTRY.items():
        node = tree
        for part in path[:-1]:
            slot = node.setdefault(
                part, {"help": "", "entry": None, "children": {}, "source": entry.source}
            )
            node = slot["children"]
        slot = node.setdefault(
            path[-1], {"help": entry.help, "entry": None, "children": {}, "source": entry.source}
        )
        slot["entry"] = entry
        slot["source"] = entry.source
        if not slot["help"]:
            slot["help"] = entry.help

    return tree


def _wire(parser: argparse.ArgumentParser, tree: dict) -> None:
    """Recursively wire the tree into argparse subparsers (unlimited depth)."""
    if not tree:
        return
    subs = parser.add_subparsers(metavar="<subcommand>")
    for name, node in tree.items():
        source_badge = f"  [{node['source']}]" if node["source"] != "builtin" else ""
        p = subs.add_parser(name, help=f"{node['help']}{source_badge}")
        if (entry := node["entry"]) is not None:
            _add_params(p, entry.handler)
            p.set_defaults(_entry=entry)
        _wire(p, node["children"])


def _call(entry: _Entry, ctx: object, ns: argparse.Namespace) -> None:
    """Invoke an entry's handler, injecting CLIContext by annotation type."""
    from agenthicc.cli.context import CLIContext  # noqa: PLC0415

    hints = typing.get_type_hints(entry.handler)
    kwargs: dict[str, object] = {}
    for name, _ in inspect.signature(entry.handler).parameters.items():
        ann = hints.get(name, inspect.Parameter.empty)
        if ann is CLIContext:
            kwargs[name] = ctx
        else:
            attr = name.replace("-", "_")
            if hasattr(ns, attr):
                kwargs[name] = getattr(ns, attr)
    if entry.is_async:
        asyncio.run(entry.handler(**kwargs))
    else:
        entry.handler(**kwargs)


# ── discovery ──────────────────────────────────────────────────────────────────


def _discover_package(package_name: str) -> None:
    """Import every module in *package_name* to trigger @command registrations."""
    try:
        pkg = importlib.import_module(package_name)
    except ImportError:
        return
    for _, name, _ in pkgutil.iter_modules(getattr(pkg, "__path__", [])):
        importlib.import_module(f"{package_name}.{name}")


def _discover_directory(directory: Path, source: str) -> None:
    """Dynamically load command plugins from *directory*, tagged with *source*."""
    if not directory.is_dir():
        return

    token = _LOADING_SOURCE.set(source)
    try:
        for py in sorted(directory.glob("*.py")):
            if py.name.startswith("_"):
                continue
            mod_name = f"agenthicc._cli_plugin.{source}.{py.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                warnings.warn(
                    f"[agenthicc] Failed to load CLI plugin {py}: {exc}",
                    stacklevel=2,
                )

        # Optional TOML shorthand: cli.toml sits beside the cli/ directory
        toml_file = directory.parent / "cli.toml"
        if toml_file.exists():
            _load_toml_commands(toml_file, source=source)
    finally:
        _LOADING_SOURCE.reset(token)


def _load_toml_commands(toml_file: Path, source: str) -> None:
    """Synthesise handlers from a TOML shorthand file."""
    import tomllib  # noqa: PLC0415
    import shlex  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    try:
        with open(toml_file, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"[agenthicc] Bad cli.toml at {toml_file}: {exc}", stacklevel=2)
        return

    token = _LOADING_SOURCE.set(source)
    try:
        for spec in data.get("command", []):
            path_list = spec.get("path", [])
            if not path_list:
                continue
            path = tuple(path_list)
            help_str = spec.get("help", "")
            run_tmpl = spec.get("run", "")
            arg_specs = spec.get("args", [])

            def _make_handler(tmpl: str, args: list[dict], h: str) -> Callable:
                async def handler(**kwargs: object) -> None:
                    cmd_str = tmpl.format(**kwargs)
                    subprocess.run(shlex.split(cmd_str), check=True)

                handler.__name__ = "_".join(path_list)
                handler.__doc__ = h
                handler.__annotations__ = {
                    a["name"]: bool if a.get("type") == "bool" else str for a in args
                }
                handler.__kwdefaults__ = {  # type: ignore[attr-defined]
                    a["name"]: a["default"] for a in args if "default" in a
                }
                return handler

            _REGISTRY[path] = _Entry(
                path=path,
                help=help_str,
                handler=_make_handler(run_tmpl, arg_specs, help_str),
                is_async=True,
                source=source,
            )
    finally:
        _LOADING_SOURCE.reset(token)


def _check_shadows(strict: bool = False) -> None:
    """Warn (or error) when a project command shadows a user-global command."""
    user_paths = {p for p, e in _REGISTRY.items() if e.source == "user"}
    project_paths = {p for p, e in _REGISTRY.items() if e.source == "project"}
    conflicts = user_paths & project_paths
    for p in sorted(conflicts):
        msg = (
            f"[agenthicc] .agenthicc/cli/ (project) shadows "
            f"~/.agenthicc/cli/ (user) for command path {list(p)}. "
            f"Run `agenthicc --help` to see which commands are active."
        )
        if strict:
            raise SystemExit(f"error: {msg}")
        warnings.warn(msg, stacklevel=2)


def _discover(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    strict_cli_shadow: bool = False,
) -> None:
    """Discover commands from all three layers in precedence order."""
    # 1. Built-in (lowest priority)
    _discover_package("agenthicc.cli.commands")

    # 2. User-global — implicit trust (same as ~/.bashrc)
    user_cli = (user_dir or Path.home() / ".agenthicc") / "cli"
    _discover_directory(user_cli, source="user")

    # 3. Project-local — highest priority; requires trust manifest
    project_cli = (project_dir or Path(".agenthicc")) / "cli"
    _maybe_load_trusted(project_cli)

    _check_shadows(strict=strict_cli_shadow)


def _maybe_load_trusted(project_cli: Path) -> None:
    """Load project-local CLI plugins only if the trust manifest is valid."""
    if not project_cli.is_dir():
        return

    trust_file = project_cli.parent / "trusted_cli.json"

    # PluginSettings.auto_trust bypasses the check for CI/CD environments.
    try:
        from agenthicc.config import load_config  # noqa: PLC0415

        cfg = load_config()
        if cfg.plugins.auto_trust:
            _discover_directory(project_cli, source="project")
            return
    except Exception:  # noqa: BLE001
        pass

    if not trust_file.exists():
        warnings.warn(
            f"[agenthicc] ⚠  {project_cli}/ has untrusted files.\n"
            f"   Run `agenthicc trust cli` to allow loading.",
            stacklevel=2,
        )
        return

    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    try:
        manifest = json.loads(trust_file.read_text())
    except Exception:  # noqa: BLE001
        warnings.warn(f"[agenthicc] Could not read {trust_file}.", stacklevel=2)
        return

    recorded: dict[str, str] = manifest.get("files", {})
    for py in sorted(project_cli.glob("*.py")):
        if py.name.startswith("_"):
            continue
        rel = str(py.relative_to(project_cli.parent))
        digest = f"sha256:{hashlib.sha256(py.read_bytes()).hexdigest()}"
        if recorded.get(rel) != digest:
            warnings.warn(
                f"[agenthicc] ⚠  {py} has changed since `agenthicc trust cli` was run.\n"
                f"   Re-run `agenthicc trust cli` to allow loading.",
                stacklevel=2,
            )
            return

    _discover_directory(project_cli, source="project")
