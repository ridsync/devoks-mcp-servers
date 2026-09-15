"""Tests for devoks_slackbot.slack.format (TASK-009).

Traces: AC-SB-006-2, CTR-SB-005, EDGE-SB-010.
"""

from __future__ import annotations

from typing import Any

import pytest

from devoks_slackbot.config import MAX_RESPONSE_CHARS_DEFAULT
from devoks_slackbot.slack.format import TRUNCATION_NOTICE, apply_response_length_policy

#: First two codepoints of each sequence below are (base, at-risk-modifier) —
#: used to land a truncation cut exactly after the modifier and verify it
#: gets backed off rather than left dangling.
_FAMILY_EMOJI = "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"  # base ZWJ base ZWJ base ZWJ base
_SKIN_TONE_THUMBS_UP = "\U0001f44d\U0001f3fb"  # base + Fitzpatrick modifier
_VARIATION_HEART = "❤️"  # base + variation selector-16
_COMBINING_E_ACUTE = "é"  # base + combining acute accent


def _max_chars_for_content_budget(budget: int) -> int:
    """Pick ``max_chars`` so the reserved content budget equals ``budget`` exactly.

    Derives the offset from ``TRUNCATION_NOTICE`` itself rather than a
    hardcoded number, so these tests do not silently stop landing on the
    intended boundary if the notice text's length ever changes.
    """
    return budget + len(TRUNCATION_NOTICE)


# --- CTR-SB-005: within-limit / boundary ------------------------------------


def test_within_limit_returned_unchanged_no_notice_CTR_SB_005() -> None:
    text = "a" * (MAX_RESPONSE_CHARS_DEFAULT - 100)
    assert apply_response_length_policy(text) == text
    assert TRUNCATION_NOTICE not in apply_response_length_policy(text)


def test_boundary_3499_unchanged_CTR_SB_005() -> None:
    text = "a" * (MAX_RESPONSE_CHARS_DEFAULT - 1)
    assert apply_response_length_policy(text) == text


def test_boundary_3500_unchanged_CTR_SB_005() -> None:
    text = "a" * MAX_RESPONSE_CHARS_DEFAULT
    assert apply_response_length_policy(text) == text


def test_boundary_3501_is_truncated_CTR_SB_005() -> None:
    text = "a" * (MAX_RESPONSE_CHARS_DEFAULT + 1)
    result = apply_response_length_policy(text)
    assert result != text
    assert len(result) <= MAX_RESPONSE_CHARS_DEFAULT
    assert TRUNCATION_NOTICE in result


# --- AC-SB-006-2: truncated result never exceeds the limit -----------------


@pytest.mark.parametrize("length", [3501, 5000, 40000, 100000])
def test_truncated_result_never_exceeds_limit_AC_SB_006_2(length: int) -> None:
    text = "a" * length
    result = apply_response_length_policy(text, max_chars=MAX_RESPONSE_CHARS_DEFAULT)
    assert len(result) <= MAX_RESPONSE_CHARS_DEFAULT


# --- EDGE-SB-010: truncation notice is present -------------------------------


def test_truncation_includes_notice_EDGE_SB_010() -> None:
    text = "a" * (MAX_RESPONSE_CHARS_DEFAULT + 1)
    assert TRUNCATION_NOTICE in apply_response_length_policy(text)


def test_truncation_notice_is_korean_and_nonempty_EDGE_SB_010() -> None:
    assert TRUNCATION_NOTICE.strip()
    assert any("가" <= ch <= "힣" for ch in TRUNCATION_NOTICE)


# --- grapheme-boundary back-off (trap #2) -----------------------------------


def test_zwj_sequence_boundary_has_no_dangling_joiner() -> None:
    prefix = "a" * 50
    text = prefix + _FAMILY_EMOJI + "b" * 200
    max_chars = _max_chars_for_content_budget(len(prefix) + 2)
    result = apply_response_length_policy(text, max_chars=max_chars)
    content = result.removesuffix(TRUNCATION_NOTICE)
    assert not content.endswith("‍")
    # The budget fits 2 of the family's 7 codepoints, so no part of it can be
    # kept: keeping ``_FAMILY_EMOJI[0]`` alone would render a lone man where the
    # source had a four-person family — a valid glyph, but not this text's.
    assert content == prefix
    assert len(result) <= max_chars


def test_skin_tone_modifier_boundary_has_no_dangling_modifier() -> None:
    prefix = "a" * 50
    text = prefix + _SKIN_TONE_THUMBS_UP + "b" * 200
    max_chars = _max_chars_for_content_budget(len(prefix) + 2)
    result = apply_response_length_policy(text, max_chars=max_chars)
    content = result.removesuffix(TRUNCATION_NOTICE)
    # The modifier *completes* the cluster and the pair fits the budget exactly,
    # so both codepoints stay. Trimming the modifier would waste a character of
    # budget and render a default-skin thumbs up the author did not write.
    assert content == prefix + _SKIN_TONE_THUMBS_UP
    assert len(result) <= max_chars


def test_variation_selector_boundary_has_no_dangling_selector() -> None:
    prefix = "a" * 50
    text = prefix + _VARIATION_HEART + "b" * 200
    max_chars = _max_chars_for_content_budget(len(prefix) + 2)
    result = apply_response_length_policy(text, max_chars=max_chars)
    content = result.removesuffix(TRUNCATION_NOTICE)
    # VS-16 completes the cluster; dropping it renders the text-style heart
    # instead of the emoji one. The pair fits the budget exactly.
    assert content == prefix + _VARIATION_HEART
    assert len(result) <= max_chars


