"""CLI command registry: the built-in manifest plus plugin commands (PRD-79, AGX-01).

Built-in commands are declared in :mod:`agenthicc.cli.manifest` and imported
only when an invocation selects them. Plugin commands in ``~/.agenthicc/cli/``
and ``.agenthicc/cli/`` register with the decorators below and override a
built-in command at the same path:

    from agenthicc.cli.registry import command, group
    from agenthicc.cli.context import CLIContext

    @group("deploy", help="Deploy this project")
    def _(): ...

    @command("deploy", "preview")
    async def deploy_preview(ctx: CLIContext, branch: str = "main") -> None:
        '''Deploy a preview environment.'''
        ...

CLIContext is injected by annotation type, not by parameter name — any name works.
Positional args, --flags, and --options are inferred from the function signature.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import sys
import typing
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from agenthicc.cli.manifest import BUILTIN_COMMANDS, BUILTIN_GROUPS, CommandRef

Handler: TypeAlias = Callable[..., object]
CommandPath: TypeAlias = tuple[str, ...]


# ── entries ────────────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    path: CommandPath
    help: str
    handler: Handler
    is_async: bool
    source: str = "builtin"  # "builtin" | "user" | "project" | "plugin"


# Plugin registrations only; built-in commands come from the manifest.
_PLUGIN_COMMANDS: dict[CommandPath, _Entry] = {}
_PLUGIN_GROUPS: dict[CommandPath, str] = {}

# ContextVar lets @command() read the source tag at decoration time even
# when commands are loaded dynamically from user/project directories.
_LOADING_SOURCE: ContextVar[str] = ContextVar("_LOADING_SOURCE", default="plugin")


# ── public decorators (plugin API) ─────────────────────────────────────────────


def command(*path: str, help: str = "") -> Callable[[Handler], Handler]:
    """Register a plugin command handler at *path*.

    Signature inference rules (applied at argparse-build time):
      param: str            (no default)  → positional argument
      flag:  bool = False                 → --flag  (store_true)
      opt:   str  = "value"               → --opt VALUE
      ann:   CLIContext                   → injected at call time; never argparse arg
    """

    def decorator(fn: Handler) -> Handler:
        doc = help or (inspect.getdoc(fn) or "").splitlines()[0]
        _PLUGIN_COMMANDS[path] = _Entry(
            path=path,
            help=doc,
            handler=fn,
            is_async=inspect.iscoroutinefunction(fn),
            source=_LOADING_SOURCE.get(),
        )
        return fn

    return decorator


def group(*path: str, help: str = "") -> Callable[[Handler | None], Handler | None]:
    """Declare a plugin command group (branch node with no handler of its own)."""
    _PLUGIN_GROUPS[path] = help

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
        fn.__dict__["_optional_positionals"] = frozenset(names)
        return fn

    return decorator


# ── the command index ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandIndex:
    """Every known command path and group, built-in and plugin, without imports."""

    commands: Mapping[CommandPath, CommandRef | _Entry]
    groups: Mapping[CommandPath, str]

    def nodes(self) -> list[CommandPath]:
        """Every command and group path, in declaration order."""
        seen: dict[CommandPath, None] = {}
        for path in (*self.groups, *self.commands):
            for depth in range(1, len(path) + 1):
                seen.setdefault(path[:depth], None)
        return list(seen)

    def children(self, prefix: CommandPath) -> list[str]:
        """Names of the nodes directly below *prefix*, in declaration order."""
        names: dict[str, None] = {}
        for path in self.nodes():
            if len(path) == len(prefix) + 1 and path[: len(prefix)] == prefix:
                names.setdefault(path[-1], None)
        return list(names)

    def help_for(self, path: CommandPath) -> str:
        source = self.commands.get(path)
        if source is not None and source.help:
            return source.help
        return self.groups.get(path, "")

    def source_for(self, path: CommandPath) -> str:
        source = self.commands.get(path)
        return source.source if isinstance(source, _Entry) else "builtin"

    def resolve(self, tokens: Sequence[str]) -> CommandPath | None:
        """Return the longest command or group path that prefixes *tokens*."""
        known = set(self.nodes())
        best: CommandPath | None = None
        for depth in range(1, len(tokens) + 1):
            candidate = tuple(tokens[:depth])
            if candidate in known:
                best = candidate
            else:
                break
        return best


def command_index() -> CommandIndex:
    """Merge the built-in manifest with registered plugins; plugins win."""
    commands: dict[CommandPath, CommandRef | _Entry] = {ref.path: ref for ref in BUILTIN_COMMANDS}
    commands.update(_PLUGIN_COMMANDS)
    groups = dict(BUILTIN_GROUPS)
    groups.update(_PLUGIN_GROUPS)
    return CommandIndex(commands=commands, groups=groups)


def load_entry(source: CommandRef | _Entry) -> _Entry:
    """Import a built-in command's module and return its entry."""
    if isinstance(source, _Entry):
        return source
    module_name, _, function_name = source.target.partition(":")
    handler: Handler = vars(importlib.import_module(module_name))[function_name]
    return _Entry(
        path=source.path,
        help=source.help,
        handler=handler,
        is_async=inspect.iscoroutinefunction(handler),
        source="builtin",
    )


