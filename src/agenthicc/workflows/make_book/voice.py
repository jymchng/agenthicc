"""Shared prose-voice instructions for every ``make_book`` phase that touches prose.

The workflow has exactly two phases that *author* running prose (``chapter`` and
``front_matter``) and three that *edit* existing Markdown after it was written
(``layout_review``, ``final_layout_review`` and ``compile``).  Each of them
needs the same answer to "how should this book sound?", and each needs it
stated once: when several prompts carry slightly different copies of a style
rule, the strictest copy wins and the phases quietly disagree.  This module is
therefore the single source for that answer.

* :data:`PROSE_VOICE` is the full voice guide handed to the authoring phases.
* :data:`PROSE_VOICE_SUMMARY` is its opening paragraph; the declarative
  ``PhaseSpec`` skeleton in :mod:`agenthicc.workflows.make_book.runner` embeds it
  so the skeleton and the live prompts cannot drift apart.
* :data:`PROSE_PRESERVATION` is handed to the phases that edit existing files so
  a layout or build fix never flattens the voice back into terse, impersonal
  prose.

The expression pools are plain data.  They are rendered into the prompt text
below rather than pasted into it, so a pool can be extended in one place and the
tests can prove that the prompt really carries every entry.
"""

from __future__ import annotations

__all__ = [
    "CONJUNCTIVE_ADVERBS",
    "CONNECTIVE_PHRASES",
    "CONVERSATIONAL_OPENERS",
    "COORDINATING_CONJUNCTIONS",
    "EXPLANATORY_FILLERS",
    "IMPERSONAL_TO_DIRECT",
    "NARRATIVE_TRANSITIONS",
    "PROSE_PRESERVATION",
    "PROSE_VOICE",
    "PROSE_VOICE_SUMMARY",
    "QUALIFYING_EXPRESSIONS",
    "READER_QUESTIONS",
    "REFLECTIVE_EXPRESSIONS",
    "SECOND_PERSON_CONSTRUCTIONS",
    "SENTENCE_OPENINGS",
    "SUBORDINATING_CONJUNCTIONS",
]

# ── expression pools ──────────────────────────────────────────────────────────

COORDINATING_CONJUNCTIONS: tuple[str, ...] = (
    "and",
    "but",
    "or",
    "nor",
    "yet",
    "so",
    "for",
)

SUBORDINATING_CONJUNCTIONS: tuple[str, ...] = (
    "because",
    "although",
    "though",
    "even though",
    "while",
    "whereas",
    "since",
    "unless",
    "until",
    "whenever",
    "wherever",
    "whether",
    "if",
    "as",
    "before",
    "after",
    "once",
    "even if",
    "provided that",
    "given that",
    "considering that",
    "insofar as",
)

CONJUNCTIVE_ADVERBS: tuple[str, ...] = (
    "however",
    "nevertheless",
    "nonetheless",
    "consequently",
    "therefore",
    "accordingly",
    "meanwhile",
    "furthermore",
    "moreover",
    "instead",
    "rather",
    "subsequently",
    "ultimately",
    "indeed",
    "certainly",
    "naturally",
    "admittedly",
    "interestingly",
    "remarkably",
    "essentially",
    "particularly",
    "specifically",
    "conversely",
    "alternatively",
    "comparatively",
    "similarly",
    "likewise",
    "notably",
    "arguably",
    "apparently",
    "presumably",
    "effectively",
    "importantly",
)

CONNECTIVE_PHRASES: tuple[str, ...] = (
    "as a result",
    "in other words",
    "in fact",
    "after all",
    "at the same time",
    "on the other hand",
    "in this respect",
    "to some extent",
    "in many cases",
    "in practical terms",
    "for this reason",
    "by contrast",
    "in comparison",
    "more importantly",
    "more specifically",
    "to put it another way",
    "with that in mind",
    "that being said",
    "having said that",
    "in light of this",
    "in view of this",
    "in the context of",
    "from this perspective",
    "in the broader sense",
    "at least in principle",
    "as far as this is concerned",
    "this is particularly important because",
    "this is precisely where",
    "what makes this interesting is that",
)

CONVERSATIONAL_OPENERS: tuple[str, ...] = (
    "Now,",
    "Well,",
    "Of course,",
    "Naturally,",
    "Indeed,",
    "Interestingly,",
    "In fact,",
    "Admittedly,",
    "To be fair,",
    "Consider, for a moment, ...",
    "Think about it this way.",
    "Here is the interesting part.",
    "The important thing to understand is that ...",
    "What is worth considering is ...",
    "It is worth taking a closer look at ...",
    "This brings us to another important consideration.",
)