def test_combining_mark_boundary_has_no_dangling_mark() -> None:
    prefix = "a" * 50
    text = prefix + _COMBINING_E_ACUTE + "b" * 200
    max_chars = _max_chars_for_content_budget(len(prefix) + 2)
    result = apply_response_length_policy(text, max_chars=max_chars)
    content = result.removesuffix(TRUNCATION_NOTICE)
    # The combining acute completes "é"; dropping it leaves a bare "e".
    assert content == prefix + _COMBINING_E_ACUTE
    assert len(result) <= max_chars


def test_korean_text_boundary_stays_a_clean_prefix() -> None:
    sentence = "안녕하세요, 이것은 데브옥스 슬랙봇 테스트 문장입니다. "
    text = sentence * 20
    max_chars = 300
    result = apply_response_length_policy(text, max_chars=max_chars)
    assert len(result) <= max_chars
    assert TRUNCATION_NOTICE in result
    content = result.removesuffix(TRUNCATION_NOTICE)
    # Precomposed Hangul has no combining marks to back off from, so the
    # content must be exactly the untouched prefix at the reserved budget.
    assert content == text[: max_chars - len(TRUNCATION_NOTICE)]


# --- code-fence balancing (trap #3) -----------------------------------------


def test_unclosed_code_fence_gets_closed() -> None:
    text = "설명입니다\n```\n" + ("code line\n" * 1000)
    max_chars = 500
    result = apply_response_length_policy(text, max_chars=max_chars)
    assert len(result) <= max_chars
    assert result.count("```") == 2


def test_already_balanced_fence_is_left_alone() -> None:
    text = "```\nshort snippet\n```\n" + ("filler " * 1000)
    max_chars = 500
    result = apply_response_length_policy(text, max_chars=max_chars)
    assert len(result) <= max_chars
    assert result.count("```") % 2 == 0


# --- trap #4: max_chars smaller than the notice -----------------------------


@pytest.mark.parametrize("max_chars", [1, 5])
def test_max_chars_smaller_than_notice_omits_notice_EDGE_SB_010(max_chars: int) -> None:
    text = "hello world, this is a long answer"
    result = apply_response_length_policy(text, max_chars=max_chars)
    assert TRUNCATION_NOTICE not in result
    assert len(result) <= max_chars
    assert result == text[:max_chars]


def test_max_chars_smaller_than_notice_still_backs_off_combining() -> None:
    text = _COMBINING_E_ACUTE + "xyz"
    max_chars = 2  # exactly covers "e" + the combining acute accent
    assert max_chars < len(TRUNCATION_NOTICE)
    result = apply_response_length_policy(text, max_chars=max_chars)
    assert TRUNCATION_NOTICE not in result
    # Both codepoints fit the 2-char budget, so the complete "é" survives.
    assert result == _COMBINING_E_ACUTE
    assert len(result) <= max_chars


# --- defensive input handling ------------------------------------------------


def test_empty_string_returns_empty_string() -> None:
    assert apply_response_length_policy("") == ""


def test_none_input_returns_empty_string() -> None:
    assert apply_response_length_policy(None) == ""


@pytest.mark.parametrize(
    "value", [123, 3.14, ["not", "a", "string"], {"k": "v"}, object()], ids=type
)
def test_non_string_input_returns_empty_string(value: Any) -> None:
    assert apply_response_length_policy(value) == ""


# --- max_chars outside CTR-SB-005's valid range -----------------------------


def test_max_chars_zero_returns_empty_string() -> None:
    assert apply_response_length_policy("hello", max_chars=0) == ""


def test_max_chars_negative_returns_empty_string() -> None:
    assert apply_response_length_policy("hello", max_chars=-100) == ""


def test_empty_text_with_negative_max_chars_does_not_raise() -> None:
    assert apply_response_length_policy("", max_chars=-1) == ""


# --- repeated application (연타) is idempotent -------------------------------


def test_repeated_application_is_idempotent() -> None:
    text = "a" * (MAX_RESPONSE_CHARS_DEFAULT + 500)
    once = apply_response_length_policy(text)
    twice = apply_response_length_policy(once)
    assert once == twice


def test_regional_indicator_pair_is_never_split_edge_sb_010() -> None:
    """A split flag leaves a lone indicator, rendered as a boxed letter.

    Regional indicators are not combining marks, so the combining guard does
    not see them — they need their own parity check.
    """
    flag = "\U0001f1f0\U0001f1f7"  # 🇰🇷 = two regional indicators
    prefix = "a" * 50
    text = prefix + flag * 200  # far longer than any budget below, so truncation always fires
    for extra in range(1, 12):
        max_chars = _max_chars_for_content_budget(len(prefix) + extra)
        content = apply_response_length_policy(text, max_chars=max_chars).removesuffix(
            TRUNCATION_NOTICE
        )
        indicators = sum(1 for ch in content if 0x1F1E6 <= ord(ch) <= 0x1F1FF)
        assert indicators % 2 == 0, f"lone indicator at budget {extra}: {content!r}"
        assert len(content) <= len(prefix) + extra
