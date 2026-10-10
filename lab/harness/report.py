"""Report v1: one static HTML page from the latest L1, L2 and L3 runs.

    uv run lab report            -> results/report/index.html (+ audio/ next to it)

Per-layer scores, failures by attribution tag, the persona sweep, behavior scenarios,
and a per-case drill-down: what the caller said, what STT heard, what the agent said,
the tags, the judges' reasons, the retrieved sources, timings, and both sides' audio.
L2 failures are attributed with the same rules as L3 (no STT, no latency). D1 publishes
this page; nothing in it needs a server.
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any

from lab.config import ROOT, LabConfig
from lab.harness import attribution, golden

OUT = ROOT / "results" / "report"


def _load(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def ms(x: float | None) -> str:
    return "—" if x is None else (f"{x / 1000:.1f} s" if x >= 1000 else f"{x:.0f} ms")


def card(title: str, rows: list[tuple[str, str, bool | None]]) -> str:
    body = "".join(
        f'<div class="row"><span>{e(k)}</span><b class="{"" if ok is None else ("ok" if ok else "bad")}">'
        f"{v}</b></div>" for k, v, ok in rows)
    return f'<section class="card"><h3>{e(title)}</h3>{body}</section>'


def meets(value: float | None, threshold: float | None, higher_is_better: bool = True):
    if value is None or threshold is None:
        return None
    return value >= threshold if higher_is_better else value <= threshold


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


def audio_html(rec: dict[str, Any], audio_dir: Path) -> str:
    out = []
    for side in ("caller", "reply"):
        src = (rec.get("audio") or {}).get(side)
        if src and (ROOT / src).is_file():
            dest = audio_dir / Path(src).name
            shutil.copyfile(ROOT / src, dest)
            out.append(f'<label>{side}<audio controls preload="none" '
                       f'src="audio/{e(dest.name)}"></audio></label>')
    return f'<div class="audio">{"".join(out)}</div>' if out else ""


def l3_rows(run: dict[str, Any], audio_dir: Path) -> str:
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
{audio_html(r, audio_dir)}
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


def build(cfg: LabConfig, out: Path = OUT) -> Path:
    runs = {layer: _load(ROOT / "results" / layer / "latest.json")
            for layer in ("L1", "L2", "L3")}
    cases = {c.id: c for c in golden.load()}
    t = cfg.thresholds
    out.mkdir(parents=True, exist_ok=True)
    audio_dir = out / "audio"
    audio_dir.mkdir(exist_ok=True)
    cards, sections, meta = [], [], []

    if runs["L1"]:
        a = runs["L1"]["aggregate"]
        cards.append(card("L1 · retrieval", [
            ("Gold source in top 5", pct(a["hit_rate"]), meets(a["hit_rate"],
                                                               t.get("l1_hit_rate"))),
            ("MRR", f"{a['mrr']:.2f}", meets(a["mrr"], t.get("l1_mrr"))),
            ("Trap above gold", str(len(a["traps_above_gold"])), None),
            ("Latency p95", ms(a["latency_ms"]["p95"]), meets(
                a["latency_ms"]["p95"], cfg.retrieval.p95_budget_ms, False))]))
        meta.append(f"L1 {e(runs['L1']['started_at'])} @ {e(runs['L1']['lab_commit'])}")

    if runs["L2"]:
        r2 = runs["L2"]
        for rec in r2["cases"]:
            rec["attribution"] = attribution.attribute(cases[rec["id"]], rec, t)
        a = r2["aggregate"]
        cards.append(card("L2 · agent (text)", [
            ("Behavior", pct(a["behavior_accuracy"]), meets(a["behavior_accuracy"],
                                                           t.get("behavior_accuracy"))),
            ("Correct", pct(a["correctness_pass_rate"]),
             meets(a["correctness_pass_rate"], t.get("correctness"))),
            ("Faithful", f"{a['faithfulness'] or 0:.2f}",
             meets(a["faithfulness"], t.get("faithfulness"))),
            ("Speakable", pct(a["speakability_pass_rate"]),
             meets(a["speakability_pass_rate"], t.get("speakability")))]))
        s2 = attribution.summarize(r2["cases"])
        sections.append(f"""<section><h2>L2 · text agent</h2>
<p>{s2['cases']} cases, {pct(s2['pass_rate'])} pass. Failures by primary cause:</p>
{tag_bars(s2)}<div class="cases">{l2_rows(r2)}</div></section>""")
        meta.append(f"L2 {e(r2['started_at'])} @ {e(r2['lab_commit'])}, agent "
                    f"{e(r2.get('agent', {}).get('model'))}, judge {e(r2.get('judge'))} "
                    f"(${(r2.get('judge_usage') or {}).get('usd', 0):.2f})")

    if runs["L3"]:
        from lab.harness import l3

        r3 = runs["L3"]
        budget = t.get("time_to_first_audio_ms")
        # Re-attribute with the current rules, so a rule change needs no re-run.
        for rec in r3["cases"]:
            if rec.get("kind") == "turn":
                rec["attribution"] = attribution.attribute(cases[rec["case_id"]], rec, t,
                                                           budget)
        r3["aggregate"] = l3.aggregate(r3["cases"], budget)
        a = r3["aggregate"]
        lat = a["latency_ms"]
        cards.append(card("L3 · voice", [
            ("Content pass", pct(a["attribution"]["content_pass_rate"]), None),
            ("Pass incl. latency", pct(a["attribution"]["pass_rate"]), None),
            ("STT heard the question", pct(a["stt_equivalent_rate"]), None),
            ("Time to first audio p50", ms(lat["time_to_first_audio_p50"]),
             meets(lat["time_to_first_audio_p50"], lat["budget"], False))]))
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
<h3>Cases</h3><div class="cases">{l3_rows(r3, audio_dir)}</div></section>""")
        v = r3.get("voice", {})
        meta.append(f"L3 {e(r3['started_at'])} @ {e(r3['lab_commit'])}, STT "
                    f"{e((v.get('stt') or {}).get('size'))}, TTS "
                    f"{e((v.get('tts') or {}).get('voice'))}, judge "
                    f"(${(r3.get('judge_usage') or {}).get('usd', 0):.2f})")

    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>EFIS Voice Lab report</title><style>{CSS}</style></head><body><main>
<header><h1>EFIS Voice Lab</h1><p class=muted>A voice agent for the pyEfis docs, tested
layer by layer. Each failure is tagged with the layer that caused it.</p>
<p class="meta">{'<br>'.join(meta)}</p></header>
<div class="cards">{''.join(cards)}</div>
{''.join(sections)}
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
h1{margin:0 0 4px;font-size:28px}h2{margin:40px 0 8px;font-size:21px}
h3{margin:20px 0 8px;font-size:15px;text-transform:uppercase;letter-spacing:.04em;
color:var(--muted)}.muted{color:var(--muted)}.meta{font-size:12px;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;
margin-top:20px}.card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:12px 14px}.card h3{margin:0 0 8px}
.row{display:flex;justify-content:space-between;gap:8px;padding:2px 0}
.ok{color:var(--ok)}.bad{color:var(--bad)}
.bar{display:grid;grid-template-columns:220px 1fr 32px;align-items:center;gap:8px;
margin:4px 0}.track{background:var(--pill);border-radius:4px;height:10px;overflow:hidden}
.track span{display:block;height:100%;background:var(--accent)}
.tag{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:4px 8px;
border-bottom:1px solid var(--line)}
.behaviors{padding-left:18px}
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
