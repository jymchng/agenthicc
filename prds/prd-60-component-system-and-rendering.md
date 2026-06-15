# PRD-60 — Component System & Rendering Pipeline

## 1. Purpose

Define how UI components are structured, how they subscribe to reactive state,
and how they produce Rich renderables. Rich is a **dumb rendering backend** —
it receives renderables from components but owns no state and no lifecycle.

---

## 2. Component Contract

```python
from __future__ import annotations
from typing import Any
from abc import ABC, abstractmethod


class Component(ABC):
    """Base class for all UI components.

    Lifecycle:
        mount()   → called once when component is added to the workspace
        unmount() → called once when component is removed
        render()  → called on every redraw cycle; must be pure/fast

    Rules:
        1. render() reads reactive state only — no mutations
        2. Event handling mutates state via CommandBus or store methods
        3. Components subscribe to signals in mount(), unsubscribe in unmount()
        4. No component calls another component's methods directly
    """

    def __init__(self, app_state: Any) -> None:
        self._state = app_state
        self._unsubs: list[Any] = []   # unsubscribe callables

    def mount(self) -> None:
        """Called once when component enters the workspace."""

    def unmount(self) -> None:
        """Called once when component leaves the workspace."""
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()

    @abstractmethod
    def render(self) -> Any:
        """Return a Rich renderable (Text, Panel, Group, etc.)."""

    def _subscribe(self, signal: Any, fn: Any) -> None:
        """Subscribe to a signal and track for cleanup."""
        self._unsubs.append(signal.subscribe(fn))
```

---

## 3. Workspace Layout

The workspace is the **root component**. It owns the terminal layout
and delegates to child components. It does not navigate.

```
Workspace
│
├── ContextStrip        (top bar: session, model, tokens, cost)
├── ConversationSurface (scroll buffer projection — read-only)
├── LiveRegion          (always-on Rich Live block)
│   ├── StatusComponent  (flower + animation + tool + runtime)
│   ├── [border]
│   ├── ComposerComponent (❯ input▌)
│   ├── [border]
│   └── FooterComponent   (mode str + hints)
└── OverlayHost          (z-layer above workspace, for menus/pickers)
```

```python
class Workspace:
    """Root component — owns the terminal for the application lifetime."""

    def __init__(self, app_state: AppState, console: Console) -> None:
        self._state   = app_state
        self._console = console

        # Child components
        self.context_strip = ContextStripComponent(app_state)
        self.status        = StatusComponent(app_state)
        self.composer      = ComposerComponent(app_state)
        self.footer        = FooterComponent(app_state)
        self.overlay_host  = OverlayHost(app_state)

        # Scroll buffer appender — writes conversation to stdout
        self.scroll        = ScrollBufferAppender(app_state, console)

        # The Live block (always-on)
        self._live: Live | None = None

    def start(self) -> None:
        self.context_strip.mount()
        self.status.mount()
        self.composer.mount()
        self.footer.mount()
        self.scroll.mount()

        self._live = Live(
            self._build_live(),
            console=self._console,
            auto_refresh=False,
            transient=False,   # NOT transient — it's always-on
        )
        self._live.start()

        # All state subscriptions trigger _redraw
        for sig in (
            self._state.conversation.agent_state,
            self._state.conversation.active_tool,
            self._state.conversation.elapsed_s,
            self._state.conversation.model_name,
            self._state.conversation.total_tokens,
            self._state.conversation.cost_usd,
            self._state.input.buf,
            self._state.input.cursor,
            self._state.input.paste_condensed,
            self._state.input.paste_label,
            self._state.overlay,
        ):
            sig.subscribe(self._redraw)

    def stop(self) -> None:
        if self._live:
            self._live.stop()
        self.context_strip.unmount()
        self.status.unmount()
        self.composer.unmount()
        self.footer.unmount()
        self.scroll.unmount()

    def _build_live(self) -> Any:
        from rich.console import Group
        parts = [self.status.render()]
        if self.overlay_host.active:
            parts.append(self.overlay_host.render())
        parts += [
            _border(self._console),
            self.composer.render(),
            _border(self._console),
            self.footer.render(),
        ]
        return Group(*parts)

    def _redraw(self) -> None:
        if self._live is not None:
            try:
                self._live.update(self._build_live())
            except OSError:
                pass
```

---

## 4. StatusComponent

```python
class StatusComponent(Component):
    """
    Line 1: ✿ Thinking │ Runtime: 00:15 │ active_tool
    Line 2: model_name │ Tokens: 12k │ $0.0045
    """

    def render(self) -> Any:
        from rich.text import Text
        store = self._state.conversation
        cols  = _get_cols()

        # Flower + animation
        flower     = _FLOWERS[store._flower_frame % len(_FLOWERS)]
        state_text = _thinking_markup(store._thinking_frame) if store.is_running() else store.agent_state().name.title()
        line1 = _build_status_line_1(flower, state_text, store.active_tool(), store.elapsed_s(), cols)
        line2 = _build_status_line_2(store.model_name(), store.total_tokens(), store.cost_usd(), cols)

        return Group(Text.from_markup(line1), Text.from_markup(line2))
```

---

## 5. ComposerComponent

```python
class ComposerComponent(Component):
    """Renders the ❯ input▌ line from InputState signals."""

    def render(self) -> Any:
        from rich.text import Text
        from agenthicc.tui.input.renderer import build_prompt

        inp = self._state.input
        buf    = inp.buf()
        cursor = inp.cursor()

        if inp.paste_condensed():
            disp_buf    = list(inp.paste_label())
            disp_cursor = len(disp_buf)
        else:
            disp_buf    = buf
            disp_cursor = cursor

        cols    = _get_cols()
        prompt  = build_prompt(disp_buf, disp_cursor)
        line    = _fit(prompt, cols)
        return Text.from_markup(line)
```

