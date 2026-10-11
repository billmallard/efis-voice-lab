"""Report: one static HTML page from L1, L2 and L3 runs.

    uv run lab report            -> results/report/index.html (+ audio/ next to it)
    uv run lab site              -> site/ from the published runs (see lab.harness.site)

Per-layer scores with the change since the previous run, failures by attribution tag, the
persona sweep, behavior scenarios, and a per-case drill-down: what the caller said, what
STT heard, what the agent said, the tags, the judges' reasons, the retrieved sources,
timings, and both sides' audio. With more than one run per layer it adds trend charts and
a table of every run. L2 failures are attributed with the same rules as L3 (no STT, no
latency). Nothing in the page needs a server.
"""

from __future__ import annotations

import html
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lab.config import ROOT, LabConfig
from lab.harness import attribution, golden

OUT = ROOT / "results" / "report"
LAYERS = ("L1", "L2", "L3")
LAYER_NAMES = {"L1": "retrieval", "L2": "agent (text)", "L3": "voice"}

# src (a repo-relative WAV path from a run record) -> href for the page, or None.
AudioResolver = Callable[[str], "str | None"]


def _load(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def ms(x: float | None) -> str:
    return "—" if x is None else (f"{x / 1000:.1f} s" if x >= 1000 else f"{x:.0f} ms")


def fmt(x: float | None, kind: str) -> str:
    if x is None:
        return "—"
    return {"rate": pct, "ms": ms}.get(kind, lambda v: f"{v:.2f}")(x)


def meets(value: float | None, threshold: float | None, higher_is_better: bool = True):
    if value is None or threshold is None:
        return None
    return value >= threshold if higher_is_better else value <= threshold


# The numbers a layer is judged by: card rows, deltas and trend charts all come from here.
# (label, getter over the aggregate, kind, threshold key or None, higher is better)
Metric = tuple[str, Callable[[dict[str, Any]], "float | None"], str, "str | None", bool]
METRICS: dict[str, list[Metric]] = {
    "L1": [
        ("Gold source in top 5", lambda a: a["hit_rate"], "rate", "l1_hit_rate", True),
        ("MRR", lambda a: a["mrr"], "score", "l1_mrr", True),
    ],
    "L2": [
        ("Behavior", lambda a: a["behavior_accuracy"], "rate", "behavior_accuracy", True),
        ("Correct", lambda a: a["correctness_pass_rate"], "rate", "correctness", True),
        ("Faithful", lambda a: a["faithfulness"], "score", "faithfulness", True),
        ("Speakable", lambda a: a["speakability_pass_rate"], "rate", "speakability", True),
    ],
    "L3": [
        ("Content pass", lambda a: a["attribution"]["content_pass_rate"], "rate", None, True),
        ("STT heard the question", lambda a: a["stt_equivalent_rate"], "rate", None, True),
        ("Time to first audio p50", lambda a: a["latency_ms"]["time_to_first_audio_p50"],
         "ms", "time_to_first_audio_ms", False),
    ],
}


def metric_values(layer: str, run: dict[str, Any]) -> list[float | None]:
    out = []
    for _, get, *_ in METRICS[layer]:
        try:
            out.append(get(run["aggregate"]))
        except (KeyError, TypeError):
            out.append(None)
    return out


def delta(cur: float | None, prev: float | None, kind: str, higher: bool) -> str:
    """The change since the previous run: arrow, signed amount, colored by direction."""
    if cur is None or prev is None:
        return ""
    d = cur - prev
    shown = {"rate": f"{abs(d) * 100:.0f} pts", "ms": ms(abs(d))}.get(kind, f"{abs(d):.2f}")
    if shown in ("0 pts", "0.00", "0 ms"):
        return '<span class="delta">= prev</span>'
    better = (d > 0) == higher
    return (f'<span class="delta {"ok" if better else "bad"}" title="change since the '
            f'previous run">{"▲" if d > 0 else "▼"} {shown}</span>')


Row = tuple[str, str, "bool | None", str]


def card(title: str, rows: list[Row]) -> str:
    body = "".join(
        f'<div class="row"><span>{e(k)}</span><span class="val">{d}<b class="'
        f'{"" if ok is None else ("ok" if ok else "bad")}">{v}</b></span></div>'
        for k, v, ok, d in rows)
    return f'<section class="card"><h3>{e(title)}</h3>{body}</section>'


def metric_rows(layer: str, run: dict[str, Any], prev: dict[str, Any] | None,
                t: dict[str, float]) -> list[Row]:
    cur_vals = metric_values(layer, run)
    prev_vals = metric_values(layer, prev) if prev else [None] * len(cur_vals)
    rows = []
    for (label, _, kind, tkey, higher), cur, old in zip(METRICS[layer], cur_vals, prev_vals,
                                                         strict=True):
        rows.append((label, fmt(cur, kind), meets(cur, threshold(layer, run, tkey, t), higher),
                     delta(cur, old, kind, higher)))
    return rows


def threshold(layer: str, run: dict[str, Any], key: str | None, t: dict[str, float]):
    if layer == "L3" and key == "time_to_first_audio_ms":
        return run["aggregate"]["latency_ms"].get("budget") or t.get(key)
    return t.get(key) if key else None


def tag_bars(summary: dict[str, Any]) -> str:
    counts = summary.get("by_primary_tag") or {}
    if not counts:
        return "<p class=muted>No failures.</p>"
    top = max(counts.values())
    return "".join(
        f'<div class="bar"><span class="tag">{e(t)}</span><span class="track">'
        f'<span style="width:{c / top * 100:.0f}%"></span></span><b>{c}</b></div>'
        for t, c in sorted(counts.items(), key=lambda kv: -kv[1]))


def tags_html(att: dict[str, Any] | None) -> str:
    if not att:
        return ""
    if att.get("passed"):
        return '<span class="pill ok">pass</span>'
    return " ".join(f'<span class="pill{" primary" if i == 0 else ""}">{e(t)}</span>'
                    for i, t in enumerate(att.get("tags", [])))


def verdicts_html(rec: dict[str, Any]) -> str:
    rows = []
    for name in ("stt", "policy", "correctness", "speakability"):
        v = rec.get(name)
        if isinstance(v, dict) and v.get("verdict"):
            rows.append(f"<li><b>{e(name)}</b>: {e(v['verdict'])} — {e(v.get('reason'))}</li>")
    for name in ("faithfulness", "answer_relevancy"):
        if rec.get(name) is not None:
            rows.append(f"<li><b>{e(name)}</b>: {rec[name]:.2f}</li>")
    return "<ul>" + "".join(rows) + "</ul>" if rows else ""


def sources_html(rec: dict[str, Any]) -> str:
    items = "".join(f"<li>{e(h['repo'])} <i>({e(h.get('origin'))})</i> {e(h['path'])} — "
                    f"{e(h.get('heading'))}</li>" for h in rec.get("retrieved") or [])
    q = "; ".join(e(q) for q in rec.get("queries") or [])
    return (f"<p><b>Agent searched:</b> {q or '—'}</p><ol>{items}</ol>" if items
            else f"<p><b>Agent searched:</b> {q or 'nothing'}</p>")


def copy_wavs(audio_dir: Path) -> AudioResolver:
    """The local report's resolver: copy each WAV next to the page."""
    def resolve(src: str) -> str | None:
        if not (ROOT / src).is_file():
            return None
        audio_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / src, audio_dir / Path(src).name)
        return f"audio/{Path(src).name}"
    return resolve


