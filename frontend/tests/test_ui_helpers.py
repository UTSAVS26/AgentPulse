"""Pure UI helpers (no backend needed)."""

from ui import preview_markdown


def test_preview_markdown_shifts_headings_but_not_code_or_hashtags():
    md = "# Title\n\n## Section\n\n```\n# not a heading\n```\n#hashtag\n###### deep"
    out = preview_markdown(md).splitlines()
    assert out[0] == "### Title" and out[2] == "#### Section"
    assert out[5] == "# not a heading"  # inside a code fence
    assert out[7] == "#hashtag"  # no space: not a heading
    assert out[8] == "###### deep"  # capped at h6