EXPLANATORY_FILLERS: tuple[str, ...] = (
    "Put simply,",
    "In other words,",
    "To put it differently,",
    "To put this into perspective,",
    "More precisely,",
    "To be more specific,",
    "What this essentially means is ...",
    "The point here is that ...",
    "The underlying idea is that ...",
    "What this tells us is that ...",
    "In practical terms,",
    "At its most basic level,",
    "In essence,",
    "To put it another way,",
)

REFLECTIVE_EXPRESSIONS: tuple[str, ...] = (
    "After all,",
    "As it turns out,",
    "Interestingly enough,",
    "Perhaps more importantly,",
    "More to the point,",
    "As one might expect,",
    "Not surprisingly,",
    "It is easy to overlook the fact that ...",
    "What is particularly interesting, however, is that ...",
    "There is, of course, another side to this.",
    "And this raises an important question.",
    "It is precisely this distinction that matters.",
    "This is where things become particularly interesting.",
)

QUALIFYING_EXPRESSIONS: tuple[str, ...] = (
    "To some extent,",
    "In many respects,",
    "In certain circumstances,",
    "At least in principle,",
    "More often than not,",
    "Generally speaking,",
    "In most instances,",
    "By and large,",
    "For the most part,",
    "In some cases,",
    "Depending on the circumstances,",
    "As a general rule,",
    "Strictly speaking,",
    "To be entirely accurate,",
    "In a broader sense,",
)

NARRATIVE_TRANSITIONS: tuple[str, ...] = (
    "With that in mind,",
    "Having established this,",
    "Against this background,",
    "Returning to the earlier point,",
    "This brings us back to ...",
    "Building on this idea,",
    "Taking this a step further,",
    "There is another dimension to consider.",
    "But that is only part of the story.",
    "And this is where the distinction becomes important.",
    "The implications, however, extend further.",
    "Yet there is more to consider.",
    "This leads naturally to another question.",
)

SENTENCE_OPENINGS: tuple[str, ...] = (
    "And, ...",
    "But, ...",
    "Yet, ...",
    "So, ...",
    "Of course, ...",
    "Interestingly, ...",
    "In fact, ...",
    "More importantly, ...",
    "At the same time, ...",
    "Given this, ...",
    "Against this background, ...",
    "In practical terms, ...",
    "What makes this particularly interesting is ...",
    "One might reasonably argue that ...",
    "It is worth remembering that ...",
    "There is, however, an important distinction.",
    "This is not necessarily because ...",
    "What becomes apparent, then, is ...",
    "The reason for this is relatively straightforward.",
)

SECOND_PERSON_CONSTRUCTIONS: tuple[str, ...] = (
    "When you consider ...",
    "If you look closely ...",
    "You may notice that ...",
    "You might wonder why ...",
    "What you need to understand is ...",
    "You can think of this as ...",
    "When you encounter ...",
    "As you begin to understand ...",
    "You may find that ...",
    "You should also consider ...",
    "It is important for you to recognize ...",
    "Once you understand this ...",
    "The reason you need to understand this is ...",
    "What this means for you is ...",
    "You can see why this matters when ...",
    "If you think about it ...",
    "As you will discover ...",
    "You may already be familiar with ...",
    "Perhaps you have experienced ...",
    "Consider what happens when you ...",
    "This becomes especially important when you ...",
    "The next time you encounter ...",
    "As you work through this process ...",
    "You will begin to see that ...",
    "You are likely to discover that ...",
)

READER_QUESTIONS: tuple[str, ...] = (
    "You might be wondering why this matters.",
    "But what does this actually mean for you?",
    "So, how should you interpret this?",
    "You may be asking yourself whether this applies in your situation.",
    "At this point, you might reasonably ask ...",
    "What happens when you put this into practice?",
    "How can you distinguish between these two possibilities?",
    "And why should you care about this distinction?",
    "Perhaps the more important question is what you should do next.",
)

