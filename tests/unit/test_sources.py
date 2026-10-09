from lab.ingest import sources as src
from lab.ingest.index import point_id

EXCLUDE = ["CLAUDE.md", "docs/archive/**"]


def make_repo(tmp_path, files):
    for rel in files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return tmp_path


def test_select_files_paths_excludes_and_suffixes(tmp_path):
    root = make_repo(tmp_path, [
        "README.rst", "CLAUDE.md", "setup.py",
        "docs/a.md", "docs/sub/b.rst", "docs/CLAUDE.md", "docs/archive/old.md", "docs/img.png",
        "src/pyefis/config/default.yaml", "src/pyefis/main.py",
        "other/README.rst",
    ])
    source = src.Source(repo="o/r", branch="main", origin="fork",
                        paths=["README.rst", "docs/**", "src/pyefis/config/**"])
    assert src.select_files(root, source, EXCLUDE) == [
        "README.rst", "docs/a.md", "docs/sub/b.rst", "src/pyefis/config/default.yaml",
    ]


def test_glob_semantics():
    assert src.matches("docs/a/b.md", "docs/**")
    assert not src.matches("docs/a/b.md", "docs/*")
    assert src.matches("x/y/CLAUDE.md", "CLAUDE.md")
    assert src.matches("a.md", "**/*.md") and src.matches("d/a.md", "**/*.md")


def test_fingerprint_tracks_selection_config():
    s = src.Source(repo="o/r", branch="main", origin="fork", paths=["docs/**"])
    base = src.fingerprint(s, EXCLUDE, "1")
    assert base == src.fingerprint(s, list(EXCLUDE), "1")
    assert base != src.fingerprint(s, EXCLUDE, "2")
    assert base != src.fingerprint(s, EXCLUDE + ["x"], "1")


def test_point_ids_are_deterministic():
    assert point_id("o/r@main", "k", "README.rst", 0) == point_id("o/r@main", "k", "README.rst", 0)
    assert point_id("o/r@main", "k", "README.rst", 0) != point_id("o/r@main", "k", "README.rst", 1)


def test_repo_sources_yaml_loads():
    cfg = src.load()
    assert {s.origin for s in cfg.sources} == {"fork", "upstream"}
    assert "CLAUDE.md" in cfg.exclude
