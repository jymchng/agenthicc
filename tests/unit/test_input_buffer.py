"""Unit tests for agenthicc.tui.input.buffer.InputBuffer (PRD-57 §10.2)."""
from __future__ import annotations

import pytest
from agenthicc.tui.input.buffer import InputBuffer

pytestmark = pytest.mark.unit


class TestInsert:
    def test_insert_advances_cursor(self) -> None:
        buf = InputBuffer()
        buf.insert("a")
        assert buf.buf == ["a"]
        assert buf.cursor == 1

    def test_insert_at_middle(self) -> None:
        buf = InputBuffer(list("hello"))
        buf.cursor = 2          # between 'e' and 'l'
        buf.insert("X")
        assert "".join(buf.buf) == "heXllo"
        assert buf.cursor == 3

    def test_insert_many_returns_range(self) -> None:
        buf = InputBuffer()
        start, end = buf.insert_many(["a", "b", "c"])
        assert start == 0
        assert end == 3
        assert "".join(buf.buf) == "abc"

    def test_insert_multibyte_char(self) -> None:
        buf = InputBuffer()
        buf.insert("❯")
        assert buf.buf == ["❯"]
        assert buf.cursor == 1


class TestDeleteBefore:
    def test_backspace_removes_char(self) -> None:
        buf = InputBuffer(list("hello"))
        buf.delete_before()
        assert "".join(buf.buf) == "hell"
        assert buf.cursor == 4

    def test_backspace_at_zero_is_noop(self) -> None:
        buf = InputBuffer()
        buf.delete_before()     # should not raise
        assert buf.buf == []
        assert buf.cursor == 0

    def test_backspace_at_middle(self) -> None:
        buf = InputBuffer(list("hello"))
        buf.cursor = 3          # after 'l'
        buf.delete_before()
        assert "".join(buf.buf) == "helo"
        assert buf.cursor == 2


class TestDeleteRange:
    def test_delete_range(self) -> None:
        buf = InputBuffer(list("hello"))
        buf.cursor = 5
        buf.delete_range(1, 3)
        assert "".join(buf.buf) == "hlo"

    def test_delete_range_clamps_cursor(self) -> None:
        buf = InputBuffer(list("hello"))
        buf.cursor = 4
        buf.delete_range(0, 5)  # delete everything
        assert buf.buf == []
        assert buf.cursor == 0


class TestSet:
    def test_set_replaces_buf(self) -> None:
        buf = InputBuffer(list("old"))
        buf.set(list("new text"))
        assert "".join(buf.buf) == "new text"
        assert buf.cursor == 8  # defaults to end

    def test_set_with_explicit_cursor(self) -> None:
        buf = InputBuffer()
        buf.set(list("hello"), cursor=2)
        assert buf.cursor == 2

    def test_set_clamps_cursor(self) -> None:
        buf = InputBuffer()
        buf.set(list("hi"), cursor=100)
        assert buf.cursor == 2


class TestCursorNavigation:
    def test_move_left_right(self) -> None:
        buf = InputBuffer(list("abc"))
        assert buf.cursor == 3
        buf.move_left()
        assert buf.cursor == 2
        buf.move_right()
        assert buf.cursor == 3

    def test_move_left_clamps(self) -> None:
        buf = InputBuffer()
        buf.move_left()
        assert buf.cursor == 0

    def test_move_right_clamps(self) -> None:
        buf = InputBuffer(list("x"))
        buf.move_right()
        buf.move_right()    # already at end
        assert buf.cursor == 1

    def test_move_home_single_line(self) -> None:
        buf = InputBuffer(list("hello"))
        buf.move_home()
        assert buf.cursor == 0

    def test_move_end_single_line(self) -> None:
        buf = InputBuffer(list("hello"))
        buf.cursor = 0
        buf.move_end()
        assert buf.cursor == 5

    def test_move_home_multiline(self) -> None:
        buf = InputBuffer(list("line1\nline2"))
        buf.cursor = 8          # inside 'line2'
        buf.move_home()
        assert buf.cursor == 6  # start of 'line2'

    def test_move_end_multiline(self) -> None:
        buf = InputBuffer(list("line1\nline2"))
        buf.cursor = 2          # inside 'line1'
        buf.move_end()
        assert buf.cursor == 5  # end of 'line1' (before \n)

    def test_move_up_false_on_first_line(self) -> None:
        buf = InputBuffer(list("hello"))
        assert buf.move_up() is False

    def test_move_up_true_on_second_line(self) -> None:
        buf = InputBuffer(list("line1\nline2"))
        buf.cursor = 9          # on 'line2'
        assert buf.move_up() is True
        assert buf.cursor <= 5  # on 'line1'

    def test_move_down_false_on_last_line(self) -> None:
        buf = InputBuffer(list("hello"))
        assert buf.move_down() is False

    def test_move_down_true_on_first_line(self) -> None:
        buf = InputBuffer(list("line1\nline2"))
        buf.cursor = 2          # on 'line1'
        assert buf.move_down() is True
        assert buf.cursor >= 6  # on 'line2'


class TestClear:
    def test_clear_resets_state(self) -> None:
        buf = InputBuffer(list("stuff"))
        buf.clear()
        assert buf.buf == []
        assert buf.cursor == 0
