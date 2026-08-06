"""make_pdf_book — Write a specialised, technical book chapter-by-chapter and compile it into a PDF.

Same shape as ``make_epub_book``, but the output is a polished, typeset PDF
instead of an EPUB. The workflow:

1. ``toc`` — plans the table of contents (title, author, audience, technical
   level, chapter list with outlines, output directory, rich content types).
2. ``research`` — performs extensive, authoritative technical research across
   every chapter topic, recording per-chapter notes, data, sources, and assets.
3. ``chapter`` — one phase run PER chapter. The runner re-enters this phase
   once for every chapter in the TOC, so the effective number of phases is
   dynamic: ``N chapters + 3`` for a given run.
4. ``compile`` — typesets all chapters into a PDF (typst → xelatex →
   chromium+MathJax → reportlab) and validates the result.

Books are RICH in content: images and figures with captions, LaTeX equations
(native typeset math in the PDF), syntax-highlighted code snippets, and data
tables — all preserved through compilation.

The declarative ``phases`` list is a static 4-node skeleton because the
registry reads it at discovery time; the custom runner owns the per-chapter
loop and updates the TUI phase counter dynamically.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Callable
from enum import Enum, auto
from typing import TYPE_CHECKING

from agenthicc.workflows.code_plan.runner import CodePlanRunner
from agenthicc.workflows.plugin import PhaseSpec, WorkflowParams, WorkflowPlugin

if TYPE_CHECKING:
    from lauren_ai._memory import ShortTermMemory
    from agenthicc.tui.runtime.mode_manager import ModeManager
    from agenthicc.workflows.config import WorkflowConfig

log = logging.getLogger(__name__)

#: Bounded retries per phase — never loop forever waiting for a tool call.
_MAX_ATTEMPTS = 5

# Stable workflow policy. Book metadata, research notes, chapter content,
# artifact paths, and validation results remain dynamic phase context.
CACHE_CONTRACT = """
[MAKE PDF BOOK CACHE CONTRACT]
Keep this book-authoring policy unchanged across TOC, RESEARCH, CHAPTER,
ASSETS, FRONT_MATTER, BACK_MATTER, and COMPILE. Ask the user a focused
clarifying question through the existing ask_user tool whenever a missing or
ambiguous requirement could materially change the book; wait for the answer
instead of guessing. Actual questions and answers are dynamic context.

Keep the title, author, audience, chapter list, research notes, written
content, asset paths, validation reports, retry details, and phase state in
dynamic context. Do not insert changing book data into this stable contract,
rewrite the beginning of shared conversation history, or put rolling summaries
here. Use the shared run_phase API so stable tools remain ordered before
phase-local transition tools and the session's capability, approval,
workspace, and memory policies remain authoritative.
""".strip()


#: MathJax node renderer template injected into the compile prompt. Used by the
#: CHROMIUM-HEADLESS fallback tier of the PDF pipeline: it replaces pandoc's
#: MathML <math> elements with self-contained SVG images (glyphs inline as path
#: data — no fonts, no JS, no network), which Chromium then prints to PDF as
#: crisp vector math. Hardened against the pitfalls that break strict parsers.
_MATHJAX_RENDERER_JS = r"""// render-math.js — replace pandoc MathML <math> with self-contained MathJax SVG.
// Usage: node render-math.js <file.xhtml>   (edits the file in place)
const fs = require('fs');
const { mathjax } = require('mathjax-full/js/mathjax.js');
const { TeX } = require('mathjax-full/js/input/tex.js');
const { SVG } = require('mathjax-full/js/output/svg.js');
const { liteAdaptor } = require('mathjax-full/js/adaptors/liteAdaptor.js');
const { RegisterHTMLHandler } = require('mathjax-full/js/handlers/html.js');
const { AllPackages } = require('mathjax-full/js/input/tex/AllPackages.js');

const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const tex = new TeX({ packages: AllPackages });
const svg = new SVG({ fontCache: 'none' });          // glyphs inline: no fonts, no JS
const doc = mathjax.document('', { InputJax: tex, OutputJax: svg });

const file = process.argv[2];
let html = fs.readFileSync(file, 'utf8');
let count = 0;

html = html.replace(/<math[\s\S]*?<\/math>/g, (block) => {
  const display = /display="block"/.test(block) || /display="true"/.test(block);
  const ann = block.match(/<annotation[^>]*>([\s\S]*?)<\/annotation>/);
  if (!ann) return block;                            // no TeX source: leave untouched
  const texStr = ann[1]
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&').trim();
  try {
    const node = doc.convert(texStr, { display: display, em: 16, ex: 8, containerWidth: 500 });
    // Greedy match: MathJax nests inner <svg> for stretchy delimiters.
    let svgStr = adaptor.outerHTML(node).match(/<svg[\s\S]*<\/svg>/)[0];
    // Never add a second xmlns — MathJax already emits it.
    if (!svgStr.includes('xmlns="http://www.w3.org/2000/svg"')) {
      svgStr = svgStr.replace('<svg ', '<svg xmlns="http://www.w3.org/2000/svg" ');
    }
    // Accessible label from the LaTeX source (screen readers + search).
    // Must be XML-escaped: & first, then <, >, ".
    const label = texStr
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').slice(0, 200);
    svgStr = svgStr.replace('<svg ', '<svg aria-label="' + label + '" ');
    count++;
    // <span style="display:block"> NOT <div>: a <div> inside <p> is invalid XHTML.
    return display
      ? `<span class="math-display" style="display:block;text-align:center;margin:0.8em 0;">${svgStr}</span>`
      : `<span class="math-inline" style="white-space:nowrap;">${svgStr}</span>`;
  } catch (e) {
    console.error('  ! failed: ' + texStr.slice(0, 60) + ' (' + e.message + ')');
    return block;
  }
});

fs.writeFileSync(file, html);
console.log('rendered ' + count + ' equations -> ' + file);"""


class MakePdfBookState(Enum):
    """Every state this workflow can be in."""

    TOC = auto()
    RESEARCH = auto()
    ASSETS = auto()
    CHAPTER = auto()
    FRONT_MATTER = auto()
    BACK_MATTER = auto()
    COMPILE = auto()
    COMPLETE = auto()  # terminal
    FAILED = auto()  # terminal

    @property
    def is_terminal(self) -> bool:
        """True when no further phase should run."""
        return self in (MakePdfBookState.COMPLETE, MakePdfBookState.FAILED)


@dataclasses.dataclass
class ChapterInfo:
    """One chapter planned by the TOC phase and written by a chapter phase."""

    index: int
    title: str = ""
    outline: str = ""
    file_path: str = ""
    word_count: int = 0
    status: str = "pending"  # "pending" | "written"
    assets: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class MakePdfBookContext:
    """Data carried across every phase of one run."""

    intent: str
    run_id: str = ""
    state: MakePdfBookState = MakePdfBookState.TOC
    phase_iteration: int = 0
    # Session memory is injected by the session and deliberately excluded from
    # the checkpoint payload. The restore hook reattaches the supplied object.
    shared_memory: ShortTermMemory | None = dataclasses.field(default=None, repr=False)
    title: str = ""
    author: str = ""
    audience: str = ""
    technical_level: str = ""
    prerequisites: str = ""
    output_dir: str = ""
    content_types: list[str] = dataclasses.field(
        default_factory=lambda: ["images", "equations", "code", "tables"]
    )
    assets_dir: str = ""
    images: list[str] = dataclasses.field(default_factory=list)
    chapters: list[ChapterInfo] = dataclasses.field(default_factory=list)
    current_chapter_index: int = 0
    toc_summary: str = ""
    research_notes: dict[int, str] = dataclasses.field(default_factory=dict)
    research_sources: list[str] = dataclasses.field(default_factory=list)
    research_summary: str = ""
    pdf_path: str = ""
    compile_summary: str = ""
    front_matter_summary: str = ""
    front_matter_files: list[str] = dataclasses.field(default_factory=list)
    back_matter_summary: str = ""
    back_matter_files: list[str] = dataclasses.field(default_factory=list)
    fail_reason: str = ""
    artifacts: dict[str, str] = dataclasses.field(default_factory=dict)


# ── phase tool factories ──────────────────────────────────────────────────────


def _make_toc_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the only tool that can end the toc phase."""
    from lauren_ai._tools import tool

    @tool()
    async def submit_toc(
        title: str,
        author: str,
        chapters: list[dict[str, str]],
        audience: str = "",
        technical_level: str = "advanced",
        prerequisites: str = "",
        output_dir: str = "",
        content_types: list[str] | None = None,
    ) -> dict[str, object]:
        """Record the book plan (table of contents) and advance to the chapters.

        Args:
            title: The book title.
            author: The author name (defaults to "Anonymous" if unknown).
            chapters: List of chapters, each {"title": str, "outline": str}.
                One chapter phase will run per entry, so list ALL chapters.
            audience: The specialised readership (e.g. "embedded systems
                engineers", "NLP researchers", "quantitative traders").
            technical_level: Depth target — "intermediate", "advanced", or
                "expert".
            prerequisites: Knowledge the reader is assumed to have.
            output_dir: Directory to write chapters and the PDF into
                (defaults to a folder derived from the title).
            content_types: Rich content the book should include — any subset of
                "images", "equations", "code", "tables" (default: all four).
        """
        if not title.strip():
            return {
                "ok": False,
                "error": "title must not be empty.",
                "fix": "Call submit_toc() with a book title.",
            }
        if not chapters:
            return {
                "ok": False,
                "error": "chapters must not be empty.",
                "fix": "Provide at least one chapter with a title and outline.",
            }
        if technical_level not in ("intermediate", "advanced", "expert"):
            return {
                "ok": False,
                "error": f"Unsupported technical_level '{technical_level}'.",
                "fix": "Use 'intermediate', 'advanced', or 'expert'.",
            }
        valid_types: tuple[str, ...] = ("images", "equations", "code", "tables")
        types: list[str] = list(content_types or list(valid_types))
        bad_types: list[str] = [t for t in types if t not in valid_types]
        if bad_types:
            return {
                "ok": False,
                "error": f"Unsupported content_types: {bad_types}.",
                "fix": f"Use only: {', '.join(valid_types)}.",
            }
        cleaned: list[dict[str, str]] = []
        for raw in chapters:
            if not isinstance(raw, dict):
                continue
            c_title = str(raw.get("title", "")).strip()
            c_outline = str(raw.get("outline", "")).strip()
            if not c_title:
                continue
            cleaned.append({"title": c_title, "outline": c_outline})
        if not cleaned:
            return {
                "ok": False,
                "error": "No valid chapters were supplied.",
                "fix": "Each chapter needs a non-empty 'title'; outline is recommended.",
            }
        data["title"] = title.strip()
        data["author"] = (author or "Anonymous").strip()
        data["audience"] = audience.strip()
        data["technical_level"] = technical_level
        data["prerequisites"] = prerequisites.strip()
        data["chapters"] = cleaned
        data["output_dir"] = output_dir.strip()
        data["content_types"] = types
        event.set()
        return {
            "ok": True,
            "message": (
                f"Table of contents recorded: '{title}' with {len(cleaned)} chapters "
                f"(level: {technical_level}, content: {', '.join(types)}). "
                "One chapter phase will run per chapter."
            ),
        }

    return [submit_toc]


