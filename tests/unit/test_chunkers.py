from lab.ingest.chunkers import _split, chunk_file

BODY = "Enough body text to clear the minimum chunk size comfortably."


def headings(chunks):
    return [" > ".join(c.heading_path) for c in chunks]


def test_markdown_heading_path_and_fences():
    md = f"""# Guide
{BODY}
## Install
{BODY}
```sh
# not a heading
```
### Pi
{BODY}
## Run
{BODY}
"""
    chunks = chunk_file("docs/guide.md", md)
    assert headings(chunks) == ["Guide", "Guide > Install", "Guide > Install > Pi", "Guide > Run"]
    assert "# not a heading" in chunks[1].text


def test_markdown_preamble_and_min_chars():
    md = f"{BODY}\n# Empty\n\n# Full\n{BODY}\n"
    chunks = chunk_file("README.md", md)
    assert headings(chunks) == ["(top of file)", "Full"]


def test_rst_levels_follow_first_appearance():
    rst = f"""======
pyEfis
======
{BODY}

Hardware
========
{BODY}

Minimum
-------
{BODY}

Testing
=======
{BODY}
"""
    chunks = chunk_file("README.rst", rst)
    assert headings(chunks) == [
        "pyEfis", "pyEfis > Hardware", "pyEfis > Hardware > Minimum", "pyEfis > Testing",
    ]


def test_rst_short_overline_is_not_a_title():
    # Seen in the wild: ''' used as a makeshift code fence around a command.
    rst = f"Usage\n=====\n{BODY}\n'''\n./MakeCIFPIndex.py CIFP/FAACIFP18\n'''\n"
    chunks = chunk_file("README.rst", rst)
    assert headings(chunks) == ["Usage"]
    assert "MakeCIFPIndex.py" in chunks[0].text


def test_yaml_top_level_keys_keep_their_comments():
    y = f"""# File header: {BODY}

# Comment for main: {BODY}
main:
  nodeID: 1
  # nested comment stays with main
  ini_file: config/fix.ini

instruments:
  - type: airspeed
"""
    chunks = chunk_file("config/default.yaml", y, min_chars=10)
    assert headings(chunks) == ["(file header)", "main", "instruments"]
    assert chunks[1].text.startswith("# Comment for main")
    assert "nested comment stays with main" in chunks[1].text


def test_split_respects_max_chars_and_keeps_everything():
    paras = [f"para {i} " + "x" * 300 for i in range(10)]
    text = "\n\n".join(paras)
    pieces = _split(text, 1000, by_lines=False)
    assert all(len(p) <= 1000 for p in pieces)
    assert "".join(pieces).replace("\n", "") == text.replace("\n", "")


def test_split_hard_splits_a_single_huge_unit():
    pieces = _split("y" * 2500, 1000, by_lines=True)
    assert [len(p) for p in pieces] == [1000, 1000, 500]