def audio_html(rec: dict[str, Any], audio: AudioResolver) -> str:
    out = []
    for side in ("caller", "reply"):
        src = (rec.get("audio") or {}).get(side)
        href = audio(src) if src else None
        if href:
            out.append(f'<label>{side}<audio controls preload="none" '
                       f'src="{e(href)}"></audio></label>')
    return f'<div class="audio">{"".join(out)}</div>' if out else ""


def l3_rows(run: dict[str, Any], audio: AudioResolver) -> str:
    rows = []
    for r in run["cases"]:
        if r.get("kind") != "turn":
            continue
        t = r.get("timing") or {}
        rows.append(f"""
<details class="case {'pass' if (r.get('attribution') or {}).get('passed') else 'fail'}">
<summary><span class="id">{e(r['id'])}</span><span class="said">{e(r.get('said'))}</span>
{tags_html(r.get('attribution'))}</summary>
<div class="body">
<p><b>Heard:</b> {e(r.get('transcript')) or '<i>nothing</i>'} <span class=muted>(WER {e((r.get('stt') or {}).get('wer'))})</span></p>
<p><b>Answered:</b> {e(r.get('spoken') or r.get('answer'))}</p>
{audio_html(r, audio)}
{verdicts_html(r)}
{sources_html(r)}
<p class=muted>Time to first audio {ms(t.get('time_to_first_audio_ms'))}: STT {ms(t.get('stt_ms'))},
agent {ms(t.get('agent_ms'))}, TTS {ms(t.get('tts_first_audio_ms'))}.</p>
</div></details>""")
    return "".join(rows)