---

## 6. FooterComponent

```python
_HINTS: dict[str, str] = {
    "idle":     "Enter Submit  Ctrl+J Newline  /cmd  @Mention",
    "thinking": "Esc Interrupt",
    "running":  "Esc Interrupt  Ctrl+Z Background",
    "error":    "R Retry  Esc Dismiss",
}

class FooterComponent(Component):
    """
    Row 1: mode string (⏵⏵ Auto ...)
    Row 2: context-sensitive key hints
    """

    def render(self) -> Any:
        from rich.text import Text
        from rich.console import Group
        cols    = _get_cols()
        mode    = self._state.conversation.mode_str()
        state   = self._state.conversation.agent_state()
        hints   = _HINTS.get(state.name.lower(), _HINTS["idle"])
        return Group(
            Text.from_markup(_fit(f"  [dim]{mode}[/dim]", cols)),
            Text.from_markup(_fit(_build_hints(hints), cols)),
        )
```

---

## 7. ScrollBufferAppender

The `ScrollBufferAppender` is the **only component allowed to call
`console.print()`**. It subscribes to `ConversationStore.on_event()` and
renders each event exactly once to stdout (above the always-on Live region).

```python
class ScrollBufferAppender(Component):
    """Writes conversation events to the scroll buffer. Never to the Live region."""

    def __init__(self, app_state: AppState, console: Console) -> None:
        super().__init__(app_state)
        self._console = console

    def mount(self) -> None:
        unsub = self._state.conversation.on_event(self._on_event)
        self._unsubs.append(unsub)

    def render(self) -> Any:
        return None  # ScrollBufferAppender does not render into the Live region

    def _on_event(self, ev: ConversationEvent) -> None:
        if ev.rendered:
            return
        ev.rendered = True

        match ev.kind:
            case "turn_start":
                self._console.print(
                    f"[bold cyan]●[/bold cyan] [bold]{ev.payload['agent_name']}[/bold]"
                    f"  [dim]{_hhmmss(ev.timestamp)}[/dim]",
                    markup=True, highlight=False,
                )
            case "tool_complete":
                self._render_tool_complete(ev.payload)
            case "text":
                from rich.markdown import Markdown
                self._console.print(Markdown(ev.payload["text"]), end="")
            case "error":
                self._console.print(
                    f"[red bold]ERROR[/red bold] {ev.payload['message']}",
                    markup=True, highlight=False,
                )

    def _render_tool_complete(self, payload: dict) -> None:
        from rich.markup import escape as _e
        name     = _e(payload.get("name", ""))
        args_str = payload.get("args_str", "")
        icon     = "[green]✓[/green]" if payload.get("success") else "[red]✗[/red]"
        dur      = payload.get("dur_str", "")
        self._console.print(
            f"  [dim]⎿[/dim] [bold]{name}[/bold]{args_str}  {icon}{dur}",
            markup=True, highlight=False,
        )
        for line in payload.get("output_lines", [])[:4]:
            self._console.print(f"    [dim]{_e(line[:120])}[/dim]", markup=True, highlight=False)
```

---

## 8. Rendering Pipeline

```
Reactive Signal change
    │
    ▼
Signal.set() → notify subscribers
    │
    ▼
Workspace._redraw()
    │
    ▼
_build_live()
    │
    ├── StatusComponent.render()     → Rich Text (2 lines)
    ├── [border]                     → Rich Text
    ├── ComposerComponent.render()   → Rich Text
    ├── [border]                     → Rich Text
    └── FooterComponent.render()     → Rich Group (2 lines)
    │
    ▼
Rich Group(...)
    │
    ▼
self._live.update(group)
    │
    ▼
Rich renders to terminal (cursor manipulation, no background thread)
```

Key property: **the pipeline is synchronous and single-threaded**. No locks
needed. No race conditions. `auto_refresh=False` ensures no background thread
interferes.

---

## 9. ContextStripComponent (Terminal Title Bar)

```python
class ContextStripComponent(Component):
    """
    Printed ONCE before each input prompt:
      openai/poolside/laguna-xs.2  |  3 turns  |  $0.015  ↑ 12k  ↓ 8k
      ────────────────────────────────────────────────────────────────────
    """

    def print_idle_header(self, console: Console) -> None:
        """Called by InputSession before each new prompt, not in Live region."""
        store = self._state.conversation
        cols  = _get_cols()
        sid   = store.session_id() or "session"
        turns = store.turn_count()
        cost  = f"${store.cost_usd():.3f}"
        console.print(
            f" [dim]{sid}  |  {turns} turn{'s' if turns != 1 else ''}  |  {cost}[/dim]"
            f"  [cyan]↑ {store.tokens_in():,}[/cyan]"
            f"  [green]↓ {store.tokens_out():,}[/green]",
            markup=True, highlight=False,
        )
        console.print(f"[dim]{'─' * cols}[/dim]", markup=True, highlight=False)
```

---

## 10. Component Isolation Rules

| Rule | Rationale |
|---|---|
| Components read state via signals only | No direct store mutation from render path |
| Components emit events upward via CommandBus | No component-to-component direct calls |
| render() is always fast and pure | No I/O, no blocking, no side effects |
| ScrollBufferAppender is the only console.print caller | Single point of scroll buffer writes |
| Live region never contains conversation history | Scroll buffer and Live region are separate surfaces |
