"""Single agent turn: LLM streaming, Live spinner, tool signals, transcript wiring."""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


async def _run_agent_turn(
    text: str,
    runner: Any,
    transcript: Any,
    renderer: Any,
    processor: Any,
    session_memory: Any = None,
    max_agent_turns: int = 200,
    pending_queue: list | None = None,
) -> None:
    """Run one agent turn: call LLM, stream response into transcript, show Rich spinner."""
    import re as _re  # noqa: PLC0415
    from lauren_ai._agents import agent as agent_decorator, use_tools  # noqa: PLC0415
    from lauren_ai.testing import _build_runner_for_agent  # noqa: PLC0415
    from agenthicc.kernel import Event  # noqa: PLC0415
    from agenthicc.tui.app import _thinking_wave  # noqa: PLC0415
    from agenthicc.agent_tools import AGENT_TOOLS  # noqa: PLC0415
    from rich.live import Live  # noqa: PLC0415
    from rich.text import Text  # noqa: PLC0415

    if runner is None:
        transcript.append_turn("system", "system", time.monotonic())
        transcript.append_line("system", "⚠ No LLM configured. Set ANTHROPIC_API_KEY or OPENAI_API_KEY.")
        return

    # ── resolve model_id FIRST (fixes UnboundLocalError) ──────────────────
    model_id = (
        runner._transport._config.model
        if hasattr(runner._transport, "_config")
        else "unknown"
    )
    model_short = model_id.split("/")[-1]

    # ── kernel event ──────────────────────────────────────────────────────
    intent_id = uuid.uuid4().hex
    await processor.emit(Event.create("IntentCreated", {"intent_id": intent_id, "raw_text": text}))

    agent_id = f"agent-{intent_id[:8]}"
    transcript.append_turn(agent_id, f"assistant ({model_short})", time.monotonic())

    # ── let signal bridge know the current agent ──────────────────────────
    cell = getattr(getattr(runner, "_signals", None), "_current_agent_cell", None)
    if cell is not None:
        cell[0] = agent_id

    # ── status bar + Rich Live spinner ────────────────────────────────────
    renderer._status.active = True
    renderer._status.intent_started_at = time.monotonic()
    renderer._status.input_tokens = 0
    renderer._status.output_tokens = 0

    # Wire ModelCallComplete signal → live token count updates in the spinner.
    # Fires once per LLM turn so counts accumulate in real-time during multi-turn runs.
    _signals = getattr(runner, "_signals", None)

    # Live tool call list shown inside the spinner block while the agent is thinking.
    # Each entry: {"id": str, "name": str, "args": str, "done": bool, "ok": bool, "ms": float|None}
    _live_calls: list[dict] = []
    # Keyed by tool_use_id: (relative_path, original_content) captured before the call.
    _file_snapshots: dict[str, tuple[str, str]] = {}
    # File-editing tool names that warrant a unified diff in the transcript.
    _FILE_EDIT_TOOLS = {"write_file", "patch_file", "append_file"}

    if _signals is not None:
        from lauren_ai._signals import ModelCallComplete as _MCC  # noqa: PLC0415
        from lauren_ai._signals import ToolCallStarted as _TCS, ToolCallComplete as _TCC  # noqa: PLC0415

        @_signals.on(_MCC)
        async def _on_model_complete(sig: Any) -> None:
            usage = getattr(sig, "usage", None)
            if usage:
                renderer._status.input_tokens += getattr(usage, "input_tokens", 0)
                renderer._status.output_tokens += getattr(usage, "output_tokens", 0)
            renderer._status.session_cost_usd += getattr(sig, "cost_usd", 0.0) or 0.0

        @_signals.on(_TCS)
        async def _on_tool_started(sig: Any) -> None:
            args = dict(getattr(sig, "input", {}) or {})
            tool_name = getattr(sig, "tool_name", "")
            tid = getattr(sig, "tool_use_id", "")
            # Format args as Claude Code style: first value only if single, else key=val pairs
            items = list(args.items())
            if len(items) == 1:
                args_str = repr(items[0][1])[:50]
            elif items:
                args_str = ", ".join(f"{k}={repr(v)[:25]}" for k, v in items[:3])
                if len(items) > 3:
                    args_str += ", …"
            else:
                args_str = ""
            _live_calls.append({
                "id": tid,
                "name": tool_name,
                "args": args_str,
                "done": False,
                "ok": True,
                "ms": None,
            })
            # Snapshot file content before write/patch so we can produce a diff later.
            if tool_name in _FILE_EDIT_TOOLS:
                rel_path = args.get("path", "")
                if rel_path:
                    full = os.path.join(os.getcwd(), rel_path) if not os.path.isabs(rel_path) else rel_path
                    try:
                        original = await asyncio.to_thread(
                            lambda p=full: open(p).read() if os.path.exists(p) else ""
                        )
                        _file_snapshots[tid] = (rel_path, original)
                    except Exception:
                        pass

        @_signals.on(_TCC)
        async def _on_tool_complete(sig: Any) -> None:
            import difflib as _dl  # noqa: PLC0415
            tid = getattr(sig, "tool_use_id", "")
            for entry in _live_calls:
                if entry["id"] == tid:
                    entry["done"] = True
                    entry["ok"] = bool(getattr(sig, "success", True))
                    entry["ms"] = getattr(sig, "duration_ms", None)
                    break
            # Generate unified diff for file-editing tools and store as transcript output.
            if tid in _file_snapshots:
                rel_path, original = _file_snapshots.pop(tid)
                full = os.path.join(os.getcwd(), rel_path) if not os.path.isabs(rel_path) else rel_path
                try:
                    new_content = await asyncio.to_thread(
                        lambda p=full: open(p).read() if os.path.exists(p) else ""
                    )
                    diff = "".join(_dl.unified_diff(
                        original.splitlines(keepends=True),
                        new_content.splitlines(keepends=True),
                        fromfile=f"a/{rel_path}",
                        tofile=f"b/{rel_path}",
                        lineterm="",
                    ))
                    if diff:
                        transcript.finish_tool_call(tool_use_id=tid, output=diff)
                        for entry in _live_calls:
                            if entry["id"] == tid:
                                entry["diff"] = diff
                                break
                except Exception:
                    pass

    # ── @mention injection ────────────────────────────────────────────
    from agenthicc.mentions.injector import build_context_prefix, InjectionConfig  # noqa: PLC0415
    from agenthicc.mentions.parser import parse_mentions as _parse_mentions  # noqa: PLC0415
    from agenthicc.tui.transcript import MentionChip  # noqa: PLC0415
    _exec_cfg = getattr(renderer, "_exec_cfg", None)
    _mention_cfg = InjectionConfig(
        mention_token_budget=getattr(_exec_cfg, "mention_token_budget", 32_000),
        max_file_chars=getattr(_exec_cfg, "mention_max_file_chars", 16_000),
        max_glob_files=getattr(_exec_cfg, "mention_max_glob_files", 20),
        cwd=Path(os.getcwd()),
    )
    _mention_cache_ref = getattr(renderer, "_mention_cache", None)
    _mention_prefix, _injected = await build_context_prefix(
        text,
        cwd=_mention_cfg.cwd,
        cfg=_mention_cfg,
        cache=_mention_cache_ref,
        current_turn=renderer._status.completed_agents,
    )
    _agent_text = _mention_prefix + text if _mention_prefix else text

    # Add @mention chips to transcript
    if _injected:
        from agenthicc.mentions.parser import MentionKind as _MK  # noqa: PLC0415
        for r in _injected:
            if r.mention.kind == _MK.FILE:
                chip = MentionChip(raw=r.mention.raw, kind="file",
                                   display_size=f"{r.chars_used/1024:.1f} KB", ok=r.ok, error=r.error)
            elif r.mention.kind == _MK.DIRECTORY:
                chip = MentionChip(raw=r.mention.raw, kind="dir", display_size="", ok=r.ok)
            elif r.mention.kind == _MK.GLOB:
                count = r.block.count("<file ")
                chip = MentionChip(raw=r.mention.raw, kind="glob",
                                   display_size=f"→ {count} file{'s' if count!=1 else ''}", ok=r.ok)
            elif r.mention.kind == _MK.URL:
                chip = MentionChip(raw=r.mention.raw, kind="url",
                                   display_size=f"{r.chars_used:,} chars" if r.ok else "", ok=r.ok)
            else:
                chip = MentionChip(raw=r.mention.raw, kind="unresolved",
                                   display_size="", ok=False, error="not found")
            transcript.add_mention_chips(agent_id, [chip])
            if r.block and r.ok:
                transcript.set_mention_content(agent_id, r.mention.raw, r.block)

    # ── skills auto-triggering ────────────────────────────────────────
    from agenthicc.skills.runner import find_matching_skills, process_skill_body  # noqa: PLC0415
    _matched_skills = find_matching_skills(text, getattr(renderer, "_skills", {}) or {})
    _skill_suffix = ""
    if _matched_skills:
        _addenda = "\n\n".join(
            f"## Skill: {s.name}\n{process_skill_body(s, args=[], cwd=Path(os.getcwd()))}"
            for s in _matched_skills
        )
        _skill_suffix = f"\n\n---\n\n{_addenda}"

    # Build the agent class with tools so it can actually DO things
    from agenthicc.plugins.registry import build_registry  # noqa: PLC0415
    _mcp_tools = []
    _mcp_reg = getattr(renderer, "_mcp_registry", None)
    if _mcp_reg is not None:
        _mcp_tools = _mcp_reg.all_tools()
    _registry = build_registry(
        agent_name=getattr(renderer, "_active_agent", None) or "default",
        project_plugin_tools=(getattr(renderer, "_project_plugin_tools", None) or []) + _mcp_tools,
    )
    _tool_description = _registry.describe()

    @agent_decorator(
        model=model_id,
        system=(
            "You are a capable AI assistant with access to filesystem, shell, "
            "and git tools. Use them directly to complete tasks. "
            "Give concise responses. Show command output when relevant. "
            "Never invent file contents — always read them first."
            + (_skill_suffix if _skill_suffix else "")
            + (f"\n\n{_tool_description}" if _tool_description else "")
        ),
    )
    @use_tools(*_registry.tools)
    class _AgenthiccAgent: ...

    # Use _build_runner_for_agent so tools are resolved correctly from @use_tools metadata
    _agent_instance = _AgenthiccAgent()
    _active_runner = _build_runner_for_agent(
        _agent_instance,
        runner._transport,
        signals=getattr(runner, "_signals", None),
    )

    live = Live(console=renderer.console, refresh_per_second=12, transient=True)
    live.start()

    from agenthicc.tui.transcript import TranscriptModel as _TM  # noqa: PLC0415
    _MAX_VISIBLE_CALLS = _TM.MAX_VISIBLE_TOOL_CALLS

    # CTRL+O toggles expanded/collapsed; ↑/↓ scroll in expanded mode.
    _expanded = [False]
    _scroll_offset = [0]  # first visible call_line index when expanded
    _queue_input_buf: list[str] = []  # chars typed in the Live block input bar

    async def _watch_input() -> None:
        """Read keystrokes during agent streaming.

        Preserves CTRL+O expand/collapse and arrow-key scroll.  Adds full
        text-entry support so the user can compose and queue messages while
        the agent is running:
          - Printable / UTF-8 chars → _queue_input_buf
          - Backspace (0x7f/0x08)   → pop from _queue_input_buf
          - Ctrl+U (0x15)           → clear _queue_input_buf
          - Enter (0x0d/0x0a)       → submit to pending_queue + print indicator
        """
        import select as _sel, tty as _tty, termios as _tm  # noqa: PLC0415
        from rich.markup import escape as _markup_escape  # noqa: PLC0415
        fd = sys.stdin.fileno()
        try:
            old = _tm.tcgetattr(fd)
            _tty.setcbreak(fd)
        except Exception:
            return
        try:
            while True:
                await asyncio.sleep(0.05)
                r, _, _ = _sel.select([fd], [], [], 0)
                if not r:
                    continue
                b = os.read(fd, 1)

                if b == b"\x0f":                    # CTRL+O — toggle expand
                    _expanded[0] = not _expanded[0]
                    _scroll_offset[0] = 0

                elif b == b"\x1b":                  # escape sequence
                    r2, _, _ = _sel.select([fd], [], [], 0.05)
                    if r2:
                        rest = os.read(fd, 2)
                        seq = b + rest
                        if _expanded[0]:
                            if seq == b"\x1b[A":    # up arrow
                                _scroll_offset[0] = max(0, _scroll_offset[0] - 1)
                            elif seq == b"\x1b[B":  # down arrow
                                _scroll_offset[0] += 1  # clamped in _spin()

                elif b == b"\r":                    # Enter (\r) — submit
                    typed = "".join(_queue_input_buf).strip()
                    _queue_input_buf.clear()
                    if typed:
                        if pending_queue is not None:
                            pending_queue.append(typed)
                        renderer.console.print(
                            f"[dim]❯ {_markup_escape(typed)}  ⌛ Queued[/dim]",
                            markup=True, highlight=False,
                        )
                        renderer._flush_new_lines()

                elif b == b"\n":                    # Ctrl+J — insert newline
                    _queue_input_buf.append("\n")

                elif b in (b"\x7f", b"\x08"):       # Backspace
                    if _queue_input_buf:
                        _queue_input_buf.pop()

                elif b == b"\x15":                  # Ctrl+U — clear input
                    _queue_input_buf.clear()

                else:                               # printable / multi-byte UTF-8
                    raw = b
                    first = b[0]
                    if first & 0b11100000 == 0b11000000:
                        n_extra = 1
                    elif first & 0b11110000 == 0b11100000:
                        n_extra = 2
                    elif first & 0b11111000 == 0b11110000:
                        n_extra = 3
                    else:
                        n_extra = 0
                    for _ in range(n_extra):
                        r3, _, _ = _sel.select([fd], [], [], 0.05)
                        if r3:
                            raw += os.read(fd, 1)
                    try:
                        ch = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        ch = ""
                    if ch and ch.isprintable():
                        _queue_input_buf.append(ch)

        except asyncio.CancelledError:
            pass
        finally:
            try:
                _tm.tcsetattr(fd, _tm.TCSADRAIN, old)
            except Exception:
                pass

    async def _spin() -> None:
        import shutil as _sh  # noqa: PLC0415
        from rich.markup import render as _mk  # noqa: PLC0415
        while True:
            # Flush any completed transcript lines (text turns, finished tool calls)
            # to the permanent scroll buffer before updating the transient Live block.
            renderer._flush_new_lines()
            elapsed = time.monotonic() - renderer._status.intent_started_at
            frame = _thinking_wave(renderer._status.spinner_frame)
            header = (
                f" {frame}  [dim]{elapsed:.1f}s  │[/dim]"
                f"  [cyan]↑ {renderer._status.input_tokens:,}[/cyan]"
                f"  [green]↓ {renderer._status.output_tokens:,}[/green]"
            )
            # Current streaming text (partial turn, cleared at turn boundary).
            _live_text = _streaming_text[0] if _streaming_text else ""

            # Build one entry (list of lines) per tool call so that diff lines
            # are grouped with their parent call, not counted as extra calls.
            entries: list[list[str]] = []
            for call in _live_calls:
                name = call["name"]
                args = call["args"]
                entry: list[str] = []
                if call["done"]:
                    icon = "[green]✓[/green]" if call["ok"] else "[red]✗[/red]"
                    ms = f"  [dim]{call['ms']:.0f}ms[/dim]" if call["ms"] else ""
                    entry.append(
                        f"   [dim]⎿[/dim] [bold]{name}[/bold][dim]({args})[/dim]  {icon}{ms}"
                    )
                    diff_text = call.get("diff", "")
                    if diff_text:
                        diff_lines_all = diff_text.splitlines()
                        diff_preview = diff_lines_all[:8]
                        for dl in diff_preview:
                            if dl.startswith("+++") or dl.startswith("---"):
                                entry.append(f"      [dim]{dl}[/dim]")
                            elif dl.startswith("@@"):
                                entry.append(f"      [dim cyan]{dl}[/dim cyan]")
                            elif dl.startswith("+"):
                                entry.append(f"      [green]{dl}[/green]")
                            elif dl.startswith("-"):
                                entry.append(f"      [red]{dl}[/red]")
                            else:
                                entry.append(f"      [dim]{dl}[/dim]")
                        if len(diff_lines_all) > 8:
                            entry.append(
                                f"      [dim]… {len(diff_lines_all) - 8} more diff lines[/dim]"
                            )
                else:
                    entry.append(
                        f"   [dim]⎿[/dim] [bold]{name}[/bold][dim]({args})[/dim]  [dim]…[/dim]"
                    )
                entries.append(entry)

            n_entries = len(entries)
            if _expanded[0]:
                # Flatten all entries; apply viewport scroll.
                call_lines = [ln for e in entries for ln in e]
                rows = _sh.get_terminal_size((80, 24)).lines
                viewport = max(4, rows - 3)
                total = len(call_lines)
                _scroll_offset[0] = min(_scroll_offset[0], max(0, total - viewport))
                off = _scroll_offset[0]
                end = min(off + viewport, total)
                indicator = (
                    f"   [dim]{off + 1}–{end} of {total}"
                    f"  ↑↓ to scroll  ctrl+O to collapse[/dim]"
                )
                call_lines = call_lines[off:end] + [indicator]
            else:
                # Truncate at the call-entry level so diff lines don't inflate
                # the "hidden" count — a call with a 10-line diff is still 1 call.
                visible_entries = entries[:_MAX_VISIBLE_CALLS]
                hidden = n_entries - len(visible_entries)
                call_lines = [ln for e in visible_entries for ln in e]
                if hidden > 0:
                    # Use parentheses, not square brackets — Rich interprets [] as
                    # markup tags and silently drops unrecognised ones like [ctrl+O].
                    call_lines.append(
                        f"   [dim]… and {hidden} more tool call{'s' if hidden != 1 else ''}"
                        f"  (ctrl+O to expand)[/dim]"
                    )
                elif n_entries > 0:
                    # Always show the hint even when all calls fit on screen.
                    call_lines.append("   [dim](ctrl+O to expand)[/dim]")
            cols = _sh.get_terminal_size((80, 24)).columns
            # Append partial streaming text below tool calls, above the status line.
            if _live_text:
                _display = _live_text.replace("\n", " ")
                if len(_display) > cols - 4:
                    _display = _display[:cols - 7] + "…"
                call_lines.append(f"   [dim]{_display}[/dim]")
            # Top rule separates user input (scroll buffer) from agent response.
            # Bottom rule + ❯ is the persistent input bar pinned at the bottom.
            from agenthicc.tui.input_area import (  # noqa: PLC0415
                get_mode_str as _ia_mode_str,
                prompt_markup as _ia_prompt,
                footer_markup as _ia_footer,
            )
            top_rule = "[dim]" + "─" * cols + "[/dim]"
            bot_rule = "[dim]" + "─" * cols + "[/dim]"
            _q_count = len(pending_queue) if pending_queue is not None else 0
            _hdr = header + (f"  [dim]({_q_count} queued)[/dim]" if _q_count else "")
            _bar = _ia_prompt("".join(_queue_input_buf), cols)
            _mode_str = _ia_mode_str(getattr(renderer, "_mode_manager", None))
            _mode_border, _mode_text = _ia_footer(_mode_str, cols)
            live.update(_mk("\n".join(
                [top_rule] + call_lines + [_hdr, bot_rule, _bar, _mode_border, _mode_text]
            )))
            renderer._status.spinner_frame += 1
            await asyncio.sleep(0.05)

    # Live-streaming text shared with _spin() so partial text appears in the
    # spinner as the model generates it.  Cleared at each turn boundary.
    _streaming_text: list[str] = [""]   # [0] = current partial turn text

    spin_task = asyncio.create_task(_spin())
    input_task = asyncio.create_task(_watch_input())

    # Per-turn text accumulation (run_stream yields chunks across all turns)
    _current_turn: list[str] = []
    _all_turn_texts: list[str] = []     # one entry per LLM turn that produced text
    content = ""

    try:
        from lauren_ai._config import AgentConfig as _AgentConfig  # noqa: PLC0415
        _cfg = _AgentConfig(
            max_turns=max_agent_turns,
            parallel_tool_calls=True,
        )
        _stream = await _active_runner.run_stream(
            _agent_instance,
            _agent_text,
            memory=session_memory,
            config_override=_cfg,
        )
        async for _chunk in _stream:
            # Accumulate text delta into the current turn buffer.
            if _chunk.delta:
                _current_turn.append(_chunk.delta)
                _streaming_text[0] = "".join(_current_turn)

            # A chunk with stop_reason marks the end of one LLM turn.
            if _chunk.stop_reason is not None:
                _turn_text = "".join(_current_turn).strip()
                if _turn_text:
                    _all_turn_texts.append(_turn_text)
                    # Add text to transcript NOW — before _stream_loop() executes
                    # tools for this turn.  ToolCallStarted/Complete signals fire
                    # after this yield, so the transcript order becomes:
                    #   turn-1 text → turn-1 tools → turn-2 text → turn-2 tools …
                    transcript.append_line(agent_id, "\x00md\x00" + _turn_text)
                    # Immediately print the completed turn text to the scroll buffer
                    # so it persists above the tool-call Live block.
                    renderer._flush_new_lines()
                _current_turn = []
                _streaming_text[0] = ""

        # Build final content from the last turn (replaces response.content).
        content = _all_turn_texts[-1] if _all_turn_texts else ""
        # Strip any residual XML that slipped through
        content = _re.sub(r"<tool_call>.*?</tool_call>", "", content, flags=_re.DOTALL)
        content = _re.sub(r"<[^>]+>", "", content).strip()
        # If tools ran but the final turn produced no prose, that's fine — skip it.
        if not content and not _live_calls:
            content = "(no response)"

    except (asyncio.CancelledError, KeyboardInterrupt):
        # Ctrl+C pressed while the agent was running.  Clean exit — nothing to
        # append to the transcript; the input loop will resume normally.
        content = ""
        _all_turn_texts = []
    except Exception as exc:
        _all_turn_texts = []
        content = ""
        _err_line = f"[red bold]⚠ {type(exc).__name__}:[/red bold] [red]{exc}[/red]"
        transcript.append_line(agent_id, _err_line)
        renderer._flush_new_lines()
    finally:
        _streaming_text[0] = ""
        spin_task.cancel()
        input_task.cancel()
        # Protect the cleanup await: if this task is being cancelled a second
        # time the gather itself could raise CancelledError mid-flight.
        try:
            await asyncio.gather(spin_task, input_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        live.stop()
        renderer._status.active = False
        renderer._status.completed_agents += 1

    if content == "(no response)":
        transcript.append_line(agent_id, "\x00md\x00" + content)

    await processor.emit(
        Event.create("IntentStatusChanged", {"intent_id": intent_id, "status": "complete"})
    )