def l2_rows(run: dict[str, Any]) -> str:
    rows = []
    for r in run["cases"]:
        rows.append(f"""
<details class="case {'pass' if r['attribution']['passed'] else 'fail'}">
<summary><span class="id">{e(r['id'])}</span><span class="said">{e(r.get('question'))}</span>
{tags_html(r['attribution'])}</summary>
<div class="body"><p><b>Answered:</b> {e(r.get('spoken') or r.get('answer'))}</p>
{verdicts_html(r)}{sources_html(r)}</div></details>""")
    return "".join(rows)


def prepare(layer: str, run: dict[str, Any], cases: dict[str, Any],
            t: dict[str, float]) -> dict[str, Any]:
    """Re-attribute with the current rules, so a rule change needs no re-run.

    Records whose golden case has since been removed keep their stored attribution."""
    if layer == "L2":
        for rec in run["cases"]:
            if rec["id"] in cases:
                rec["attribution"] = attribution.attribute(cases[rec["id"]], rec, t)
    elif layer == "L3":
        from lab.harness import l3

        budget = t.get("time_to_first_audio_ms")
        for rec in run["cases"]:
            if rec.get("kind") == "turn" and rec.get("case_id") in cases:
                rec["attribution"] = attribution.attribute(cases[rec["case_id"]], rec, t,
                                                           budget)
        run["aggregate"] = l3.aggregate(run["cases"], budget)
    return run


# --- trend charts: one small single-series chart per metric, inline SVG ------------------

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 240, 120, 38, 40, 12, 20


def utc(run: dict[str, Any]) -> str:
    return run["started_at"][:16].replace("T", " ") + " UTC"


def when(run: dict[str, Any]) -> str:
    return run["started_at"][5:16].replace("T", " ")


def trend_svg(layer: str, runs: list[dict[str, Any]], index: int,
              t: dict[str, float]) -> str:
    label, _, kind, tkey, _ = METRICS[layer][index]
    pts = [(r, metric_values(layer, r)[index]) for r in runs]
    pts = [(r, v) for r, v in pts if v is not None]
    if not pts:
        return ""
    limit = threshold(layer, runs[-1], tkey, t)
    if kind == "ms":
        top = max([v for _, v in pts] + [limit or 0]) * 1.1
        ticks = [0, top / 2, top]
    else:
        top, ticks = 1.0, [0, 0.5, 1.0]
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def x(i: int) -> float:
        return PAD_L + (iw / 2 if len(pts) == 1 else iw * i / (len(pts) - 1))

    def y(v: float) -> float:
        return PAD_T + ih * (1 - v / top)

    grid = "".join(
        f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
        f'<text class="axis" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">'
        f'{e(fmt(v, kind))}</text>' for v in ticks)
    target = ""
    if limit is not None and limit <= top:
        target = (f'<line class="target" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(limit):.1f}" '
                  f'y2="{y(limit):.1f}"/><text class="axis" x="{PAD_L + 4}" '
                  f'y="{y(limit) - 4:.1f}">target {e(fmt(limit, kind))}</text>')
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(pts))
    dots = []
    for i, (r, v) in enumerate(pts):
        n = r["aggregate"].get("cases", r["aggregate"].get("turns"))
        tip = (f"{label}: {fmt(v, kind)}\n{utc(r)}\n"
               f"commit {r['lab_commit']} · {n} cases")
        dots.append(f'<g class="pt"><title>{e(tip)}</title><circle class="hit" '
                    f'cx="{x(i):.1f}" cy="{y(v):.1f}" r="12"/><circle class="dot" '
                    f'cx="{x(i):.1f}" cy="{y(v):.1f}" r="4"/></g>')
    last_x, last_v = x(len(pts) - 1), pts[-1][1]
    end_label = (f'<text class="end" x="{last_x + 8:.1f}" y="{y(last_v) + 4:.1f}">'
                 f'{e(fmt(last_v, kind))}</text>')
    dates = (f'<text class="axis" x="{x(0):.1f}" y="{H - 4}" text-anchor="start">'
             f'{e(when(pts[0][0]))}</text>')
    if len(pts) > 1:
        dates += (f'<text class="axis" x="{last_x:.1f}" y="{H - 4}" text-anchor="end">'
                  f'{e(when(pts[-1][0]))}</text>')
    return f"""<figure class="trend"><figcaption>{e(label)}</figcaption>
<svg viewBox="0 0 {W} {H}" role="img" aria-label="{e(label)} over {len(pts)} runs">
{grid}{target}<polyline class="line" points="{line}"/>{''.join(dots)}{end_label}{dates}
</svg></figure>"""


