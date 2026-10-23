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
from typing import Callable, TypeAlias, TypedDict

from agenthicc.plugins.trust import current_extension_trust

Handler: TypeAlias = Callable[..., object]


class _TreeNode(TypedDict):
    help: str
    entry: _Entry | None
    children: dict[str, _TreeNode]
    source: str


Tree: TypeAlias = dict[str, _TreeNode]


# ── entry dataclass ────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    path: tuple[str, ...]
    help: str
    handler: Handler
    is_async: bool
    source: str = "builtin"  # "builtin" | "user" | "project"


# ── flat registries ────────────────────────────────────────────────────────────

_REGISTRY: dict[tuple[str, ...], _Entry] = {}
_GROUPS: dict[tuple[str, ...], str] = {}

# ContextVar lets @command() read the source tag at decoration time even
# when commands are loaded dynamically from user/project directories.
_LOADING_SOURCE: ContextVar[str] = ContextVar("_LOADING_SOURCE", default="builtin")


# ── public decorators ─────────────────────────────────────────────────────────


def command(*path: str, help: str = "") -> Callable[[Handler], Handler]:
    """Register a leaf command handler at *path*.

    Signature inference rules (applied at argparse-build time):
      param: str            (no default)  → positional argument
      flag:  bool = False                 → --flag  (store_true)
      opt:   str  = "value"               → --opt VALUE
      ann:   CLIContext                   → injected at call time; never argparse arg
    """

    def decorator(fn: Handler) -> Handler:
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


def group(*path: str, help: str = "") -> Callable[[Handler | None], Handler | None]:
    """Declare a command group (branch node with no handler of its own)."""
    _GROUPS[path] = help

    def decorator(fn: Handler | None = None) -> Handler | None:
        return fn

    return decorator


def optional_positionals(*names: str) -> Callable[[Handler], Handler]:
    """Mark defaulted string parameters as optional positional arguments.

    Most CLI defaults intentionally become ``--option`` flags. A small number
    of commands, such as ``mcp doctor [NAME]``, have a conventional optional
    positional form, so they opt into it explicitly instead of changing the
    inference rule for every existing command.
    """

    def decorator(fn: Handler) -> Handler:
        # Metadata on a function object; callables have no typed attributes.
        setattr(fn, "_optional_positionals", frozenset(names))  # noqa: B010
        return fn

    return decorator


# ── argparse wiring ────────────────────────────────────────────────────────────


def _add_params(parser: argparse.ArgumentParser, fn: Handler) -> None:
    """Add argparse arguments inferred from the function signature."""
    from agenthicc.cli.context import CLIContext

    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    optional: frozenset[str] = getattr(fn, "_optional_positionals", frozenset())
    for name, param in sig.parameters.items():
        ann = hints.get(name, inspect.Parameter.empty)
        default = param.default
        empty = inspect.Parameter.empty
        if ann is CLIContext:
            continue  # injected — skip
        # A trailing underscore lets handlers use Python keywords, e.g.
        # ``global_`` becomes the user-facing ``--global`` option.
        option_name = name[:-1] if name.endswith("_") else name
        option = f"--{option_name.replace('_', '-')}"
        if name in optional:
            parser.add_argument(
                name,
                nargs="?",
                default=None if default is empty else default,
                metavar=name.upper(),
            )
            continue
        if ann is bool:
            parser.add_argument(
                option,
                action="store_true",
                dest=name,
                default=default if default is not empty else False,
            )
        elif default is empty:
            parser.add_argument(name, metavar=name.upper())  # positional
        else:
            parser.add_argument(
                option,
                dest=name,
                default=default,
                type=ann if ann in (int, float) else str,
            )


def _as_tree() -> Tree:
    """Build a nested dict from the flat _REGISTRY and _GROUPS."""
    tree: Tree = {}

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


def _wire(parser: argparse.ArgumentParser, tree: Tree) -> None:
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
    from agenthicc.cli.context import CLIContext

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
    result = entry.handler(**kwargs)
    if inspect.isawaitable(result):

        async def _wait() -> object:
            return await result

        asyncio.run(_wait())


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

    trust = current_extension_trust()
    token = _LOADING_SOURCE.set(source)
    try:
        for py in sorted(directory.glob("*.py")):
            if py.name.startswith("_") or not trust.permits(py, kind="cli"):
                continue
            mod_name = f"agenthicc._cli_plugin.{source}.{py.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as exc:  # noqa: BLE001
                warnings.warn(
                    f"[agenthicc] Failed to load CLI plugin {py}: {exc}",
                    stacklevel=2,
                )

        # Optional TOML shorthand: cli.toml sits beside the cli/ directory.
        # Its commands run shell templates, so it needs trust like Python.
        toml_file = directory.parent / "cli.toml"
        if toml_file.exists() and trust.permits(toml_file, kind="cli"):
            _load_toml_commands(toml_file, source=source)
    finally:
        _LOADING_SOURCE.reset(token)


def _load_toml_commands(toml_file: Path, source: str) -> None:
    """Synthesise handlers from a TOML shorthand file."""
    import tomllib
    import shlex
    import subprocess

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

            def _make_handler(
                tmpl: str, args: list[dict[str, object]], h: str, handler_name: str
            ) -> Handler:
                async def handler(**kwargs: object) -> None:
                    argv = shlex.split(tmpl.format(**kwargs))
                    process = await asyncio.create_subprocess_exec(*argv)
                    returncode = await process.wait()
                    if returncode != 0:
                        raise subprocess.CalledProcessError(returncode, argv)

                handler.__name__ = handler_name
                handler.__doc__ = h
                annotations: dict[str, object] = {}
                defaults: dict[str, object] = {}
                for arg in args:
                    name = arg.get("name")
                    if not isinstance(name, str):
                        continue
                    annotations[name] = bool if arg.get("type") == "bool" else str
                    if "default" in arg:
                        defaults[name] = arg["default"]
                handler.__annotations__ = annotations
                handler.__kwdefaults__ = defaults
                return handler

            _REGISTRY[path] = _Entry(
                path=path,
                help=help_str,
                handler=_make_handler(run_tmpl, arg_specs, help_str, "_".join(path_list)),
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
    """Discover commands from all three layers in precedence order.

    Every plugin file must be trusted (``agenthicc trust``); files in the
    user's own ``~/.agenthicc`` are trusted by location.
    """
    # 1. Built-in (lowest priority)
    _discover_package("agenthicc.cli.commands")

    # 2. User-global
    user_cli = (user_dir or Path.home() / ".agenthicc") / "cli"
    _discover_directory(user_cli, source="user")

    # 3. Project-local — highest priority
    project_cli = (project_dir or Path(".agenthicc")) / "cli"
    _discover_directory(project_cli, source="project")

    _check_shadows(strict=strict_cli_shadow)