def leading_tokens(arguments: Sequence[str]) -> list[str]:
    """Return the positional words before the first option in *arguments*."""
    tokens: list[str] = []
    for argument in arguments:
        if argument.startswith("-"):
            break
        tokens.append(argument)
    return tokens


# ── argparse wiring ────────────────────────────────────────────────────────────


def _add_params(parser: argparse.ArgumentParser, fn: Handler) -> bool:
    """Add argparse arguments inferred from the function signature.

    Returns whether an optional positional argument was added.
    """
    from agenthicc.cli.context import CLIContext

    hints = typing.get_type_hints(fn)
    sig = inspect.signature(fn)
    optional: frozenset[str] = getattr(fn, "_optional_positionals", frozenset())
    added_optional = False
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
            added_optional = True
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
    return added_optional


def wire(
    parser: argparse.ArgumentParser,
    index: CommandIndex,
    selected: CommandPath | None,
) -> _Entry | None:
    """Wire subcommands into *parser* and return the selected command's entry.

    Only the command on the *selected* path is imported and given its real
    parameters. Every other node is a help-only stub, so ``--help`` listings
    and "invalid choice" errors stay complete without importing their modules.
    """
    return _wire_level(parser, (), index, selected or ())


def _wire_level(
    parser: argparse.ArgumentParser,
    prefix: CommandPath,
    index: CommandIndex,
    selected: CommandPath,
) -> _Entry | None:
    names = index.children(prefix)
    if not names:
        return None
    subparsers = parser.add_subparsers(metavar="<subcommand>")
    # A group without a handler of its own needs a subcommand; the root does
    # not (no subcommand starts the TUI), nor does a command with children.
    subparsers.required = bool(prefix) and prefix not in index.commands
    selected_entry: _Entry | None = None
    for name in names:
        path = (*prefix, name)
        source = index.source_for(path)
        badge = f"  [{source}]" if source != "builtin" else ""
        child = subparsers.add_parser(name, help=f"{index.help_for(path)}{badge}")
        if selected[: len(path)] != path:
            continue
        if path == selected and path in index.commands:
            selected_entry = load_entry(index.commands[path])
            optional = _add_params(child, selected_entry.handler)
            child.set_defaults(_entry=selected_entry)
            if optional and index.children(path):
                # argparse would give an optional positional's value to the
                # subcommand slot. The path resolved to this command, so the
                # next word is not a subcommand: list them in the help instead.
                child.epilog = "subcommands: " + ", ".join(index.children(path))
                continue
        nested = _wire_level(child, path, index, selected)
        selected_entry = nested or selected_entry
    return selected_entry


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
        import asyncio

        async def _wait() -> object:
            return await result

        asyncio.run(_wait())


# ── plugin discovery ───────────────────────────────────────────────────────────


def _discover_directory(directory: Path, source: str) -> None:
    """Dynamically load command plugins from *directory*, tagged with *source*."""
    if not directory.is_dir():
        return

    from agenthicc.plugins.trust import current_extension_trust

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
                    import asyncio

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

            _PLUGIN_COMMANDS[path] = _Entry(
                path=path,
                help=help_str,
                handler=_make_handler(run_tmpl, arg_specs, help_str, "_".join(path_list)),
                is_async=True,
                source=source,
            )
    finally:
        _LOADING_SOURCE.reset(token)


def _check_shadows(user_paths: set[CommandPath], strict: bool = False) -> None:
    """Warn (or error) when a project command shadows a user-global command."""
    project_paths = {path for path, entry in _PLUGIN_COMMANDS.items() if entry.source == "project"}
    for path in sorted(user_paths & project_paths):
        msg = (
            f"[agenthicc] .agenthicc/cli/ (project) shadows "
            f"~/.agenthicc/cli/ (user) for command path {list(path)}. "
            f"Run `agenthicc --help` to see which commands are active."
        )
        if strict:
            raise SystemExit(f"error: {msg}")
        warnings.warn(msg, stacklevel=2)


def discover_plugins(
    project_dir: Path | None = None,
    user_dir: Path | None = None,
    strict_cli_shadow: bool = False,
) -> None:
    """Load user-global then project-local command plugins.

    Every plugin file must be trusted (``agenthicc trust``); files in the
    user's own ``~/.agenthicc`` are trusted by location. A directory that does
    not exist costs one ``stat``.
    """
    user_cli = (user_dir or Path.home() / ".agenthicc") / "cli"
    _discover_directory(user_cli, source="user")
    user_paths = {path for path, entry in _PLUGIN_COMMANDS.items() if entry.source == "user"}

    project_cli = (project_dir or Path(".agenthicc")) / "cli"
    _discover_directory(project_cli, source="project")

    _check_shadows(user_paths, strict=strict_cli_shadow)
