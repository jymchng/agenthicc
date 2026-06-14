from __future__ import annotations

import io


def render_markdown_to_lines(
    text: str,
    width: int,
    *,
    force_terminal: bool = True,
) -> list[str]:
    """Render markdown text to a list of ANSI-styled strings using Rich.

    Args:
        text: Markdown text to render.
        width: Terminal width in columns.
        force_terminal: When True, force Rich to emit ANSI codes even when
            the output is not a real terminal (e.g. in tests).

    Returns:
        List of strings, each representing one rendered line (with ANSI codes).
    """
    try:
        from rich.console import Console  # noqa: PLC0415
        from rich.markdown import Markdown  # noqa: PLC0415

        buf = io.StringIO()
        console = Console(
            file=buf,
            width=width,
            force_terminal=force_terminal,
            highlight=False,
            markup=False,
        )
        console.print(Markdown(text))
        rendered = buf.getvalue()
    except ImportError:
        # Fallback: return raw text split into lines
        rendered = text

    # Split into lines preserving ANSI; strip trailing blank lines
    lines = rendered.split('\n')
    # Remove trailing empty lines
    while lines and not lines[-1].strip():
        lines.pop()
    return lines
