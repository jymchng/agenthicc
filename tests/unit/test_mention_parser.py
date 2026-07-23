from __future__ import annotations

import pytest
from agenthicc.mentions.parser import MentionKind, parse_mentions, strip_mentions

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# PRD-32 baseline tests
# ---------------------------------------------------------------------------


def test_parse_file_mention(tmp_path):
    (tmp_path / "README.md").write_text("hello")
    mentions = parse_mentions("Read @README.md please", cwd=tmp_path)
    assert len(mentions) == 1
    m = mentions[0]
    assert m.path == "README.md"
    assert m.kind == MentionKind.FILE
    assert m.resolved == (tmp_path / "README.md").resolve()


def test_parse_directory_mention(tmp_path):
    (tmp_path / "src").mkdir()
    mentions = parse_mentions("Look at @src/", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.DIRECTORY


def test_parse_url_mention(tmp_path):
    mentions = parse_mentions("See @https://example.com/doc", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.URL
    assert mentions[0].resolved is None


def test_parse_glob_mention(tmp_path):
    mentions = parse_mentions("Load @src/**/*.py", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.GLOB


def test_parse_unresolved_mention(tmp_path):
    mentions = parse_mentions("Check @does_not_exist.txt", cwd=tmp_path)
    assert mentions[0].kind == MentionKind.UNRESOLVED


def test_multiple_mentions(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    mentions = parse_mentions("Compare @a.py and @b.py", cwd=tmp_path)
    assert len(mentions) == 2
    assert {m.path for m in mentions} == {"a.py", "b.py"}


def test_no_mentions(tmp_path):
    assert parse_mentions("Hello world", cwd=tmp_path) == []


def test_mention_stops_at_comma(tmp_path):
    (tmp_path / "file.py").write_text("")
    mentions = parse_mentions("Read @file.py,please", cwd=tmp_path)
    assert mentions[0].path == "file.py"


def test_question_mark_after_existing_file_is_sentence_punctuation(tmp_path):
    """A question ending in a file mention does not create a glob wildcard."""
    (tmp_path / "README.md").write_text("hello")

    text = "what is @README.md?"
    mentions = parse_mentions(text, cwd=tmp_path)

    assert len(mentions) == 1
    assert mentions[0].path == "README.md"
    assert mentions[0].kind == MentionKind.FILE
    assert text[mentions[0].start : mentions[0].end] == "@README.md"


def test_mention_stops_at_whitespace(tmp_path):
    mentions = parse_mentions("@foo bar", cwd=tmp_path)
    assert mentions[0].path == "foo"


def test_strip_mentions_removes_at_prefix(tmp_path):
    (tmp_path / "auth.py").write_text("")
    mentions = parse_mentions("Review @auth.py for issues", cwd=tmp_path)
    stripped = strip_mentions("Review @auth.py for issues", mentions)
    assert stripped == "Review auth.py for issues"


def test_start_end_positions(tmp_path):
    (tmp_path / "f.py").write_text("")
    mentions = parse_mentions("x @f.py y", cwd=tmp_path)
    text = "x @f.py y"
    m = mentions[0]
    assert text[m.start : m.end] == "@f.py"


# ---------------------------------------------------------------------------
# Additional tests
# ---------------------------------------------------------------------------


def test_parse_nested_path(tmp_path):
    """@src/app/main.py resolves as FILE if the file exists."""
    nested = tmp_path / "src" / "app"
    nested.mkdir(parents=True)
    (nested / "main.py").write_text("# main")
    mentions = parse_mentions("Review @src/app/main.py", cwd=tmp_path)
    assert len(mentions) == 1
    m = mentions[0]
    assert m.path == "src/app/main.py"
    assert m.kind == MentionKind.FILE
    assert m.resolved == (tmp_path / "src" / "app" / "main.py").resolve()


def test_mention_at_start_of_string(tmp_path):
    """A mention at position 0 is captured correctly."""
    (tmp_path / "auth.py").write_text("")
    mentions = parse_mentions("@auth.py is important", cwd=tmp_path)
    assert len(mentions) == 1
    m = mentions[0]
    assert m.start == 0
    assert m.path == "auth.py"
    assert m.kind == MentionKind.FILE


def test_multiple_at_same_file_twice(tmp_path):
    """The same file mentioned twice produces two separate Mention objects."""
    (tmp_path / "utils.py").write_text("")
    mentions = parse_mentions("See @utils.py and also @utils.py", cwd=tmp_path)
    assert len(mentions) == 2
    assert mentions[0].path == "utils.py"
    assert mentions[1].path == "utils.py"
    # They are distinct objects with different positions
    assert mentions[0].start != mentions[1].start


def test_glob_with_question_mark(tmp_path):
    """A path containing '?' is classified as GLOB."""
    mentions = parse_mentions("Check @src/?.py", cwd=tmp_path)
    assert len(mentions) == 1
    assert mentions[0].kind == MentionKind.GLOB


def test_directory_without_trailing_slash(tmp_path):
    """An existing directory referenced without trailing '/' is still DIRECTORY."""
    (tmp_path / "mydir").mkdir()
    mentions = parse_mentions("List @mydir", cwd=tmp_path)
    assert len(mentions) == 1
    assert mentions[0].kind == MentionKind.DIRECTORY


def test_strip_mentions_multiple(tmp_path):
    """Two mentions are replaced correctly; right-to-left order keeps offsets valid."""
    (tmp_path / "foo.py").write_text("")
    (tmp_path / "bar.py").write_text("")
    text = "Compare @foo.py with @bar.py"
    mentions = parse_mentions(text, cwd=tmp_path)
    assert len(mentions) == 2
    stripped = strip_mentions(text, mentions)
    assert stripped == "Compare foo.py with bar.py"