def history_html(history: dict[str, list[dict[str, Any]]], t: dict[str, float]) -> str:
    blocks = []
    for layer in LAYERS:
        runs = history.get(layer) or []
        if not runs:
            continue
        charts = "".join(trend_svg(layer, runs, i, t) for i in range(len(METRICS[layer])))
        blocks.append(f'<h3>{layer} · {LAYER_NAMES[layer]} <span class="muted">'
                      f'({len(runs)} run{"s" if len(runs) != 1 else ""})</span></h3>'
                      f'<div class="trends">{charts}</div>')
    return (f"<section><h2>History</h2><p class=muted>Every published run, oldest to newest. "
            f"Hover a point for its date and commit; the table below lists the same runs."
            f"</p>{''.join(blocks)}</section>")


def commit_link(sha: str, repo_url: str | None) -> str:
    bare = sha.removesuffix("-dirty")
    if not repo_url or not bare:
        return f"<code>{e(sha)}</code>"
    return f'<a href="{e(repo_url)}/commit/{e(bare)}"><code>{e(sha)}</code></a>'


def runs_table(history: dict[str, list[dict[str, Any]]], repo_url: str | None) -> str:
    rows = []
    for layer in LAYERS:
        for r in history.get(layer) or []:
            a = r["aggregate"]
            n = a.get("cases", a.get("turns"))
            agent = (r.get("agent") or {}).get("model") or "—"
            cost = (r.get("judge_usage") or {}).get("usd")
            head = METRICS[layer][0]
            rows.append((r["started_at"], f"""<tr><td>{e(utc(r)[:-4])}</td>
<td>{layer}</td><td>{e(n)}</td><td>{commit_link(r['lab_commit'], repo_url)}</td>
<td>{e(agent)}</td><td>{e(head[0])} {fmt(metric_values(layer, r)[0], head[2])}</td>
<td>{'—' if cost is None else f'${cost:.2f}'}</td></tr>"""))
    body = "".join(h for _, h in sorted(rows, reverse=True))
    return f"""<section><h2>Runs</h2><div class="scroll"><table class="runs">
<tr><th>Started (UTC)</th><th>Layer</th><th>Cases</th><th>Lab commit</th><th>Agent</th>
<th>Headline</th><th>Judge cost</th></tr>{body}</table></div></section>"""


def corpus_html(run: dict[str, Any]) -> str:
    sources = (run.get("index") or {}).get("sources") or {}
    return ", ".join(f"{e(k)} <code>{e(v[:7])}</code>" for k, v in sources.items())


