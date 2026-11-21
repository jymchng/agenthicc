"""The shared make_book prose voice reaches every phase that writes or edits prose."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from agenthicc.workflows.make_book import voice
from agenthicc.workflows.make_book.runner import (
    ChapterInfo,
    MakeBookContext,
    MakeBookRunner,
    MakeBookWorkflow,
)

pytestmark = pytest.mark.unit

_POOLS: dict[str, tuple[str, ...]] = {
    "COORDINATING_CONJUNCTIONS": voice.COORDINATING_CONJUNCTIONS,
    "SUBORDINATING_CONJUNCTIONS": voice.SUBORDINATING_CONJUNCTIONS,
    "CONJUNCTIVE_ADVERBS": voice.CONJUNCTIVE_ADVERBS,
    "CONNECTIVE_PHRASES": voice.CONNECTIVE_PHRASES,
    "CONVERSATIONAL_OPENERS": voice.CONVERSATIONAL_OPENERS,
    "EXPLANATORY_FILLERS": voice.EXPLANATORY_FILLERS,
    "REFLECTIVE_EXPRESSIONS": voice.REFLECTIVE_EXPRESSIONS,
    "QUALIFYING_EXPRESSIONS": voice.QUALIFYING_EXPRESSIONS,
    "NARRATIVE_TRANSITIONS": voice.NARRATIVE_TRANSITIONS,
    "SENTENCE_OPENINGS": voice.SENTENCE_OPENINGS,
    "SECOND_PERSON_CONSTRUCTIONS": voice.SECOND_PERSON_CONSTRUCTIONS,
    "READER_QUESTIONS": voice.READER_QUESTIONS,
}

#: Instructions that used to sit in the chapter prompt and push against the
#: connective, second-person voice. None may survive anywhere in a phase prompt.
_SUPERSEDED_PHRASES: tuple[str, ...] = (
    "WRITE LIKE A HUMAN",
    "NO AI tells",
    "any formulaic transition",
    "Prefer dense, precise prose",
    "short, scannable sections",
    "specialist colleague",
    "Open every section with a concrete hook",
    "technical books run denser",
    "End with a short transition",
    "never use mechanical transitions",
)


async def _capture(
    call: Callable[[MakeBookRunner], Awaitable[object]],
) -> list[str]:
    """Run one phase method with a recording ``run_phase`` and return its prompts.

    The recording turn never fires the transition event, so a phase exhausts its
    bounded retries; that is exactly what exposes every attempt's prompt.
    """

    prompts: list[str] = []

    async def record(**kwargs: object) -> None:
        prompts.append(str(kwargs["system_prompt"]))

    runner = object.__new__(MakeBookRunner)
    runner.run_phase = record  # type: ignore[method-assign]
    await call(runner)
    assert prompts, "the phase never reached run_phase"
    return prompts


def _context(tmp_path: Path) -> MakeBookContext:
    output = tmp_path / "book"
    return MakeBookContext(
        intent="write a technical book",
        title="Queues in Practice",
        output_dir=str(output),
        assets_dir=str(output / "assets"),
        research_dir=str(output / "research"),
        chapters=[
            ChapterInfo(index=0, title="Bounded Queues", outline="- backpressure"),
            ChapterInfo(index=1, title="Fair Scheduling", outline="- weighted round robin"),
        ],
    )


# ── the shared component itself ───────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(_POOLS))
def test_every_expression_pool_is_populated_and_free_of_duplicates(name: str) -> None:
    pool = _POOLS[name]
    assert pool, f"{name} is empty"
    assert all(entry.strip() == entry and entry for entry in pool)
    folded = [entry.casefold() for entry in pool]
    assert len(folded) == len(set(folded)), f"{name} repeats an entry"


def test_pools_cover_the_requested_connective_vocabulary() -> None:
    assert {"nor", "yet", "for"} <= set(voice.COORDINATING_CONJUNCTIONS)
    assert {"insofar as", "provided that", "even though"} <= set(voice.SUBORDINATING_CONJUNCTIONS)
    assert {"nevertheless", "conversely", "importantly"} <= set(voice.CONJUNCTIVE_ADVERBS)
    assert "what makes this interesting is that" in voice.CONNECTIVE_PHRASES
    assert "Consider, for a moment, ..." in voice.CONVERSATIONAL_OPENERS
    assert "What this means for you is ..." in voice.SECOND_PERSON_CONSTRUCTIONS
    assert "And why should you care about this distinction?" in voice.READER_QUESTIONS
    assert ("One must consider ...", "You need to consider ...") in voice.IMPERSONAL_TO_DIRECT


def test_prose_voice_renders_every_pool_entry() -> None:
    for name, pool in _POOLS.items():
        for entry in pool:
            assert entry in voice.PROSE_VOICE, f"{name} entry missing from the prompt: {entry!r}"
    for impersonal, direct in voice.IMPERSONAL_TO_DIRECT:
        assert impersonal in voice.PROSE_VOICE
        assert direct in voice.PROSE_VOICE


def test_prose_voice_opens_with_its_own_summary() -> None:
    assert voice.PROSE_VOICE_SUMMARY in voice.PROSE_VOICE
    assert '"you"' in voice.PROSE_VOICE_SUMMARY


@pytest.mark.parametrize(
    "required",
    [
        'ADDRESS THE READER DIRECTLY AS "YOU" (non-negotiable)',
        "never let more than two consecutive prose paragraphs pass",
        "Do NOT begin every sentence with",
        "Invite; do not presume",
        "CONNECTIVE PROSE",
        "CONVERSATIONAL FILLERS",
        "may each appear at most once or twice in a chapter",
        "GUARDRAILS AGAINST OVERCORRECTION",
        "Conversational language must never cost factual precision",
        "REVISION PASS",
        "never remove the second person",
        'do not put "you" in headings',
    ],
)
def test_prose_voice_states_each_core_rule(required: str) -> None:
    assert required in voice.PROSE_VOICE


def test_prose_preservation_protects_connectives_and_second_person() -> None:
    text = voice.PROSE_PRESERVATION
    assert '"you" and "your"' in text
    assert "conjunctions, transitions" in text
    assert "Do not rewrite, condense" in text


# ── authoring phases carry the full voice ─────────────────────────────────────


async def test_chapter_prompt_carries_the_voice_on_every_attempt(tmp_path: Path) -> None:
    context = _context(tmp_path)
    prompts = await _capture(lambda runner: runner._chapter(context, None))

    assert len(prompts) == 5
    for prompt in prompts:
        assert voice.PROSE_VOICE in prompt
        assert "REVISION PASS" in prompt
        assert "Close the chapter in the same connected, direct voice" in prompt


async def test_chapter_prompt_has_no_instruction_that_fights_the_voice(tmp_path: Path) -> None:
    context = _context(tmp_path)
    prompt = (await _capture(lambda runner: runner._chapter(context, None)))[0]

    for phrase in _SUPERSEDED_PHRASES:
        assert phrase not in prompt, f"superseded instruction survived: {phrase!r}"
    # The rigour rules the voice must coexist with are still present.
    assert "NEVER fabricate facts, numbers, or citations" in prompt
    assert "Mark every key indexable term in **bold**" in prompt
    assert "Every '## ' section subheading must be UNIQUE" in prompt


async def test_chapter_numbered_requirements_are_unique_and_sequential(tmp_path: Path) -> None:
    context = _context(tmp_path)
    prompt = (await _capture(lambda runner: runner._chapter(context, None)))[0]
    requirements = prompt[prompt.index("Requirements:") :]
    numbers = [
        int(line.split(".", 1)[0])
        for line in requirements.splitlines()
        if line[:1].isdigit() and line.split(".", 1)[0].isdigit()
    ]
    assert numbers == list(range(1, len(numbers) + 1))


async def test_front_matter_prompt_asks_for_a_second_person_preface(tmp_path: Path) -> None:
    context = _context(tmp_path)
    prompts = await _capture(lambda runner: runner._front_matter(context, None))

    for prompt in prompts:
        assert voice.PROSE_VOICE in prompt
        assert "speaking to the reader as 'you' from its first sentence" in prompt
        assert "contents.md" in prompt  # the structural contract is untouched
        for phrase in _SUPERSEDED_PHRASES:
            assert phrase not in prompt


# ── editing phases must not flatten the voice ─────────────────────────────────


@pytest.mark.parametrize("final", [False, True])
async def test_layout_review_prompts_preserve_the_prose(tmp_path: Path, final: bool) -> None:
    context = _context(tmp_path)
    prompts = await _capture(lambda runner: runner._layout_review(context, None, final=final))

    for prompt in prompts:
        assert voice.PROSE_PRESERVATION in prompt
        assert "6.175in" in prompt  # the layout contract is untouched
        assert voice.PROSE_VOICE not in prompt


async def test_compile_prompt_preserves_the_prose(tmp_path: Path) -> None:
    context = _context(tmp_path)
    prompts = await _capture(lambda runner: runner._compile(context, None))

    for prompt in prompts:
        assert voice.PROSE_PRESERVATION in prompt
        assert "create_build_book()" in prompt  # the build contract is untouched


# ── phases that do not write prose stay free of the voice ─────────────────────


@pytest.mark.parametrize(
    "phase",
    ["_toc", "_research", "_assets", "_back_matter"],
)
async def test_non_prose_phases_do_not_carry_the_voice(tmp_path: Path, phase: str) -> None:
    context = _context(tmp_path)
    prompts = await _capture(lambda runner: getattr(runner, phase)(context, None))

    for prompt in prompts:
        assert voice.PROSE_VOICE_SUMMARY not in prompt
        assert voice.PROSE_PRESERVATION not in prompt


# ── the declarative skeleton agrees with the live prompts ─────────────────────


def test_phase_spec_skeleton_embeds_the_shared_components() -> None:
    specs = {phase.name: phase.system_prompt_override for phase in MakeBookWorkflow.phases}

    assert voice.PROSE_VOICE_SUMMARY in specs["chapter"]
    assert voice.PROSE_VOICE_SUMMARY in specs["front_matter"]
    for name in ("layout_review", "final_layout_review", "compile"):
        assert voice.PROSE_PRESERVATION in specs[name], name
    for name in ("toc", "research", "assets", "back_matter"):
        assert voice.PROSE_VOICE_SUMMARY not in specs[name], name
    for text in specs.values():
        for phrase in _SUPERSEDED_PHRASES:
            assert phrase not in text, phrase
