"""Tests for the label/log sanitiser.

Everything a raven sends is rendered into a desktop menu and a log file, so this
module is the only thing between an attacker-chosen string and a user's screen.
The cases here are the two real attacks: terminal/log forgery via escapes, and
menu spoofing via invisible or direction-reversing characters.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from roost import sanitize


class TestSanitizeLabel:
    def test_plain_text_is_unchanged(self):
        assert sanitize.sanitize_label("Approve: deploy to staging") == \
            "Approve: deploy to staging"

    def test_unicode_is_preserved(self):
        """Only dangerous characters go; a label may legitimately be non-ASCII."""
        assert sanitize.sanitize_label("Muninn — 3 sessions ↗") == "Muninn — 3 sessions ↗"

    @pytest.mark.parametrize("hostile,expected", [
        ("\x1b[31mred", "red"),
        ("\x1b[2J\x1b[Hcleared", "cleared"),
        ("a\x1b]0;title\x07b", "ab"),
        ("a\x1b]0;title\x1b\\b", "ab"),
        ("a\x1bMb", "ab"),
        ("bare\x1bescape", "bareescape"),
    ])
    def test_ansi_sequences_are_removed_whole(self, hostile, expected):
        """The printable tail of a stripped sequence must not survive as text."""
        assert sanitize.sanitize_label(hostile) == expected

    @pytest.mark.parametrize("hostile", [
        "a\x00b", "a\x07b", "a\x1fb", "a\x7fb", "a\x9bb",
    ])
    def test_control_characters_are_removed(self, hostile):
        assert sanitize.sanitize_label(hostile) == "ab"

    @pytest.mark.parametrize("hostile", [
        "a​b",   # zero-width space
        "a‮b",   # right-to-left override
        "a⁦b",   # left-to-right isolate
        "a﻿b",   # BOM
        "a⁠b",   # word joiner
    ])
    def test_spoofing_characters_are_removed(self, hostile):
        assert sanitize.sanitize_label(hostile) == "ab"

    def test_newlines_collapse_to_a_single_line(self):
        """A menu label is one line; a second line is a forged item."""
        assert sanitize.sanitize_label("Quit\nQuit All") == "Quit Quit All"

    def test_crlf_cannot_survive(self):
        assert "\r" not in sanitize.sanitize_label("a\r\nb")
        assert "\n" not in sanitize.sanitize_label("a\r\nb")

    def test_whitespace_runs_collapse_and_trim(self):
        assert sanitize.sanitize_label("  a\t\t b   c  ") == "a b c"

    def test_length_is_capped_with_a_marker(self):
        result = sanitize.sanitize_label("x" * 500, 20)
        assert len(result) == 20
        assert result.endswith("…")

    def test_zero_limit_disables_truncation(self):
        assert sanitize.sanitize_label("x" * 300, 0) == "x" * 300

    @pytest.mark.parametrize("value", [None, 42, [], {}, object()])
    def test_non_strings_collapse_to_empty(self, value):
        """repr() of a hostile object would put attacker punctuation on screen."""
        assert sanitize.sanitize_label(value) == ""

    def test_a_label_of_only_control_characters_becomes_empty(self):
        assert sanitize.sanitize_label("\x1b[31m\x00\x07") == ""

    def test_output_is_always_safe_by_its_own_test(self):
        for hostile in ("\x1b[31ma", "a‮b", "a\x00b", "\r\n\r\n"):
            assert not sanitize.contains_unsafe_text(sanitize.sanitize_label(hostile))


class TestContainsUnsafeText:
    @pytest.mark.parametrize("value", ["plain", "Muninn — ok", "a b c", ""])
    def test_safe_values(self, value):
        assert sanitize.contains_unsafe_text(value) is False

    @pytest.mark.parametrize("value", [
        "\x1b[31m", "a\x00b", "a\x07b", "a‮b", "a​b", "a\x9bb",
    ])
    def test_unsafe_values(self, value):
        assert sanitize.contains_unsafe_text(value) is True

    def test_newline_alone_is_not_flagged(self):
        """Newlines are collapsed rather than refused; only controls are fatal."""
        assert sanitize.contains_unsafe_text("a\nb") is False

    @pytest.mark.parametrize("value", [None, 42, [], {}])
    def test_non_strings_are_unsafe(self, value):
        assert sanitize.contains_unsafe_text(value) is True


class TestSafeForLog:
    def test_empty_becomes_a_visible_placeholder(self):
        """A log line must never silently lose its subject."""
        assert sanitize.safe_for_log("") == "<empty>"
        assert sanitize.safe_for_log("\x00\x1b[31m") == "<empty>"

    def test_log_output_carries_no_escape(self):
        assert "\x1b" not in sanitize.safe_for_log("\x1b[2Jwiped")

    def test_log_output_is_short(self):
        assert len(sanitize.safe_for_log("y" * 500)) <= sanitize.DEFAULT_LOG_LIMIT