def build(cfg: LabConfig, out: Path = OUT,
          history: dict[str, list[dict[str, Any]]] | None = None,
          audio: AudioResolver | None = None, repo_url: str | None = None,
          footer: str = "") -> Path:
    """Render the report into `out`.

    history: runs per layer, oldest first; the newest one is drilled into. Defaults to
             each layer's results/<layer>/latest.json.
    audio:   maps a record's WAV path to an href; defaults to copying the WAVs to out/audio.
    """
    if history is None:
        history = {layer: [r] for layer in LAYERS
                   if (r := _load(ROOT / "results" / layer / "latest.json"))}
    cases = {c.id: c for c in golden.load()}
    t = cfg.thresholds
    for layer, runs in history.items():
        for r in runs:
            prepare(layer, r, cases, t)
    out.mkdir(parents=True, exist_ok=True)
    audio = audio or copy_wavs(out / "audio")
    latest = {layer: runs[-1] for layer, runs in history.items() if runs}
    prev = {layer: runs[-2] for layer, runs in history.items() if len(runs) > 1}
    cards, sections, meta = [], [], []

    if r1 := latest.get("L1"):
        a = r1["aggregate"]
        cards.append(card("L1 · retrieval", metric_rows("L1", r1, prev.get("L1"), t) + [
            ("Trap above gold", str(len(a["traps_above_gold"])), None, ""),
            ("Latency p95", ms(a["latency_ms"]["p95"]), meets(
                a["latency_ms"]["p95"], cfg.retrieval.p95_budget_ms, False), "")]))
        meta.append(f"L1 {e(utc(r1))} @ {commit_link(r1['lab_commit'], repo_url)}")

    if r2 := latest.get("L2"):
        cards.append(card("L2 · agent (text)", metric_rows("L2", r2, prev.get("L2"), t)))
        s2 = attribution.summarize(r2["cases"])
        sections.append(f"""<section><h2>L2 · text agent</h2>
<p>{s2['cases']} cases, {pct(s2['pass_rate'])} pass. Failures by primary cause:</p>
{tag_bars(s2)}<div class="cases">{l2_rows(r2)}</div></section>""")
        meta.append(f"L2 {e(utc(r2))} @ {commit_link(r2['lab_commit'], repo_url)}, "
                    f"agent {e(r2.get('agent', {}).get('model'))}, judge {e(r2.get('judge'))} "
                    f"(${(r2.get('judge_usage') or {}).get('usd', 0):.2f})")

    if r3 := latest.get("L3"):
        a = r3["aggregate"]
        lat = a["latency_ms"]
        rows = metric_rows("L3", r3, prev.get("L3"), t)
        rows.insert(1, ("Pass incl. latency", pct(a["attribution"]["pass_rate"]), None, ""))
        cards.append(card("L3 · voice", rows))
        personas = "".join(
            f"<tr><td>{e(p)}</td><td>{v['turns']}</td><td>{pct(v['content_pass_rate'])}</td>"
            f"<td>{e(v['stt_wer_mean'])}</td></tr>" for p, v in a["by_persona"].items())
        behaviors = "".join(
            f"<li class={'ok' if b['ok'] else 'bad'}><b>{e(b['scenario'])}</b>: "
            f"{e(b['detail'])}</li>" for b in a["behavior"])
        sections.insert(0, f"""<section><h2>L3 · voice</h2>
<p>{a['turns']} spoken turns over the websocket transport. Time to first audio
{ms(lat['time_to_first_audio_p50'])} at the median against a {ms(lat['budget'])} budget:
STT {ms(lat['stt_p50'])}, agent {ms(lat['agent_p50'])}, TTS {ms(lat['tts_first_audio_p50'])}.</p>
<h3>Failures by primary cause</h3>{tag_bars(a['attribution'])}
<div class="grid2"><div><h3>Personas</h3><table><tr><th>Persona</th><th>Turns</th>
<th>Content pass</th><th>WER</th></tr>{personas}</table></div>
<div><h3>Turn-taking</h3><ul class="behaviors">{behaviors}</ul></div></div>
<h3>Cases</h3><div class="cases">{l3_rows(r3, audio)}</div></section>""")
        v = r3.get("voice", {})
        meta.append(f"L3 {e(utc(r3))} @ {commit_link(r3['lab_commit'], repo_url)}, "
                    f"STT {e((v.get('stt') or {}).get('size'))}, TTS "
                    f"{e((v.get('tts') or {}).get('voice'))}, judge "
                    f"(${(r3.get('judge_usage') or {}).get('usd', 0):.2f})")

    newest = max(latest.values(), key=lambda r: r["started_at"], default=None)
    if newest and (corpus := corpus_html(newest)):
        meta.append(f"Corpus: {corpus}")
    if any(len(runs) > 1 for runs in history.values()):
        sections.append(history_html(history, t))
    if latest:
        sections.append(runs_table(history, repo_url))

    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>EFIS Voice Lab report</title><style>{CSS}</style></head><body><main>
