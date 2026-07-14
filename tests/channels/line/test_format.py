from __future__ import annotations

from alice_office_router.channels.line.format import (
    format_for_line,
    split_for_line,
    strip_markdown_preserving_urls,
)


class TestStripMarkdownPreservingUrls:
    def test_empty_string_returns_unchanged(self) -> None:
        """An empty string short-circuits and is returned as-is."""
        assert strip_markdown_preserving_urls("") == ""

    def test_plain_text_is_unchanged(self) -> None:
        """Text with no Markdown syntax passes through untouched."""
        assert strip_markdown_preserving_urls("哈囉！") == "哈囉！"

    def test_bold_markers_are_stripped(self) -> None:
        assert strip_markdown_preserving_urls("**重要**訊息") == "重要訊息"

    def test_italic_markers_are_stripped(self) -> None:
        assert strip_markdown_preserving_urls("*emphasis*") == "emphasis"

    def test_inline_code_backticks_are_stripped(self) -> None:
        assert strip_markdown_preserving_urls("執行 `ls -la`") == "執行 ls -la"

    def test_code_block_fences_stripped_content_kept(self) -> None:
        result = strip_markdown_preserving_urls("```python\nprint(1)\n```")
        assert result == "print(1)"

    def test_heading_prefix_is_stripped(self) -> None:
        assert strip_markdown_preserving_urls("## 標題") == "標題"

    def test_bullet_marker_converted_to_dot(self) -> None:
        assert strip_markdown_preserving_urls("- 項目一") == "• 項目一"

    def test_markdown_link_preserves_tappable_url(self) -> None:
        result = strip_markdown_preserving_urls("[點我](https://example.com)")
        assert result == "點我 (https://example.com)"

    def test_bare_url_is_untouched(self) -> None:
        text = "參考 https://example.com/path 這裡"
        assert strip_markdown_preserving_urls(text) == text


class TestSplitForLine:
    def test_empty_text_returns_empty_list(self) -> None:
        assert split_for_line("") == []

    def test_short_text_returns_single_chunk(self) -> None:
        assert split_for_line("哈囉") == ["哈囉"]

    def test_text_at_exact_limit_is_single_chunk(self) -> None:
        text = "a" * 4500
        assert split_for_line(text, max_chars=4500) == [text]

    def test_long_text_splits_into_bounded_chunks(self) -> None:
        text = "a" * 10000
        chunks = split_for_line(text, max_chars=4500)
        assert len(chunks) <= 5
        assert all(len(c) <= 4500 for c in chunks)

    def test_never_exceeds_five_chunks(self) -> None:
        text = "word " * 20000
        chunks = split_for_line(text, max_chars=100)
        assert len(chunks) == 5

    def test_overflow_final_chunk_is_truncated_with_ellipsis(self) -> None:
        text = "word " * 20000
        chunks = split_for_line(text, max_chars=100)
        assert chunks[-1].endswith("…")

    def test_prefers_paragraph_break(self) -> None:
        text = "第一段。" * 500 + "\n\n" + "第二段。" * 500
        chunks = split_for_line(text, max_chars=2200)
        assert chunks[0].endswith("第一段。")


class TestFormatForLine:
    def test_strips_markdown_then_splits(self) -> None:
        assert format_for_line("**hi**") == ["hi"]

    def test_blank_text_returns_empty_list(self) -> None:
        assert format_for_line("") == []