def _make_research_tools(
    event: asyncio.Event,
    data: dict[str, object],
    chapter_count: int,
) -> list[Callable[..., object]]:
    """Return the only tool that can end the research phase."""
    from lauren_ai._tools import tool

    @tool()
    async def submit_research(
        notes: list[dict[str, object]],
        sources: list[str],
        summary: str,
    ) -> dict[str, object]:
        """Record extensive research for every chapter and advance to writing.

        Args:
            notes: One entry per chapter, each {"chapter_index": int, "notes": str}.
                Must cover EVERY chapter (indices 0..N-1).
            sources: List of sources consulted (URLs, titles, references).
            summary: A concise overview of the research gathered.
        """
        if not notes:
            return {
                "ok": False,
                "error": "notes must not be empty.",
                "fix": "Provide research notes for every chapter.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Summarise the research you gathered.",
            }
        covered: dict[int, str] = {}
        for raw in notes:
            if not isinstance(raw, dict):
                continue
            raw_index = raw.get("chapter_index")
            raw_notes = raw.get("notes")
            if not isinstance(raw_index, int) or not isinstance(raw_notes, str):
                continue
            if not raw_notes.strip():
                continue
            covered[raw_index] = raw_notes.strip()
        missing: list[int] = [i for i in range(chapter_count) if i not in covered]
        if missing:
            return {
                "ok": False,
                "error": f"Research is missing for chapter indices: {missing}.",
                "fix": "Provide detailed notes for EVERY chapter before advancing.",
            }
        data["notes"] = {str(k): v for k, v in covered.items()}
        data["sources"] = [str(s) for s in sources if str(s).strip()]
        data["summary"] = summary.strip()
        event.set()
        return {
            "ok": True,
            "message": (
                f"Research recorded: {len(covered)} chapters covered, "
                f"{len(sources)} sources. "
                "The chapter phases start next."
            ),
        }

    return [submit_research]


def _make_chapter_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends one chapter phase."""
    from lauren_ai._tools import tool

    @tool()
    async def confirm_chapter_complete(
        chapter_index: int,
        file_path: str,
        word_count: int,
        assets: list[str] | None = None,
    ) -> dict[str, object]:
        """Signal that the chapter at *chapter_index* has been written.

        Args:
            chapter_index: Zero-based index of the chapter that was written.
                Must match the chapter the phase was asked to write.
            file_path: Path to the written Markdown file.
            word_count: Approximate word count of the chapter.
            assets: Optional list of asset file paths the chapter uses
                (images, diagrams) — must exist under <output_dir>/assets.
        """
        if chapter_index < 0:
            return {
                "ok": False,
                "error": "chapter_index must be >= 0.",
                "fix": "Pass the zero-based index of the chapter you just wrote.",
            }
        if not file_path.strip():
            return {
                "ok": False,
                "error": "file_path must not be empty.",
                "fix": "Provide the path to the chapter file you wrote.",
            }
        data["chapter_index"] = int(chapter_index)
        data["file_path"] = file_path.strip()
        data["word_count"] = max(0, int(word_count))
        data["assets"] = [str(a) for a in (assets or []) if str(a).strip()]
        event.set()
        return {
            "ok": True,
            "message": f"Chapter {chapter_index} confirmed at '{file_path}'.",
        }

    return [confirm_chapter_complete]


def _make_assets_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the assets phase."""
    from lauren_ai._tools import tool

    @tool()
    async def confirm_assets_ready(assets: list[str]) -> dict[str, object]:
        """Signal that all figure/diagram/cover assets were produced.

        Args:
            assets: Full list of asset file paths (figures + cover image).
        """
        if not assets:
            return {
                "ok": False,
                "error": "assets must not be empty.",
                "fix": "Provide at least one produced asset file path.",
            }
        data["assets"] = [str(a) for a in assets if str(a).strip()]
        event.set()
        return {
            "ok": True,
            "message": f"{len(data['assets'])} assets confirmed. The chapter phases start next.",
        }

    return [confirm_assets_ready]


def _make_front_matter_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the front_matter phase."""
    from lauren_ai._tools import tool

    @tool()
    async def confirm_front_matter_ready(
        summary: str,
        files: list[str],
    ) -> dict[str, object]:
        """Signal that the cover, preface, and contents pages are built.

        Args:
            summary: What was created.
            files: The front-matter file paths.
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the front-matter pages you created.",
            }
        if not files:
            return {
                "ok": False,
                "error": "files must not be empty.",
                "fix": "List the cover/preface/contents files you created.",
            }
        data["summary"] = summary.strip()
        data["files"] = [str(f) for f in files if str(f).strip()]
        event.set()
        return {"ok": True, "message": "Front matter confirmed."}

    return [confirm_front_matter_ready]


