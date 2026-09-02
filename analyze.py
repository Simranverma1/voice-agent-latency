"""Turn logs/bench.jsonl into the numbers and charts your report needs.

    python analyze.py
    python analyze.py --log live.jsonl

Outputs to reports/:
    summary.md          markdown tables (paste straight into your writeup)
    per_turn.csv        raw data
    breakdown.png       stacked critical-path bars per profile
    distribution.png    e2e latency distribution per profile
"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
LOGS, REPORTS = ROOT / "logs", ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

METRICS = [
    ("eot_detection_ms", "End-of-speech detection"),
    ("stt_ms", "Speech-to-text"),
    ("llm_ttft_ms", "LLM time-to-first-token"),
    ("llm_total_ms", "LLM total response"),
    ("tts_ttfa_ms", "TTS time-to-first-audio"),
    ("e2e_ms", "END-TO-END (perceived)"),
]

CP = ["1_eot_detection", "2_stt_finalize", "3_llm_dispatch", "4_llm_ttft",
      "5_text_buffering", "6_tts_ttfa", "7_audio_to_speaker"]


def load(logfile: str) -> pd.DataFrame:
    path = LOGS / logfile
    if not path.exists():
        raise SystemExit(f"No log at {path}. Run benchmark.py first.")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        row = {"profile": r["profile"], "prompt_id": r["prompt_id"],
               "turn_id": r["turn_id"], "errors": ";".join(r["errors"])}
        row.update(r["metrics"])
        row.update(r["critical_path"])
        row.update({k: v for k, v in r["meta"].items()
                    if k in ("reply_chars", "audio_ms", "user_text")})
        rows.append(row)
    df = pd.DataFrame(rows)
    return df[df["e2e_ms"].notna()].copy()


def pct(s: pd.Series, q: float):
    s = s.dropna()
    return float(np.percentile(s, q)) if len(s) else float("nan")


def summarize(df: pd.DataFrame) -> str:
    order = [p for p in df["profile"].unique()]
    out = ["# Latency Report\n", f"Turns analysed: **{len(df)}** across "
           f"**{len(order)}** profile(s)\n"]

    out.append("\n## Headline: median and p95 response latency\n")
    out.append("| Profile | n | median e2e | p95 e2e | mean | min | max |")
    out.append("|---|---|---|---|---|---|---|")
    for p in order:
        d = df[df.profile == p]["e2e_ms"]
        out.append(f"| `{p}` | {len(d)} | **{d.median():.0f} ms** | "
                   f"**{pct(d,95):.0f} ms** | {d.mean():.0f} | {d.min():.0f} | {d.max():.0f} |")

    out.append("\n## Per-stage latency (median / p95, ms)\n")
    header = "| Stage | " + " | ".join(f"`{p}`" for p in order) + " |"
    out.append(header)
    out.append("|---" * (len(order) + 1) + "|")
    for key, label in METRICS:
        cells = []
        for p in order:
            d = df[df.profile == p][key].dropna()
            cells.append(f"{d.median():.0f} / {pct(d,95):.0f}" if len(d) else "n/a")
        out.append(f"| {label} | " + " | ".join(cells) + " |")

    out.append("\n## Where the time goes (critical path, median ms)\n")
    out.append("These segments are contiguous and non-overlapping, so they sum to e2e.\n")
    out.append("| Segment | " + " | ".join(f"`{p}`" for p in order) + " |")
    out.append("|---" * (len(order) + 1) + "|")
    for seg in CP:
        cells = []
        for p in order:
            d = df[df.profile == p][seg].dropna()
            cells.append(f"{d.median():.0f}" if len(d) else "n/a")
        out.append(f"| {seg.split('_',1)[1].replace('_',' ')} | " + " | ".join(cells) + " |")
    totals = [f"**{df[df.profile==p][CP].median().sum():.0f}**" for p in order]
    out.append("| **total** | " + " | ".join(totals) + " |")

    if len(order) >= 2:
        a, b = order[0], order[-1]
        ma = df[df.profile == a]["e2e_ms"].median()
        mb = df[df.profile == b]["e2e_ms"].median()
        pa, pb = pct(df[df.profile == a]["e2e_ms"], 95), pct(df[df.profile == b]["e2e_ms"], 95)
        out.append(f"\n## Before vs after\n")
        out.append(f"| | `{a}` (before) | `{b}` (after) | change |")
        out.append("|---|---|---|---|")
        out.append(f"| median | {ma:.0f} ms | {mb:.0f} ms | "
                   f"**-{ma-mb:.0f} ms ({(1-mb/ma)*100:.0f}% faster)** |")
        out.append(f"| p95 | {pa:.0f} ms | {pb:.0f} ms | "
                   f"**-{pa-pb:.0f} ms ({(1-pb/pa)*100:.0f}% faster)** |")

    return "\n".join(out) + "\n"


def chart_breakdown(df: pd.DataFrame):
    order = list(df["profile"].unique())
    med = {p: df[df.profile == p][CP].median() for p in order}
    fig, ax = plt.subplots(figsize=(10, 0.9 * len(order) + 2.5))
    bottom = np.zeros(len(order))
    colors = plt.cm.viridis(np.linspace(0.15, 0.9, len(CP)))
    for c, seg in zip(colors, CP):
        vals = np.array([med[p].get(seg, 0) or 0 for p in order], dtype=float)
        ax.barh(order, vals, left=bottom, label=seg.split("_", 1)[1].replace("_", " "),
                color=c, edgecolor="white")
        bottom += vals
    for i, p in enumerate(order):
        ax.text(bottom[i] + 20, i, f"{bottom[i]:.0f} ms", va="center", fontsize=9)
    ax.set_xlabel("milliseconds from end of user speech")
    ax.set_title("Critical path breakdown (median)")
    ax.legend(loc="lower right", fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(REPORTS / "breakdown.png", dpi=150)
    plt.close(fig)


def chart_distribution(df: pd.DataFrame):
    order = list(df["profile"].unique())
    fig, ax = plt.subplots(figsize=(9, 5))
    data = [df[df.profile == p]["e2e_ms"].dropna().values for p in order]
    ax.boxplot(data, tick_labels=order, vert=True, showfliers=True)
    for i, d in enumerate(data, 1):
        ax.scatter(np.random.normal(i, 0.05, len(d)), d, alpha=0.5, s=18)
        ax.annotate(f"p95 {np.percentile(d,95):.0f}", (i, np.percentile(d, 95)),
                    textcoords="offset points", xytext=(12, 0), fontsize=8)
    ax.set_ylabel("end-to-end latency (ms)")
    ax.set_title("Perceived response latency distribution")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORTS / "distribution.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="bench.jsonl")
    a = ap.parse_args()
    df = load(a.log)
    md = summarize(df)
    (REPORTS / "summary.md").write_text(md, encoding="utf-8")
    df.to_csv(REPORTS / "per_turn.csv", index=False)
    chart_breakdown(df)
    chart_distribution(df)
    print(md)
    print(f"\nWrote reports/summary.md, per_turn.csv, breakdown.png, distribution.png")
