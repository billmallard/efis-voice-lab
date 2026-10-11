"""D1: the report renders from published runs, and a regression shows on the page."""

import copy

from lab import config
from lab.harness import publish, report


def l2_history():
    runs = publish.load_runs("L2")
    assert runs, "runs/L2 is the committed history the site is built from"
    worse = copy.deepcopy(runs[-1])
    worse["started_at"] = "2099-01-01T00:00:00+00:00"
    worse["lab_commit"] = "abc1234-dirty"
    worse["aggregate"]["correctness_pass_rate"] = 0.10
    return runs + [worse]


def test_stamp_and_names():
    run = {"started_at": "2026-10-10T22:01:16+00:00"}
    assert publish.stamp(run) == "20261010T220116Z"
    assert publish.audio_tag("L3", run) == "audio-L3-20261010T220116Z"
    assert publish.mp3_name("results/L3/audio/X_v1-reply.wav") == "X_v1-reply.mp3"


def test_published_runs_are_ordered_and_named_by_start():
    for layer in publish.LAYERS:
        for run in publish.load_runs(layer):
            assert (publish.RUNS / layer / f"{publish.stamp(run)}.json").is_file()


def test_delta_direction_and_color():
    assert "▼ 45 pts" in report.delta(0.10, 0.55, "rate", True)
    assert "bad" in report.delta(0.10, 0.55, "rate", True)
    # Latency: lower is better, so a drop is good.
    assert "ok" in report.delta(9000.0, 15000.0, "ms", False)
    assert "= prev" in report.delta(0.5, 0.5, "rate", True)
    assert report.delta(None, 0.5, "rate", True) == ""


def test_regression_appears_on_the_page(tmp_path):
    history = {"L2": l2_history()}
    page = report.build(config.load(), tmp_path, history=history, audio=lambda src: None,
                        repo_url="https://example.invalid/repo").read_text(encoding="utf-8")
    assert "▼ 45 pts" in page                       # the card shows the drop
    assert "<h2>History</h2>" in page and "<polyline" in page
    assert page.count("<tr><td>") >= len(history["L2"])  # every run is in the table
    assert 'href="https://example.invalid/repo/commit/abc1234"' in page


def test_single_run_has_no_history_section(tmp_path):
    history = {"L2": publish.load_runs("L2")[-1:]}
    page = report.build(config.load(), tmp_path, history=history,
                        audio=lambda src: None).read_text(encoding="utf-8")
    assert "<h2>History</h2>" not in page and "<h2>Runs</h2>" in page


def test_corpus_line_skips_unindexed_sources():
    run = {"index": {"sources": {"a/b@main": "0123456789", "c/d@main": None}}}
    assert report.corpus_html(run) == "a/b@main <code>0123456</code>"