#: ``(impersonal, direct)`` pairs shown to the writer as rewrite patterns.
IMPERSONAL_TO_DIRECT: tuple[tuple[str, str], ...] = (
    ("One must consider ...", "You need to consider ..."),
    ("It is important to understand ...", "It is important for you to understand ..."),
    ("Individuals may find ...", "You may find ..."),
    ("The reader should recognize ...", "You should recognize ..."),
    ("A person can observe ...", "You can observe ..."),
    ("This allows individuals to ...", "This allows you to ..."),
    ("The user is required to ...", "You need to ..."),
    ("It is possible to determine ...", "You can determine ..."),
    (
        "A decision-maker must evaluate ...",
        "When you make this decision, you need to evaluate ...",
    ),
)


def _pool(items: tuple[str, ...]) -> str:
    """Render one expression pool as a single ``|``-separated line.

    ``|`` is used instead of ``;`` because several pool entries are whole
    sentences ending in ``.`` or ``?``, which a semicolon list would blur.
    """

    return " | ".join(items)


def _rewrites(pairs: tuple[tuple[str, str], ...]) -> str:
    """Render ``(impersonal, direct)`` rewrite patterns, one per line."""

    return "\n".join(f'   "{before}" -> "{after}"' for before, after in pairs)


# ── the shared voice ──────────────────────────────────────────────────────────

PROSE_VOICE_SUMMARY: str = (
    "Write as an experienced, articulate human nonfiction author who is explaining the "
    "subject directly to an intelligent specialist: a continuous conversation WITH the "
    'reader, not a document ABOUT a subject. Address the reader as "you" and "your" '
    "throughout, link ideas with natural conjunctions, transitions and conversational "
    "connectives, develop each thought across compound and complex sentences, and let "
    "every paragraph grow out of the one before it. Never read like a chatbot delivering "
    "isolated answers, and never trade technical precision for conversational tone."
)