def _make_back_matter_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the tool that ends the back_matter phase."""
    from lauren_ai._tools import tool

    @tool()
    async def confirm_back_matter_ready(
        summary: str,
        files: list[str],
    ) -> dict[str, object]:
        """Signal that the index page is built.

        Args:
            summary: What was created.
            files: The index file path(s).
        """
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the index page you created.",
            }
        if not files:
            return {
                "ok": False,
                "error": "files must not be empty.",
                "fix": "List the index file(s) you created.",
            }
        data["summary"] = summary.strip()
        data["files"] = [str(f) for f in files if str(f).strip()]
        event.set()
        return {"ok": True, "message": "Back matter confirmed."}

    return [confirm_back_matter_ready]


def _make_compile_tools(
    event: asyncio.Event,
    data: dict[str, object],
) -> list[Callable[..., object]]:
    """Return the pass/fail decision tools for the compile phase."""
    from lauren_ai._tools import tool

    @tool()
    async def mark_book_complete(
        pdf_path: str,
        summary: str,
    ) -> dict[str, object]:
        """Signal that the PDF was compiled and validated.

        Args:
            pdf_path: Path to the finished .pdf file.
            summary: Summary of the book and how to open the PDF.
        """
        if not pdf_path.strip() or not pdf_path.lower().endswith(".pdf"):
            return {
                "ok": False,
                "error": "pdf_path must point to a .pdf file.",
                "fix": "Provide the actual path of the compiled PDF.",
            }
        if not summary.strip():
            return {
                "ok": False,
                "error": "summary must not be empty.",
                "fix": "Describe the finished book and where the PDF lives.",
            }
        data["action"] = "complete"
        data["pdf_path"] = pdf_path.strip()
        data["summary"] = summary.strip()
        event.set()
        return {"ok": True, "message": "Book marked as complete."}

    @tool()
    async def reject_book(
        issue: str,
        chapter_index: int = -1,
    ) -> dict[str, object]:
        """Signal that the compile failed and optionally which chapter to fix.

        Args:
            issue: Description of what went wrong.
            chapter_index: Zero-based index of the chapter that needs rewriting,
                or -1 if the whole book must be revisited.
        """
        if not issue.strip():
            return {
                "ok": False,
                "error": "issue must not be empty.",
                "fix": "Describe what needs to be fixed.",
            }
        data["action"] = "reject"
        data["issue"] = issue.strip()
        data["chapter_index"] = int(chapter_index)
        event.set()
        return {
            "ok": True,
            "message": f"Book sent back for fixes: {issue}",
        }

    return [mark_book_complete, reject_book]


# ── runner ────────────────────────────────────────────────────────────────────

# Static phase names for status-bar and event payloads.
_PHASE_NAMES: tuple[str, ...] = (
    "toc",
    "research",
    "assets",
    "chapter",
    "front_matter",
    "back_matter",
    "compile",
)


class MakePdfBookRunner(CodePlanRunner):
    """State-machine runner for make_pdf_book.

    Subclasses ``CodePlanRunner`` purely to inherit its session wiring and the
    public ``run_phase()`` helper. ``super().run()`` is never called, so none of
    code_plan's own phases execute — this runner owns the whole flow.

    The ``RESEARCH`` state runs once after the TOC; the ``CHAPTER`` state is
    re-entered once per chapter; ``total_phases`` becomes ``len(chapters) + 6``
    as soon as the TOC reveals the chapter count.
    """

    workflow_name = "make_pdf_book"
    total_phases = 7

    async def run(self, intent: str) -> MakePdfBookContext:
        """Drive toc → research → chapter×N → compile."""
        from lauren_ai._memory import ShortTermMemory

        handle = self._cfg.workflow_handle
        run_id = handle.run_id if handle is not None else uuid.uuid4().hex
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        )
        ctx = MakePdfBookContext(
            intent=intent,
            run_id=run_id,
            state=MakePdfBookState.TOC,
            shared_memory=memory,
        )
        if handle is not None:
            handle.attach_context(ctx)
        state = ctx.state

        while not state.is_terminal:
            ctx.state = state
            ctx.phase_iteration += 1
            if handle is not None:
                handle.attach_context(ctx)
                handle.update_phase(
                    state.name.lower(),
                    self._phase_index(state, ctx),
                    ctx.phase_iteration,
                )
            match state:
                case MakePdfBookState.TOC:
                    state = await self._toc(ctx, memory)
                case MakePdfBookState.RESEARCH:
                    state = await self._research(ctx, memory)
                case MakePdfBookState.ASSETS:
                    state = await self._assets(ctx, memory)
                case MakePdfBookState.CHAPTER:
                    state = await self._chapter(ctx, memory)
                case MakePdfBookState.FRONT_MATTER:
                    state = await self._front_matter(ctx, memory)
                case MakePdfBookState.BACK_MATTER:
                    state = await self._back_matter(ctx, memory)
                case MakePdfBookState.COMPILE:
                    state = await self._compile(ctx, memory)
            if handle is not None:
                ctx.state = state
                handle.attach_context(ctx)
                handle.persist_context_transition()
            log.info("make_pdf_book → %s", state.name)

        ctx.state = state
        if handle is not None:
            handle.attach_context(ctx)
        return ctx

    async def resume(self, context: object) -> MakePdfBookContext:
        """Resume the saved state with the session's existing conversation."""
        from lauren_ai._memory import ShortTermMemory

        if not isinstance(context, MakePdfBookContext):
            raise TypeError("make_pdf_book resume requires MakePdfBookContext")
        memory = (
            self._cfg.session_memory
            if self._cfg.session_memory is not None
            else context.shared_memory
        )
        if memory is None:
            memory = ShortTermMemory(max_tokens=self._cfg.cfg.execution.effective_usable_budget())
        context.shared_memory = memory
        if len(context.chapters) > 0:
            self.total_phases = len(context.chapters) + 6
        handle = self._cfg.workflow_handle
        if handle is not None:
            handle.attach_context(context)
        state = context.state
        while not state.is_terminal:
            context.state = state
            context.phase_iteration += 1
            if handle is not None:
                handle.attach_context(context)
                handle.update_phase(
                    state.name.lower(),
                    self._phase_index(state, context),
                    context.phase_iteration,
                )
            match state:
                case MakePdfBookState.TOC:
                    state = await self._toc(context, memory)
                case MakePdfBookState.RESEARCH:
                    state = await self._research(context, memory)
                case MakePdfBookState.ASSETS:
                    state = await self._assets(context, memory)
                case MakePdfBookState.CHAPTER:
                    state = await self._chapter(context, memory)
                case MakePdfBookState.FRONT_MATTER:
                    state = await self._front_matter(context, memory)
                case MakePdfBookState.BACK_MATTER:
                    state = await self._back_matter(context, memory)
                case MakePdfBookState.COMPILE:
                    state = await self._compile(context, memory)
            if handle is not None:
                context.state = state
                handle.attach_context(context)
                handle.persist_context_transition()
        context.state = state
        if handle is not None:
            handle.attach_context(context)
        return context

    @staticmethod
    def _phase_index(state: MakePdfBookState, ctx: MakePdfBookContext) -> int:
        """Return the dynamic status-bar position for *state*.

        toc=0, research=1, assets=2, chapter=3+index, front_matter=N+3,
        back_matter=N+4, compile=N+5. With N chapters the total is N+6.
        """
        n: int = len(ctx.chapters)
        if state is MakePdfBookState.TOC:
            return 0
        if state is MakePdfBookState.RESEARCH:
            return 1
        if state is MakePdfBookState.ASSETS:
            return 2
        if state is MakePdfBookState.CHAPTER:
            return 3 + ctx.current_chapter_index
        if state is MakePdfBookState.FRONT_MATTER:
            return n + 3
        if state is MakePdfBookState.BACK_MATTER:
            return n + 4
        if state is MakePdfBookState.COMPILE:
            return n + 5
        return 0

    async def _toc(
        self,
        ctx: MakePdfBookContext,
        memory: object,
    ) -> MakePdfBookState:
        """Loop until submit_toc fires; return RESEARCH or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    ctx.intent
                    if attempt == 1
                    else (
                        "Call submit_toc(title, author, chapters, audience, "
                        "technical_level, prerequisites, output_dir, content_types) now."
                    )
                ),
                system_prompt=(
                    "You are in the TOC phase of make_pdf_book. Your job is to plan the "
                    "table of contents for a SPECIALISED, TECHNICAL book from the user's "
                    "intent. This is not a general-audience title — it is written for "
                    "practitioners who already know their field.\n\n"
                    "Steps:\n"
                    "1. Understand the technical subject and the specialist readership the "
                    "user is targeting (e.g. 'systems programmers', 'ML engineers', "
                    "'structural engineers', 'clinical researchers').\n"
                    "2. Decide the book title — precise and descriptive (Title Case).\n"
                    "3. Decide the author name (use the user's name if known, else 'Anonymous').\n"
                    "4. Define the audience: the exact specialist group the book serves.\n"
                    "5. Choose a technical_level: 'intermediate', 'advanced', or 'expert' — "
                    "be honest about depth; expert books assume deep prior knowledge.\n"
                    "6. Define prerequisites: the specific knowledge/experience the reader "
                    "is assumed to have (e.g. 'fluent in Rust, familiarity with async "
                    "runtimes, basic OS concepts').\n"
                    "7. Determine how many chapters the user asked for — honour the "
                    "requested number exactly. If unspecified, choose a sensible number "
                    "(4–10) that covers the technical scope.\n"
                    "8. For EACH chapter, write:\n"
                    "   - title: a precise, technical Title Case name\n"
                    "   - outline: 3–5 bullet lines naming the SPECIFIC technical concepts, "
                    "methods, data, and worked examples the chapter will cover — avoid vague "
                    "language ('explore', 'learn about'); prefer concrete items ('derive the "
                    "Kalman update equations', 'implement a lock-free queue in Rust')\n"
                    "9. Decide which RICH CONTENT the book needs (content_types — any subset "
                    "of 'images', 'equations', 'code', 'tables'):\n"
                    "   - images: figures, diagrams, screenshots, charts — plan which "
                    "chapters need figures and what each figure shows\n"
                    "   - equations: mathematical formulas in LaTeX ($...$ / $$...$$) — "
                    "plan which derivations or formulas appear where\n"
                    "   - code: executable code snippets with language tags — plan which "
                    "chapters include code and what it demonstrates\n"
                    "   - tables: data tables, comparison matrices, reference values — "
                    "plan which chapters carry tables\n"
                    "   Default (user unspecified): all four.\n"
                    "10. Pick an output_dir (a folder derived from the book title); use the "
                    "11. Plan the book's FRONT AND BACK MATTER so the finished book looks beautiful and professional — every book gets all of these:\n"
                    "   - COVER: a designed cover page — title, author, and (if 'images' is enabled) a cover image; plan a cover theme (colours, font) matching the subject.\n"
                    "   - PREFACE: a short preface page (why this book, who it is for, what the reader will learn) placed before chapter 1.\n"
                    "   - TABLE OF CONTENTS: a visible contents page listing every chapter and its sections, placed after the preface.\n"
                    "   - INDEX: an index section at the end listing the key terms of the book (term -> chapter/section), so the book is navigable.\n"
                    "   - COLOURED HEADINGS: headings must be coloured (a consistent accent colour for h1/h2), not plain black — plan the accent colour here (e.g. a deep blue fitting the subject).\n"
                    "user's directory if given.\n\n"
                    "Call submit_toc(title, author, chapters, audience, technical_level, "
                    "prerequisites, output_dir, content_types) where chapters is a list of "
                    "{'title': ..., 'outline': ...} — one entry per chapter. Every entry "
                    "becomes one chapter phase, so include ALL chapters now.\n\n"
                    "Do NOT write any chapter content yet. This phase plans the structure only."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                max_turns=10,
                shared_memory=memory,
                tools=_make_toc_tools(event, data),
            )
            if event.is_set():
                raw_chapters = data.get("chapters", [])
                chapters: list[ChapterInfo] = []
                for index, raw in enumerate(raw_chapters):
                    if not isinstance(raw, dict):
                        continue
                    chapters.append(
                        ChapterInfo(
                            index=index,
                            title=str(raw.get("title", "")).strip(),
                            outline=str(raw.get("outline", "")).strip(),
                        )
                    )
                ctx.title = str(data.get("title", ""))
                ctx.author = str(data.get("author", "Anonymous"))
                ctx.audience = str(data.get("audience", ""))
                ctx.technical_level = str(data.get("technical_level", "advanced"))
                ctx.prerequisites = str(data.get("prerequisites", ""))
                ctx.output_dir = str(data.get("output_dir", ""))
                ctx.content_types = list(data.get("content_types", [])) or [
                    "images",
                    "equations",
                    "code",
                    "tables",
                ]
                ctx.assets_dir = f"{ctx.output_dir}/assets" if ctx.output_dir else "assets"
                ctx.chapters = chapters
                ctx.current_chapter_index = 0
                self.total_phases = len(chapters) + 6
                ctx.toc_summary = (
                    f"Title: {ctx.title}\nAuthor: {ctx.author}\n"
                    f"Audience: {ctx.audience or '(unspecified)'}\n"
                    f"Technical level: {ctx.technical_level}\n"
                    f"Prerequisites: {ctx.prerequisites or '(unspecified)'}\n"
                    f"Content: {', '.join(ctx.content_types)}\n"
                    f"Chapters: {len(chapters)}\n"
                    + "\n".join(f"  {i + 1}. {c.title}" for i, c in enumerate(chapters))
                )
                ctx.artifacts["toc"] = ctx.toc_summary
                return MakePdfBookState.RESEARCH

        ctx.fail_reason = "toc phase never called submit_toc()"
        return MakePdfBookState.FAILED

    async def _research(
        self,
        ctx: MakePdfBookContext,
        memory: object,
    ) -> MakePdfBookState:
        """Loop until submit_research fires; return CHAPTER or FAILED."""
        chapter_titles: str = "\n".join(
            f"  {i + 1}. {c.title} — {c.outline}" for i, c in enumerate(ctx.chapters)
        )
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    (
                        f"Research the technical book '{ctx.title}' extensively before writing.\n"
                        f"Chapters to research:\n{chapter_titles}"
                    )
                    if attempt == 1
                    else (
                        "Call submit_research(notes, sources, summary) now with notes "
                        "covering EVERY chapter."
                    )
                ),
                system_prompt=(
                    "You are in the RESEARCH phase of make_pdf_book. Gather extensive, "
                    "AUTHORITATIVE, TECHNICAL research before any chapter is written. "
                    "This book is specialised — vague general knowledge is not enough.\n\n"
                    f"Book: {ctx.title}\nAuthor: {ctx.author}\n"
                    f"Audience: {ctx.audience or '(unspecified)'}\n"
                    f"Technical level: {ctx.technical_level}\n"
                    f"Prerequisites: {ctx.prerequisites or '(unspecified)'}\n"
                    f"Rich content: {', '.join(ctx.content_types)}\n"
                    f"Chapters:\n{chapter_titles}\n\n"
                    "Requirements:\n"
                    "1. Research EVERY chapter topic to technical depth. For each chapter "
                    "gather:\n"
                    "   - Precise definitions of the core concepts and terminology\n"
                    "   - The specific methods, algorithms, formulas, or procedures involved\n"
                    "   - Key data: measurements, benchmarks, specifications, constants, "
                    "versions, dates\n"
                    "   - Concrete worked examples or case studies the chapter can use\n"
                    "   - Known limitations, edge cases, and pitfalls in the field\n"
                    "   - Common misconceptions to explicitly correct\n"
                    "2. Use the network/search tools to consult PRIMARY and authoritative "
                    "technical sources: peer-reviewed papers, standards documents (RFCs, "
                    "ISO/EN specs), official documentation, textbooks, and reputable "
                    "technical references. Cross-check figures across sources.\n"
                    "3. Record the exact sources used (URLs, DOIs, standard numbers, titles) "
                    "so chapters can cite them.\n"
                    "4. Keep notes dense and organised — one block per chapter, with data "
                    "and specifics the writer can quote directly.\n"
                    "5. INDEX TERMS — for EACH chapter, note the 10-20 key indexable "
                    "terms (concepts, names, methods) the index section will list; record "
                    "them explicitly in that chapter's notes. Also record the exact LaTeX "
                    "for key formulas, accurate runnable code snippets (with language tags "
                    "and expected output), and the data for reference tables — all in "
                    "the chapter notes. (Visual assets are produced in a separate ASSETS "
                    "phase after research, so do NOT generate images here.)\n\n"
                    "Call submit_research(notes, sources, summary) where:\n"
                    "  - notes: list of {'chapter_index': int, 'notes': str} — ONE entry "
                    "per chapter, indices 0..N-1\n"
                    "  - sources: list of source URLs/DOIs/standard numbers consulted\n"
                    "  - summary: a short overview of the research\n\n"
                    "Do NOT write any chapter content. This phase gathers material only."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                mode="Yolo",
                max_turns=20,
                shared_memory=memory,
                tools=_make_research_tools(event, data, len(ctx.chapters)),
            )
            if event.is_set():
                raw_notes = data.get("notes", {})
                ctx.research_notes = {
                    int(key): str(value)
                    for key, value in raw_notes.items()
                    if isinstance(value, str)
                }
                ctx.research_sources = list(data.get("sources", []))
                ctx.research_summary = str(data.get("summary", ""))
                ctx.images = [str(a) for a in data.get("assets", [])]
                ctx.artifacts["research"] = (
                    f"{len(ctx.research_notes)} chapters researched; "
                    f"{len(ctx.research_sources)} sources; {len(ctx.images)} assets"
                )
                return MakePdfBookState.ASSETS

        ctx.fail_reason = "research phase never called submit_research()"
        return MakePdfBookState.FAILED

    async def _chapter(
        self,
        ctx: MakePdfBookContext,
        memory: object,
    ) -> MakePdfBookState:
        """Write one chapter; loop until confirm_chapter_complete fires.

        Returns CHAPTER (next chapter) while chapters remain, else COMPILE.
        """
        index: int = ctx.current_chapter_index
        if index >= len(ctx.chapters):
            return MakePdfBookState.FRONT_MATTER
        chapter: ChapterInfo = ctx.chapters[index]
        research_block: str = ctx.research_notes.get(index, "")
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            text: str = (
                f"Write chapter {index + 1} of {len(ctx.chapters)}: '{chapter.title}'.\n"
                f"Outline:\n{chapter.outline}\n"
                + (
                    f"\nResearch notes for this chapter:\n{research_block}"
                    if research_block
                    else ""
                )
                if attempt == 1
                else (
                    "Call confirm_chapter_complete(chapter_index, file_path, word_count, "
                    "assets) now with the chapter you wrote."
                )
            )
            await self.run_phase(
                intent=ctx.intent,
                text=text,
                system_prompt=(
                    "You are in the CHAPTER phase of make_pdf_book. One phase runs per "
                    "chapter — write exactly ONE technical chapter now, in full.\n\n"
                    f"Book: {ctx.title}\nAuthor: {ctx.author}\n"
                    f"Audience: {ctx.audience or '(specialist reader)'}\n"
                    f"Technical level: {ctx.technical_level}\n"
                    f"Prerequisites: {ctx.prerequisites or '(assume domain knowledge)'}\n"
                    f"Rich content enabled: {', '.join(ctx.content_types)}\n"
                    f"Assets dir: {ctx.assets_dir or '(not set)'}\n"
                    f"Chapter {index + 1} of {len(ctx.chapters)}: {chapter.title}\n\n"
                    "Research for this chapter (use it — ground the writing in these facts, "
                    "data, and sources):\n"
                    + (research_block if research_block else "  (none recorded)")
                    + "\n\n"
                    "TECHNICAL WRITING STANDARDS (this is a specialised book):\n"
                    "- Write FOR the specialist reader: assume the stated prerequisites; "
                    "do not pad with basics they already know.\n"
                    "- Use precise, correct terminology; define any term that could be "
                    "ambiguous on first use.\n"
                    "- Ground every claim in the research: cite the source inline where "
                    "appropriate (e.g. [RFC 8446], [ISO 9001:2015], or a bracketed "
                    "reference to the sources list). NEVER fabricate facts, numbers, or "
                    "citations not present in the research.\n"
                    "- Prefer dense, precise prose over vague generalities. Show, don't "
                    "tell: give the exact value, the exact step, the exact trade-off.\n"
                    "- Cover limitations, edge cases, and pitfalls honestly.\n\n"
                    "BEAUTY & STRUCTURE (the finished book must look professional):\n"
                    "- Use a single '# ' chapter title and '## ' section headings — the typesetter colours these automatically with the accent colour planned in the TOC; never style headings with raw HTML or inline colours.\n"
                    "- Mark every key indexable term in **bold** on first use — the index section collects these, so consistent marking makes the index accurate.\n"
                    "- Keep paragraphs, lists, and spacing clean; prefer short, scannable sections over dense walls of text.\n"
                    "RICH CONTENT — use every enabled content type where it adds value:\n"
                    + (
                        "- IMAGES: for every figure the outline/research calls for, place "
                        "an image under <output_dir>/assets/ (create the folder), then "
                        "reference it with Markdown: ![Figure caption](assets/fig-NN-"
                        "name.png). Keep the caption descriptive. Include at least one "
                        "figure per chapter when 'images' is enabled.\n"
                        if "images" in ctx.content_types
                        else ""
                    )
                    + (
                        "- EQUATIONS: write mathematics in LaTeX — $...$ for inline and "
                        "$$...$$ for display equations. At build time these are typeset "
                        "natively in the PDF (typst or LaTeX render them as selectable, "
                        "crisp math), so use proper LaTeX (\\frac, \\sum, \\int, \\sqrt, "
                        "...).\n"
                        if "equations" in ctx.content_types
                        else ""
                    )
                    + (
                        "- CODE: include fenced code blocks with a language tag (```python, "
                        "```rust, ```ts). Code must be accurate, runnable, and directly "
                        "relevant. Add a short comment or prose tie-in for each snippet. The "
                        "language tag enables pygments syntax highlighting in the compiled "
                        "book.\n"
                        if "code" in ctx.content_types
                        else ""
                    )
                    + (
                        "- TABLES: render reference data, comparisons, and parameters as "
                        "Markdown tables with a header row and aligned columns.\n"
                        if "tables" in ctx.content_types
                        else ""
                    )
                    + "\n"
                    "Requirements:\n"
                    "1. Create the chapters directory: <output_dir>/chapters (use "
                    "mkdir or the write tool's parent-directory creation).\n"
                    "2. Write the chapter to <output_dir>/chapters/NN-slug.md where NN is "
                    "the zero-padded chapter number (01, 02, ...) and slug is a kebab-case "
                    "version of the title.\n"
                    "3. Structure: a single '# ' title, '## ' section headings, paragraphs, "
                    "and where useful: bullet lists, numbered steps, fenced code blocks, "
                    "tables, images, and blockquotes.\n"
                    "4. Length: 2,000–4,000 words of substantive technical content for this "
                    "chapter (technical books run denser than general ones).\n"
                    "5. End with a short transition that sets up the next chapter.\n"
                    "6. Do NOT include the book title or author in the file — just the "
                    "chapter content. The PDF metadata handles title/author.\n\n"
                    "Write the file with the write tool, then call "
                    "confirm_chapter_complete(chapter_index=<index>, file_path=..., "
                    "word_count=..., assets=[...]) with the exact index you were assigned "
                    "and the list of asset files (images) the chapter references."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_chapter_tools(event, data),
            )
            if event.is_set():
                reported: int = int(data.get("chapter_index", -1))
                if reported != index:
                    # The agent confirmed the wrong chapter — keep this phase active.
                    continue
                chapter.file_path = str(data.get("file_path", ""))
                chapter.word_count = int(data.get("word_count", 0))
                chapter.status = "written"
                chapter.assets = [str(a) for a in data.get("assets", [])]
                for asset in chapter.assets:
                    if asset not in ctx.images:
                        ctx.images.append(asset)
                ctx.artifacts[f"chapter_{index}"] = (
                    f"{chapter.title}: {chapter.file_path} ({chapter.word_count} words, "
                    f"{len(chapter.assets)} assets)"
                )
                ctx.current_chapter_index = index + 1
                return MakePdfBookState.CHAPTER

        ctx.fail_reason = f"chapter {index + 1} never confirmed completion"
        return MakePdfBookState.FAILED

    async def _assets(
        self,
        ctx: MakePdfBookContext,
        memory: object,
    ) -> MakePdfBookState:
        """Produce all figure/asset files; loop until confirm_assets_ready fires."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Generate all figure, diagram, and cover assets for '{ctx.title}' "
                    f"into {ctx.assets_dir or '<output_dir>/assets'}."
                    if attempt == 1
                    else "Call confirm_assets_ready(assets) now with the full asset list."
                ),
                system_prompt=(
                    "You are in the ASSETS phase of make_pdf_book. Produce EVERY visual asset "
                    "the book needs, in one phase, before any chapter is written.\n\n"
                    f"Book: {ctx.title}\nAssets dir: {ctx.assets_dir or '<output_dir>/assets'}\n"
                    f"Rich content: {', '.join(ctx.content_types)}\n"
                    f"Chapter outlines:\n{ctx.toc_summary or '(see plan)'}\n\n"
                    "Create the assets directory if missing, then generate:\n"
                    "1. Every figure the chapters need (per the research notes and "
                    "outlines): charts and plots with matplotlib (Agg backend), "
                    "architecture/cloud diagrams with the 'diagrams' library "
                    "(https://github.com/mingrammer/diagrams; pip install diagrams, "
                    "needs Graphviz), or PIL for simple graphics. Name files clearly "
                    "like fig-02-schema.png, fig-03-architecture.png.\n"
                    "2. The COVER image: <output_dir>/assets/cover.png with the title, "
                    "author, and a clean theme background (matplotlib or PIL).\n"
                    "3. Keep every image bounded and PNG at a reasonable resolution.\n\n"
                    "Then call confirm_assets_ready(assets) with the FULL list of asset "
                    "file paths you produced (figures + cover). These are handed to the "
                    "chapter phases so they can reference them by name."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                mode="Yolo",
                max_turns=25,
                shared_memory=memory,
                tools=_make_assets_tools(event, data),
            )
            if event.is_set():
                ctx.images = [str(a) for a in data.get("assets", [])]
                ctx.artifacts["assets"] = f"{len(ctx.images)} assets produced"
                return MakePdfBookState.CHAPTER

        ctx.fail_reason = "assets phase never called confirm_assets_ready()"
        return MakePdfBookState.FAILED

    async def _front_matter(
        self,
        ctx: MakePdfBookContext,
        memory: object,
    ) -> MakePdfBookState:
        """Build cover, preface, and table-of-contents pages; return BACK_MATTER or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Build the front-matter pages (cover, preface, table of contents) "
                    f"for '{ctx.title}'."
                    if attempt == 1
                    else "Call confirm_front_matter_ready(summary, files) now."
                ),
                system_prompt=(
                    "You are in the FRONT_MATTER phase of make_pdf_book. Build the book's "
                    "front-matter pages so the finished book looks like a real published "
                    "book.\n\n"
                    f"Book: {ctx.title}\nAuthor: {ctx.author}\n"
                    f"Chapters: {ctx.toc_summary or '(see plan)'}\n\n"
                    "Create these pages (as Markdown in the project for PDF, or XHTML "
                    "items for EPUB):\n"
                    "1. COVER: title, author, and the cover image "
                    "(<output_dir>/assets/cover.png) if it exists.\n"
                    "2. PREFACE: a short preface (why this book, who it is for, what the "
                    "reader will learn).\n"
                    "3. TABLE OF CONTENTS: a visible contents page listing every chapter "
                    "and its sections.\n"
                    "Use a clean, consistent style. The compile phase will assemble "
                    "these in order: cover, preface, contents, then chapters.\n\n"
                    "Then call confirm_front_matter_ready(summary, files) with a summary "
                    "and the list of files you created."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                mode="Yolo",
                max_turns=15,
                shared_memory=memory,
                tools=_make_front_matter_tools(event, data),
            )
            if event.is_set():
                ctx.front_matter_summary = str(data.get("summary", ""))
                ctx.front_matter_files = list(data.get("files", []))
                ctx.artifacts["front_matter"] = ctx.front_matter_summary
                return MakePdfBookState.BACK_MATTER

        ctx.fail_reason = "front_matter phase never called confirm_front_matter_ready()"
        return MakePdfBookState.FAILED

    async def _back_matter(
        self,
        ctx: MakePdfBookContext,
        memory: object,
    ) -> MakePdfBookState:
        """Build the index page; return COMPILE or FAILED."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            await self.run_phase(
                intent=ctx.intent,
                text=(
                    f"Build the INDEX page for '{ctx.title}' from the bold-marked terms "
                    "in the chapters."
                    if attempt == 1
                    else "Call confirm_back_matter_ready(summary, files) now."
                ),
                system_prompt=(
                    "You are in the BACK_MATTER phase of make_pdf_book. Build the book's "
                    "index page.\n\n"
                    f"Book: {ctx.title}\nChapters: {ctx.toc_summary or '(see plan)'}\n\n"
                    "Read the chapter files and collect every **bold-marked** indexable "
                    "term (concepts, names, methods) plus the research-phase index-term "
                    "notes, then produce an INDEX page: an alphabetised list of terms "
                    "each pointing to its chapter (chapter number, or link to the "
                    "chapter XHTML for EPUB).\n\n"
                    "Then call confirm_back_matter_ready(summary, files) with a summary "
                    "and the index file(s) you created."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                mode="Yolo",
                max_turns=15,
                shared_memory=memory,
                tools=_make_back_matter_tools(event, data),
            )
            if event.is_set():
                ctx.back_matter_summary = str(data.get("summary", ""))
                ctx.back_matter_files = list(data.get("files", []))
                ctx.artifacts["back_matter"] = ctx.back_matter_summary
                return MakePdfBookState.COMPILE

        ctx.fail_reason = "back_matter phase never called confirm_back_matter_ready()"
        return MakePdfBookState.FAILED

    async def _compile(
        self,
        ctx: MakePdfBookContext,
        memory: object,
    ) -> MakePdfBookState:
        """Typeset the PDF; loop until a verdict tool fires."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            event: asyncio.Event = asyncio.Event()
            data: dict[str, object] = {}
            written: list[ChapterInfo] = [c for c in ctx.chapters if c.status == "written"]
            paths: str = "\n".join(f"  {c.file_path}" for c in written)
            asset_list: str = "\n".join(f"  {a}" for a in ctx.images)
            text: str = (
                f"Compile the finished book into a PDF.\n\n"
                f"Title: {ctx.title}\nAuthor: {ctx.author}\n"
                f"Output dir: {ctx.output_dir or '(derived from title)'}\n"
                f"Rich content: {', '.join(ctx.content_types)}\n"
                f"Chapter files:\n{paths}\n"
                + (
                    "\nFront matter (cover/preface/contents):\n"
                    + "\n".join(f"  {f}" for f in ctx.front_matter_files)
                    + "\nBack matter (index):\n"
                    + "\n".join(f"  {f}" for f in ctx.back_matter_files)
                    if ctx.front_matter_files or ctx.back_matter_files
                    else ""
                )
                + (f"Asset files:\n{asset_list}" if ctx.images else "")
                if attempt == 1
                else "Call mark_book_complete(pdf_path, summary) or reject_book(issue, chapter_index) now."
            )
            await self.run_phase(
                intent=ctx.intent,
                text=text,
                system_prompt=(
                    "You are in the COMPILE phase of make_pdf_book. Typeset all written "
                    "chapters into a single polished PDF file, preserving every kind of "
                    "rich content.\n\n"
                    f"Title: {ctx.title}\nAuthor: {ctx.author}\n"
                    f"Rich content: {', '.join(ctx.content_types)}\n"
                    "Chapter files:\n" + "\n".join(f"  {c.file_path}" for c in written) + "\n\n"
                    "Asset files (images/figures the chapters reference):\n"
                    + ("\n".join(f"  {a}" for a in ctx.images) if ctx.images else "  (none)")
                    + "\n\n"
                    "BOOK POLISH — the finished PDF must look like a real published book. The front-matter pages (cover/preface/contents) and the index page were already built by the FRONT_MATTER and BACK_MATTER phases — use those files, do NOT rebuild them:\n"
                    "  FRONT MATTER: the cover/preface/contents Markdown files from the FRONT_MATTER phase (ctx.front_matter_files) go FIRST, before the chapter list, in this order: cover, preface, contents, chapters.\n"
                    "  BACK MATTER: append the index file from the BACK_MATTER phase (ctx.back_matter_files) after the last chapter.\n"
                    "  COLOURED HEADINGS:\n"
                    "   - TYPST tier: after the sed symbol fixes, prepend to book.typ a heading-colour rule and a clean page setup (adjust colours to the planned accent):\n"
                    '        #set page(paper: "a4", margin: 2.2cm)\n'
                    '        #show heading.where(level: 1): set text(fill: rgb("#1f4e79"), size: 20pt, weight: "bold")\n'
                    '        #show heading.where(level: 2): set text(fill: rgb("#2e74b5"), size: 15pt)\n'
                    "     Prepend with: sed -i '1i #set page(paper: \"a4\", margin: 2.2cm)' book.typ (repeat per rule).\n"
                    "   - XELATEX tier: write a header.tex with \\usepackage{xcolor} \\usepackage{sectsty}, \\definecolor{accent}{HTML}{1F4E79}, \\allsectionsfont{\\color{accent}}, and pass pandoc -H header.tex.\n"
                    "  TOC: the engine must emit a table of contents (pandoc --toc for typst/xelatex); the generated toc.md guarantees a visible one even if --toc is unavailable.\n"
                    "  INDEX: index.md lists the marked terms; for typst/xelatex use chapter numbers as references when page numbers are not known in advance.\n"
                    "  After building, verify: the extracted text contains 'Preface', 'Contents' and 'Index'; the cover title appears; and the typst source contains a '#show heading' colour rule (grep 'show heading' book.typ).\n"
                    "Compile strategy — try in order until one produces a valid PDF:\n"
                    "1. TYPST (preferred — native typeset math, single static binary, "
                    "no system install):\n"
                    "   a. If `typst --version` fails, download it (no root needed):\n"
                    "        curl -L https://github.com/typst/typst/releases/latest/"
                    "download/typst-x86_64-unknown-linux-musl.tar.xz -o /tmp/typst.tar.xz\n"
                    "        mkdir -p .math-render\n"
                    "        tar -xJf /tmp/typst.tar.xz -C .math-render --strip-components=1\n"
                    '        export PATH="$PWD/.math-render:$PATH"\n'
                    "   b. Create a small title page file <title>.txt:\n"
                    "        # <Title>\n        **<Author>**\n"
                    "   c. IMPORTANT — pandoc's typst writer emits symbol names that "
                    "older typst (0.15.x) rejects. Compile in TWO steps so you can patch "
                    "the intermediate .typ, and apply the FIXES below. Do NOT use "
                    "`--pdf-engine=typst` directly — it fails hard with no intermediate "
                    "to patch. Instead:\n"
                    "        pandoc <title>.txt chapters/*.md -t typst --toc "
                    "--toc-depth=2 --metadata title='<title>' --metadata author='<author>' "
                    "-o book.typ\n"
                    "        sed -i 's/planck\\.reduce/u{210f}/g; s/angle\\.l/⟨/g; "
                    "s/angle\\.r/⟩/g; s/times\\.circle/⊗/g' book.typ\n"
                    "        typst compile book.typ <title>.pdf\n"
                    "   d. WHY THESE FIXES (learned the hard way — typst ≤ 0.15 "
                    "rejects them all):\n"
                    "        - pandoc renders \\hbar as `planck.reduce` → unknown symbol "
                    "modifier; replace with `u{210f}` (the ℏ glyph).\n"
                    "        - \\langle / \\rangle become `angle.l` / `angle.r` → FAIL; "
                    "replace with literal ⟨ / ⟩.\n"
                    "        - \\otimes becomes `times.circle` → FAIL; replace with ⊗.\n"
                    "        - In the MARKDOWN (before pandoc): texmath cannot parse \\AA "
                    "(Ångström) — write \\text{Å} instead; and matplotlib "
                    "figure titles reject \\tfrac — use \\frac{1}{2}.\n"
                    "        After sed, grep -cE 'planck\\.reduce|angle\\.|times\\.circle' "
                    "book.typ must print 0 (counts inside ```python code blocks are fine "
                    "— only math outside code matters).\n"
                    "   e. pandoc converts the LaTeX math to native typst math "
                    "automatically (searchable, selectable text — better than images). "
                    "PNG figures, tables, and syntax-highlighted code all typeset "
                    "natively. Note: the shell expands chapters/*.md in the order given "
                    "— ensure it matches NN- ordering, or list the files explicitly "
                    "(pandoc does NOT sort them for you).\n"
                    "2. XELATEX (gold-standard LaTeX math; heavier install, needs root):\n"
                    "   a. apt-get install -y texlive-xetex texlive-fonts-recommended\n"
                    "   b. pandoc <title>.txt chapters/*.md -o <title>.pdf "
                    "--pdf-engine=xelatex --toc --metadata title='<title>' "
                    "--metadata author='<author>'\n"
                    "   Only use this tier if typst cannot be obtained. (xelatex handles "
                    "\\AA and \\hbar natively — no symbol fixes needed.)\n"
                    "3. CHROMIUM HEADLESS + MATHJAX SVG (works when no TeX/typst engine "
                    "can be installed):\n"
                    "   a. pandoc <title>.txt chapters/*.md -o book.epub --mathml "
                    "(same as the EPUB pipeline), extract it, and run the RENDERER "
                    "TEMPLATE below over every chapter XHTML to turn each <math> into a "
                    "self-contained SVG (glyphs inline, no fonts/JS/network).\n"
                    "   b. Concatenate the chapter bodies into ONE self-contained HTML "
                    "with a <style> block, inlining every PNG figure as base64 <img> "
                    "and every equation SVG directly.\n"
                    "   c. Install chromium via playwright if needed:\n"
                    "        pip install playwright && playwright install chromium\n"
                    "   d. Print to PDF with headless Chromium:\n"
                    "        python3 - <<'PY'\n"
                    "        from playwright.sync_api import sync_playwright\n"
                    "        with sync_playwright() as p:\n"
                    "            b = p.chromium.launch()\n"
                    "            pg = b.new_page()\n"
                    "            pg.goto('file://' + '<full path to book.html>')\n"
                    "            pg.pdf(path='<title>.pdf', format='A4', "
                    "print_background=True)\n"
                    "            b.close()\n"
                    "        PY\n"
                    "   Math prints as crisp vector SVG; text and code remain selectable.\n"
                    "4. REPORTLAB (absolute last resort — hand-rolled PDF):\n"
                    "   a. pip install reportlab\n"
                    "   b. Render each equation to a PNG via the MathJax node pipeline "
                    "(same RENDERER TEMPLATE outputting PNG or rasterized SVG), then "
                    "lay out title page, chapters (text, images, tables), and "
                    "equation PNGs manually with reportlab.platypus.\n\n"
                    "RICH CONTENT must survive — verify each enabled type in the PDF:\n"
                    "  - IMAGES: every figure referenced in a chapter appears in the PDF\n"
                    "  - EQUATIONS: math is present as real glyphs (typst/LaTeX typeset "
                    "it natively; the chromium fallback prints SVG) — NOT raw LaTeX "
                    "strings like \\frac or $...$\n"
                    "  - CODE: code blocks appear intact with their language styling "
                    "— every fenced block must carry a language tag (```python, ```rust) "
                    "so pandoc\u2019s highlighting engine (pygments themes; e.g. "
                    "--highlight-style pygments) styles it; pip install pygments if the "
                    "pygments theme is unavailable\n"
                    "  - TABLES: Markdown tables render as real tables\n"
                    "  - inline citations like [RFC 8446] remain visible\n\n"
                    "PDF VALIDATION — the file MUST pass all of these before you "
                    "confirm (mandatory):\n"
                    "  1. exists and starts with the '%PDF' magic bytes (head -c 4 "
                    "<file> == %PDF)\n"
                    "  2. page count >= 1 (aim 3+ for a real book): pip install pypdf "
                    '&& python3 -c "from pypdf import PdfReader; '
                    "print(len(PdfReader('<title>.pdf').pages))\"  (or pdfinfo)\n"
                    "  3. EVERY chapter title appears in the extracted text (so no "
                    "chapter was dropped). pdftotext is often NOT installed — use "
                    "pypdf's extract_text instead:\n"
                    "        python3 - <<'PY'\n"
                    "        from pypdf import PdfReader\n"
                    "        t = '\\n'.join((p.extract_text() or '') for p in "
                    "PdfReader('<title>.pdf').pages)\n"
                    "        for title in ['<Chapter 1 title>', ..., '<Chapter N "
                    "title>']:\n"
                    "            assert title in t, f'MISSING: {title}'\n"
                    "        print('all chapter titles present')\n"
                    "        PY\n"
                    "  4. no raw markup leaked: the extracted text must NOT contain "
                    "'<math', '<svg', '<table' or '\\frac' (those mean math/markup "
                    "was not rendered), and must NOT contain 'planck.reduce' or "
                    "'angle.' or 'times.circle' (those mean the typst symbol fixes "
                    "in step 1d were skipped).\n"
                    "  5. code and table text are present in the extracted text\n"
                    "  6. figures embedded: count image XObjects with pypdf — must "
                    "equal the number of figure files the chapters reference (8 in "
                    "the worked example):\n"
                    "        python3 - <<'PY'\n"
                    "        from pypdf import PdfReader\n"
                    "        r = PdfReader('<title>.pdf'); c = 0\n"
                    "        for p in r.pages:\n"
                    "          for n in p.get('/Resources', {}).get('/XObject', {}):\n"
                    "            if p['/Resources']['/XObject'][n].get_object()"
                    ".get('/Subtype') == '/Image': c += 1\n"
                    "        print('images:', c)\n"
                    "        PY\n"
                    "Output path convention: <output_dir>/<title-slug>.pdf (e.g. "
                    "Kalman-Filtering/Kalman-Filtering.pdf).\n\n"
                    "RENDERER TEMPLATE (only needed for tiers 3–4) — write this exactly "
                    "to .math-render/render-math.js and run with node:\n"
                    + _MATHJAX_RENDERER_JS
                    + "\n\n"
                    "On success call mark_book_complete(pdf_path, summary) with the path "
                    "and a summary. On failure call reject_book(issue, chapter_index) — "
                    "include the zero-based chapter_index that needs rewriting (or -1 to "
                    "revisit the whole book). You MUST call one of them."
                ),
                stable_system_prompt=CACHE_CONTRACT,
                mode="Yolo",
                max_turns=15,
                shared_memory=memory,
                tools=_make_compile_tools(event, data),
            )
            if event.is_set():
                action: str = str(data.get("action", ""))
                if action == "complete":
                    ctx.pdf_path = str(data.get("pdf_path", ""))
                    ctx.compile_summary = str(data.get("summary", ""))
                    ctx.artifacts["pdf"] = ctx.pdf_path
                    return MakePdfBookState.COMPLETE
                ctx.fail_reason = str(data.get("issue", ""))
                bad_index: int = int(data.get("chapter_index", -1))
                if 0 <= bad_index < len(ctx.chapters):
                    ctx.current_chapter_index = bad_index
                    return MakePdfBookState.CHAPTER
                return MakePdfBookState.FAILED

        ctx.fail_reason = "compile phase never reported a verdict"
        return MakePdfBookState.FAILED


# ── plugin ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class MakePdfBookParams(WorkflowParams):
    """Per-phase model overrides read from [workflows.make_pdf_book]."""

    toc_model: str = ""
    research_model: str = ""
    chapter_model: str = ""
    compile_model: str = ""

    def get_phase_models(self) -> dict[str, str]:
        """Map phase name to configured model override."""
        return {
            "toc": self.toc_model,
            "research": self.research_model,
            "chapter": self.chapter_model,
            "compile": self.compile_model,
        }


class MakePdfBookWorkflow(WorkflowPlugin):
    """Write a specialised, technical book chapter by chapter and compile it into a typeset PDF."""

    name = "make_pdf_book"
    description = (
        "Write a specialised, technical book chapter by chapter (one phase per chapter, "
        "dynamic count) with images, equations, code, and tables, then compile it into "
        "a typeset PDF."
    )
    mode_bindings = []  # manual only — invoke with /workflow make_pdf_book

    # Static skeleton graph. The registry reads this at discovery time, so the
    # per-chapter count cannot be declared statically: the runner re-enters the
    # 'chapter' phase once per chapter and updates the TUI counter dynamically.
    phases = [
        PhaseSpec(
            name="toc",
            agent_type="auto",
            max_turns=10,
            next="research",
            on_reject="toc",
            system_prompt_override=(
                "You are in the TOC phase of make_pdf_book. Plan a specialised, technical "
                "book's table of contents (title, author, audience, technical level, "
                "prerequisites, chapter list with concrete outlines, rich content types), "
                "then call submit_toc()."
            ),
        ),
        PhaseSpec(
            name="research",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=20,
            next="assets",
            on_reject="research",
            system_prompt_override=(
                "You are in the RESEARCH phase of make_pdf_book. Gather extensive, "
                "authoritative technical research for EVERY chapter topic (primary "
                "sources, data, formulas, citations), then call submit_research()."
            ),
        ),
        PhaseSpec(
            name="assets",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=25,
            next="chapter",
            on_reject="assets",
            system_prompt_override=(
                "You are in the ASSETS phase of make_pdf_book. Produce EVERY visual asset "
                "the book needs (figures, architecture diagrams with the 'diagrams' "
                "library, and the cover image) into the assets dir, then call "
                "confirm_assets_ready()."
            ),
        ),
        PhaseSpec(
            name="chapter",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=25,
            next="chapter",  # self-loop: the runner advances the chapter index
            on_reject="chapter",
            system_prompt_override=(
                "You are in the CHAPTER phase of make_pdf_book. Write exactly ONE "
                "technical chapter in full as Markdown (precise terminology, data, "
                "formulas, code snippets, tables, images with captions, and inline "
                "citations grounded in the research), then call "
                "confirm_chapter_complete()."
            ),
        ),
        PhaseSpec(
            name="front_matter",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=15,
            next="back_matter",
            on_reject="front_matter",
            system_prompt_override=(
                "You are in the FRONT_MATTER phase of make_pdf_book. Build the cover page, "
                "preface, and table-of-contents page, then call "
                "confirm_front_matter_ready()."
            ),
        ),
        PhaseSpec(
            name="back_matter",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=15,
            next="compile",
            on_reject="back_matter",
            system_prompt_override=(
                "You are in the BACK_MATTER phase of make_pdf_book. Build the index page "
                "from the bold-marked terms, then call confirm_back_matter_ready()."
            ),
        ),
        PhaseSpec(
            name="compile",
            agent_type="auto",
            mode_override="Yolo",
            max_turns=15,
            next=None,  # terminal on success
            on_reject="chapter",  # re-enter at the offending chapter index
            system_prompt_override=(
                "You are in the COMPILE phase of make_pdf_book. Typeset all chapters "
                "into a valid .pdf (pandoc -t typst + sed symbol fixes + typst compile "
                "→ xelatex → chromium+MathJax → reportlab), preserve images, "
                "native math, code, and tables, validate it (%PDF, page count, "
                "pypdf chapter-title + no-leak + image-count checks), and call "
                "mark_book_complete() or reject_book()."
            ),
        ),
    ]

    @classmethod
    def checkpoint_context_to_payload(cls, context: object) -> dict[str, object]:
        """Encode resumable state without duplicating provider memory."""
        if not isinstance(context, MakePdfBookContext):
            raise TypeError("make_pdf_book checkpoint requires MakePdfBookContext")
        return {
            "intent": context.intent,
            "run_id": context.run_id,
            "state": context.state.name,
            "phase_iteration": context.phase_iteration,
            "title": context.title,
            "author": context.author,
            "audience": context.audience,
            "technical_level": context.technical_level,
            "prerequisites": context.prerequisites,
            "output_dir": context.output_dir,
            "content_types": context.content_types,
            "assets_dir": context.assets_dir,
            "images": context.images,
            "chapters": [
                {
                    "index": c.index,
                    "title": c.title,
                    "outline": c.outline,
                    "file_path": c.file_path,
                    "word_count": c.word_count,
                    "status": c.status,
                    "assets": c.assets,
                }
                for c in context.chapters
            ],
            "current_chapter_index": context.current_chapter_index,
            "toc_summary": context.toc_summary,
            "research_notes": context.research_notes,
            "research_sources": context.research_sources,
            "research_summary": context.research_summary,
            "front_matter_summary": context.front_matter_summary,
            "front_matter_files": context.front_matter_files,
            "back_matter_summary": context.back_matter_summary,
            "back_matter_files": context.back_matter_files,
            "pdf_path": context.pdf_path,
            "compile_summary": context.compile_summary,
            "fail_reason": context.fail_reason,
            "artifacts": context.artifacts,
        }

    @classmethod
    def checkpoint_context_from_payload(
        cls,
        payload: dict[str, object],
        memory: object | None = None,
    ) -> MakePdfBookContext:
        """Restore state and attach the already-open session memory."""
        raw_state = str(payload.get("state", MakePdfBookState.TOC.name))
        try:
            state = MakePdfBookState[raw_state]
        except KeyError as exc:
            raise ValueError(f"unknown make_pdf_book state: {raw_state}") from exc
        raw_chapters = payload.get("chapters", [])
        chapters: list[ChapterInfo] = []
        if isinstance(raw_chapters, list):
            for raw in raw_chapters:
                if not isinstance(raw, dict):
                    continue
                chapters.append(
                    ChapterInfo(
                        index=int(raw.get("index", 0)),
                        title=str(raw.get("title", "")),
                        outline=str(raw.get("outline", "")),
                        file_path=str(raw.get("file_path", "")),
                        word_count=int(raw.get("word_count", 0)),
                        status=str(raw.get("status", "pending")),
                        assets=[
                            str(a)
                            for a in raw.get("assets", [])
                            if isinstance(raw.get("assets"), list)
                        ],
                    )
                )
        raw_artifacts = payload.get("artifacts", {})
        artifacts = (
            {str(key): str(value) for key, value in raw_artifacts.items()}
            if isinstance(raw_artifacts, dict)
            else {}
        )
        raw_research_notes = payload.get("research_notes", {})
        research_notes: dict[int, str] = {}
        if isinstance(raw_research_notes, dict):
            for key, value in raw_research_notes.items():
                try:
                    research_notes[int(key)] = str(value)
                except (TypeError, ValueError):
                    continue
        raw_research_sources = payload.get("research_sources", [])
        research_sources = (
            [str(s) for s in raw_research_sources] if isinstance(raw_research_sources, list) else []
        )
        return MakePdfBookContext(
            intent=str(payload.get("intent", "")),
            run_id=str(payload.get("run_id", "")),
            state=state,
            phase_iteration=int(payload.get("phase_iteration", 0)),
            title=str(payload.get("title", "")),
            author=str(payload.get("author", "")),
            audience=str(payload.get("audience", "")),
            technical_level=str(payload.get("technical_level", "advanced")),
            prerequisites=str(payload.get("prerequisites", "")),
            output_dir=str(payload.get("output_dir", "")),
            content_types=list(payload.get("content_types", []))
            or [
                "images",
                "equations",
                "code",
                "tables",
            ],
            assets_dir=str(payload.get("assets_dir", "")),
            images=[str(i) for i in payload.get("images", [])]
            if isinstance(payload.get("images", []), list)
            else [],
            chapters=chapters,
            current_chapter_index=int(payload.get("current_chapter_index", 0)),
            toc_summary=str(payload.get("toc_summary", "")),
            research_notes=research_notes,
            research_sources=research_sources,
            research_summary=str(payload.get("research_summary", "")),
            front_matter_summary=str(payload.get("front_matter_summary", "")),
            front_matter_files=[str(f) for f in payload.get("front_matter_files", [])]
            if isinstance(payload.get("front_matter_files", []), list)
            else [],
            back_matter_summary=str(payload.get("back_matter_summary", "")),
            back_matter_files=[str(f) for f in payload.get("back_matter_files", [])]
            if isinstance(payload.get("back_matter_files", []), list)
            else [],
            pdf_path=str(payload.get("pdf_path", "")),
            compile_summary=str(payload.get("compile_summary", "")),
            fail_reason=str(payload.get("fail_reason", "")),
            artifacts=artifacts,
            shared_memory=memory,
        )

    @classmethod
    def build_runner(
        cls,
        config: WorkflowConfig,
        mode_manager: ModeManager | None,
    ) -> MakePdfBookRunner:
        """Return this workflow's own state-machine runner."""
        return MakePdfBookRunner(config, mode_manager)

    @classmethod
    def build_params(cls, source: dict[str, object]) -> WorkflowParams:
        """Build typed params from [workflows.make_pdf_book]."""
        return MakePdfBookParams(
            toc_model=str(source.get("toc_model", "") or ""),
            research_model=str(source.get("research_model", "") or ""),
            chapter_model=str(source.get("chapter_model", "") or ""),
            compile_model=str(source.get("compile_model", "") or ""),
        )