<header><h1>EFIS Voice Lab</h1><p class=muted>A voice agent for the pyEfis docs, tested
layer by layer. Each failure is tagged with the layer that caused it.</p>
<p class="meta">{'<br>'.join(meta)}</p></header>
<div class="cards">{''.join(cards)}</div>
{''.join(sections)}
{f'<footer class="meta">{footer}</footer>' if footer else ''}
</main></body></html>"""
    path = out / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1f23;--muted:#6a6f78;--card:#fff;--line:#e4e2dc;--ok:#1f7a4d;
--bad:#b3261e;--accent:#2457a6;--pill:#eef0f4;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#15171b;--fg:#e8e6e1;--muted:#9aa0a8;
--card:#1d2026;--line:#2c3038;--ok:#5cc28d;--bad:#ef7b72;--accent:#7aa7ec;--pill:#262a32;
color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}
a{color:var(--accent)}
h1{margin:0 0 4px;font-size:28px}h2{margin:40px 0 8px;font-size:21px}
h3{margin:20px 0 8px;font-size:15px;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted)}.muted{color:var(--muted)}.meta{font-size:12px;color:var(--muted)}
footer.meta{margin-top:48px;border-top:1px solid var(--line);padding-top:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;
margin-top:20px}.card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px 14px}.card h3{margin:0 0 8px}
.row{display:flex;justify-content:space-between;gap:8px;padding:2px 0}
.val{display:flex;gap:8px;align-items:baseline;white-space:nowrap}
.delta{font-size:12px;color:var(--muted)}
.ok{color:var(--ok)}.bad{color:var(--bad)}
.bar{display:grid;grid-template-columns:220px 1fr 32px;align-items:center;gap:8px;
margin:4px 0}.track{background:var(--pill);border-radius:4px;height:10px;overflow:hidden}
.track span{display:block;height:100%;background:var(--accent)}
.tag{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:4px 8px;
border-bottom:1px solid var(--line)}.scroll{overflow-x:auto}
.runs td,.runs th{white-space:nowrap;font-size:13px}
.behaviors{padding-left:18px}
.trends{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.trend{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:8px 10px}.trend figcaption{font-size:13px;color:var(--muted)}
.trend svg{width:100%;height:auto;display:block;overflow:visible}
.trend .grid{stroke:var(--line);stroke-width:1}
.trend .target{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 3}
.trend .axis{fill:var(--muted);font-size:10px;paint-order:stroke;stroke:var(--card);stroke-width:3px}
.trend .end{fill:var(--fg);font-size:12px;font-weight:600;paint-order:stroke;stroke:var(--card);stroke-width:3px}
.trend .line{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}
.trend .dot{fill:var(--accent);stroke:var(--card);stroke-width:2}
.trend .hit{fill:transparent}.trend .pt:hover .dot{r:6}
.case{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:6px 0}
.case summary{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 12px;
cursor:pointer}.case .id{font-family:ui-monospace,Consolas,monospace;font-size:12px;
color:var(--muted);min-width:170px}.case .said{flex:1;min-width:200px}
.case .body{padding:0 12px 12px;border-top:1px solid var(--line)}
.pill{background:var(--pill);border-radius:999px;padding:1px 8px;font-size:11px;
font-family:ui-monospace,Consolas,monospace}.pill.primary{background:var(--bad);color:#fff}
.pill.ok{background:var(--ok);color:#fff}
.audio{display:flex;flex-wrap:wrap;gap:12px}.audio label{display:flex;flex-direction:column;
font-size:12px;color:var(--muted)}audio{max-width:100%}
@media (max-width:600px){.bar{grid-template-columns:140px 1fr 28px}}
"""