PROSE_VOICE: str = f"""WRITING VOICE (the most important instruction for how this prose sounds; every make_book writing and revision phase follows it):

{PROSE_VOICE_SUMMARY}

The author is comfortable developing complex ideas over extended passages: connecting observations, qualifying claims, exploring implications, and guiding the reader through the reasoning. The voice changes HOW the facts are explained, never WHICH facts are stated: every number, citation, definition and claim must still come from the research.

1. ADDRESS THE READER DIRECTLY AS "YOU" (non-negotiable).
- Speak to the reader with you / your / yourself / yours in EVERY section and in every kind of passage: opening explanations, conceptual and technical discussion, derivations, worked examples, comparisons, recommendations, transitions, and the closing of the chapter. Open each section by involving the reader, and never let more than two consecutive prose paragraphs pass without a natural "you" or "your".
- Make the reader an active participant. Frame ideas around what the reader decides, observes, measures, expects, runs, or risks, and help them think about the concept instead of merely defining it. Detached: "Backpressure prevents unbounded queue growth." Engaging: "When you add backpressure, you are deciding what should happen when the consumer cannot keep up, and that decision determines whether the pressure lands on the producer or on your memory."
- Prefer direct address over impersonal constructions, and never discuss the reader in the third person ("the reader", "the user", "one", "individuals") when "you" works:
{_rewrites(IMPERSONAL_TO_DIRECT)}
- Statements about how a system, algorithm, material or standard behaves stay statements about that thing; "you" frames the reader's reasoning and choices around the fact, and never bends the fact.
- Anticipate the reader's questions now and then, in the author's own words. Reader-question patterns: {_pool(READER_QUESTIONS)}
- Connect abstractions to situations the reader can recognize, so the idea lands before the formalism does.
- Invite; do not presume. Write "You may have run into a case where ..." or "If you have ever wondered why ...", never "You have undoubtedly experienced ..." or "You already know ...". Assume nothing about the reader's history beyond the stated prerequisites, and never talk down to them.
- Vary how "you" appears. Do NOT begin every sentence with "You", and do not insert "you" where it sounds forced. Direct-address patterns to vary: {_pool(SECOND_PERSON_CONSTRUCTIONS)}
- Direct address belongs in technical, scientific, mathematical and analytical chapters too. Adjust the warmth to the subject, but keep the address.

2. CONNECTIVE PROSE (raise the density of natural connection well above the default terse register).
Ideas must be linked, qualified, contrasted and developed, not stated and abandoned. Treat the pools below as a palette to draw from, not a checklist to tick off, and place connectives inside sentences as often as at their starts.
- Coordinating conjunctions: {_pool(COORDINATING_CONJUNCTIONS)}
- Subordinating conjunctions: {_pool(SUBORDINATING_CONJUNCTIONS)}
- Conjunctive and transitional adverbs: {_pool(CONJUNCTIVE_ADVERBS)}
- Multiword connectives: {_pool(CONNECTIVE_PHRASES)}
Sentences may begin with And, But, Yet, So or Or when that is how a person would say it. Do not make "every sentence needs a conjunction" a rule: raise the overall frequency while keeping the grammar correct, the text readable and the sentence shapes varied.

3. CONVERSATIONAL FILLERS AND GUIDING EXPRESSIONS (the small words that show an author thinking aloud).
- Conversational introductions: {_pool(CONVERSATIONAL_OPENERS)}
- Explanatory fillers: {_pool(EXPLANATORY_FILLERS)}
- Reflective expressions: {_pool(REFLECTIVE_EXPRESSIONS)}
- Qualifying expressions: {_pool(QUALIFYING_EXPRESSIONS)}
- Narrative transitions: {_pool(NARRATIVE_TRANSITIONS)}
These belong to the author's voice. They must not read as inserted SEO phrases or as a transition template repeated chapter after chapter.

4. SENTENCE SHAPE AND RHYTHM.
- Prefer developed sentences. Compound sentences join related clauses ("The tooling has become far more accessible, and teams now adopt it without the investment it once demanded."); complex sentences carry subordinate clauses ("Although the first setup looks heavy, the cost falls quickly once the pipeline is automated, provided the inputs stay consistent."). When an idea is introduced, develop it inside the same sentence wherever that reads better than splitting it into fragments: not "Logging is useful. It aids debugging. Debugging saves time." but "Logging earns its keep because it gives you something to reason from when a failure appears, and the time it saves during debugging usually outweighs what it cost to add."
- Vary length on purpose: short emphatic sentences, medium explanatory ones, long elaborative ones, and sentences that qualify an earlier statement. Keep a short sentence when it creates emphasis, delivers a key point, or gives a deliberate pause. The defect is a RUN of short, disconnected declaratives ("It is fast. It is cheap. It scales. It is safe."), so never write more than two of those in a row.
- Use commas, parenthetical clauses, em dashes, non-essential phrases and clarifying asides the way a person does, without overloading a sentence.
- Vary sentence openings. Avoid repeatedly starting with "This", "The", "It", "Additionally", "Furthermore", "Moreover" or "In addition". Alternative openings: {_pool(SENTENCE_OPENINGS)}

5. PARAGRAPHS THAT READ AS CONTINUOUS THOUGHT.
- Each paragraph develops an identifiable thought, and each sentence extends, qualifies, illustrates, challenges or elaborates the one before it. A flexible progression to draw on, never a template or a sentence count: introduce an idea, elaborate it, say why it matters, add a qualification or contrasting view, give an example or practical consequence, tie it to the wider discussion, and where it fits say what it means for you.
- Inside a paragraph, refer back to what was just introduced ("that distinction", "this trade-off") so the discussion builds instead of listing unrelated observations.
- Between paragraphs, pick up where the last one stopped: refer back to an earlier point, build on it, say how the next idea relates, and avoid abrupt jumps. Never restart the discussion as if each paragraph were written alone. The chapter must read as one conversation, not a stack of separately generated sections.
- Keep one logically connected explanation in one paragraph; do not shred it into one- and two-sentence paragraphs.
- Do not give every paragraph the same anatomy. Begin some with an observation, some with a qualification, a question, an example, a contrast, or a continuation of the previous paragraph.

6. HUMAN REASONING IN THE PROSE (an author interprets, contextualizes and weighs; an author does not merely list facts).
- Qualify: "This is not necessarily true in every situation." / "There are, of course, exceptions." / "The situation becomes more complicated when ..."
- Elaborate: "What this means in practice is ..." / "To understand why this matters, consider ..." / "What you should take away from this is ..."
- Contrast: "But this is only one side of the issue." / "What looks like a simple advantage can, in some circumstances, become a limitation." / "You should also consider the opposite possibility."
- Reflect, occasionally: "It is worth pausing to consider what this actually means." / "Ultimately, the issue is not simply whether ..., but whether ..."
- Contextualize: "Against this background ..." / "When you consider how these factors interact ..." / "Taken together, ..."

7. MECHANICAL AI PATTERNS TO ELIMINATE.
- Runs of short, isolated, parallel sentences; the same few openers ("This ...", "The ...", "It ...") repeated down the page; and the same small set of formal transitions. "Furthermore", "Moreover", "Additionally" and "In addition" may each appear at most once or twice in a chapter, and "In fact" and "Indeed" likewise. Reach for a wider range of natural connectives instead.
- Stock phrases such as "It is important to note that", "In conclusion", "In today's fast-paced world" and "delve into". Never close a section by restating how it opened.
- Every paragraph built on the same topic sentence, explanation, example, wrap-up pattern.
- Artificial brevity. Where an idea benefits from more explanation, give it: what it means, why it matters, how it connects to the other concepts, which qualifications apply, and what follows from it.

8. GUARDRAILS AGAINST OVERCORRECTION (equally mandatory).
- The goal is authentic human prose, not inflated prose. Every added clause must carry an idea: a reason, a consequence, a qualification, an example or a connection. Cut anything that only adds length, and never repeat the same point in different words.
- Do not put a conjunction in every sentence, open every paragraph with a transition, or turn every simple sentence into a tangle. Keep the grammar clean. Do not turn every paragraph into advice or motivation, and do not become needlessly academic.
- A connective must express a relationship that is true. Use "because" only for a real cause, "however" only for a real contrast, "therefore" only for a real consequence.
- Hedges ("arguably", "presumably", "apparently", "to some extent") are for points the research genuinely leaves open. Never hedge a verified fact, and never hedge to cover for a gap the research should have closed. Conversational language must never cost factual precision.
- The result should feel effortless, never engineered.

9. WHAT THE VOICE GOVERNS.
It governs running prose only. Headings, code blocks, equations, tables, figure captions, inline citations and **bold** index terms keep the exact, precise form their own rules require; the heading rules stay as they are, so do not put "you" in headings.

10. REVISION PASS (do this before confirming the work).
Re-read the file just written from top to bottom and edit it in place wherever it falls short: runs of three or more short sentences; sections or stretches of more than two paragraphs with no direct address; the same sentence opener or transition used repeatedly; paragraphs that restart instead of continuing; one-sentence paragraphs that belong to their neighbours; a "you" that sounds forced; padding. Fix them in the file itself. A revision may add elaboration or connection, but it must never remove the second person, strip the connectives, or compress developed sentences into terse ones.

ILLUSTRATIONS OF THE DIRECTION (show the target register; do NOT reuse their wording, topics or sentence skeletons):
BEFORE: "The queue is bounded. It applies backpressure. Producers block when it is full. This prevents memory exhaustion."
AFTER: "Because the queue is bounded, a producer that outpaces its consumer will eventually find the buffer full and block, and that blocking is exactly what you want: it pushes the pressure back onto the source instead of letting memory grow until the process is killed. You might be tempted to make the bound enormous so that nothing ever stalls, but then you have only postponed the failure, and you have made it harder to notice."
BEFORE: "Operators must verify the certificate chain. Failure to do so permits interception."
AFTER: "Before you trust an endpoint, you need to verify its certificate chain, since a client that skips the check will happily complete a handshake with whoever happens to be sitting in the middle of the connection. Admittedly, the check can feel like ceremony when you are working against a test server, but it is precisely the habit you will be glad to have formed when the same code reaches production."
BEFORE: "Caching improves latency. It adds complexity. Invalidation is difficult."
AFTER: "Caching is the obvious way to cut latency, and in most read-heavy systems it works well enough that you will reach for it almost by reflex; the catch, of course, is that every cache you add is also a second copy of the truth that you now have to keep honest. That is where invalidation comes in, and it is why you should decide how stale a result is allowed to be before you decide where the cache goes, not after.\""""

PROSE_PRESERVATION: str = (
    "PROSE PRESERVATION (mandatory): this phase corrects structure, layout or the build, "
    "not wording. The book's prose is written in a connected, conversational voice that "
    'addresses the reader directly as "you" and "your". Do not rewrite, condense, '
    'reorder, "tighten", de-personalise or otherwise edit any paragraph, and do not '
    "remove its conjunctions, transitions, conversational expressions or second-person "
    "address. Touch only the media, table, heading, page-break or reference lines the "
    "contract for this phase names. If a prose edit truly cannot be avoided (for example "
    "a caption that must be shortened), make the smallest change that works and keep the "
    "surrounding second-person, connected style."
)
