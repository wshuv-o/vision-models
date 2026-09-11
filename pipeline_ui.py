"""Document pipeline UI - extract, match, validate, correct, export.

Left  : the pipeline (extract -> match -> tie-out -> review -> export)
Right : ask the local model about the document currently loaded

Corrections you make are persisted and reused for every future statement
from the same source system. That is the trial-and-correction loop.
"""
import os
import sys
import time
import importlib.util
from pathlib import Path

import gradio as gr
import pandas as pd

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pipeline_core", HERE / "pipeline_core.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)

OUT = HERE / "pipeline_out"
OUT.mkdir(exist_ok=True)

CANON = pc.load_canonical()
AUTO_CUT = 0.88


def llm_status():
    import requests
    try:
        r = requests.get(f"{pc.OLLAMA}/api/tags", timeout=4)
        names = [m["name"] for m in r.json().get("models", [])]
        cur = pc.LLM_MODEL
        return f"**LLM:** `{cur}` " + ("(loaded)" if cur in names else "(available)")
    except Exception:
        return "**LLM:** Ollama not reachable on 127.0.0.1:11434"


# ------------------------------------------------------------------ step 1+2
def run_pipeline(pdf_file, fuzzy_cut, progress=gr.Progress()):
    if pdf_file is None:
        return ("Upload a PDF first.", None, None, "", None, gr.update(choices=[]), "")
    t0 = time.perf_counter()
    path = str(pdf_file)
    progress(0.1, desc="Fingerprinting source...")
    profile = pc.source_profile(path)
    digital = pc.is_digital(path)

    progress(0.3, desc="Extracting...")
    sheets, method, conf, secs = pc.extract(path)
    if not sheets:
        msg = ("### No table extracted\n\n"
               + ("This PDF has **no text layer** - it is a scan. OCR is needed; "
                  "start the OCR server and use the OCR tab.\n" if not digital
                  else "A text layer exists but no numeric table was found.\n"))
        return msg, None, None, "", None, gr.update(choices=[]), profile

    sheet_name, df = sheets[0]
    progress(0.6, desc="Matching to canonical...")
    mem = pc.load_memory()
    m = pc.match_df(df, CANON, mem, profile, fuzzy_cut)

    progress(0.85, desc="Tie-out...")
    ok, tot, pct, fails = pc.tie_out(df)

    real = m[m.tier != "skip"]
    auto = int((real.confidence >= AUTO_CUT).sum())
    review = real[real.confidence < AUTO_CUT]
    learned = int((real.tier == "learned").sum())

    tie_txt = (f"{ok}/{tot} rows reconcile (**{pct}%**)" if tot
               else "no checkable rows (layout not recognised)")
    tie_icon = "OK" if (pct or 0) >= 95 else ("WARN" if tot else "N/A")
    status = (
        f"### {Path(path).name}\n"
        f"| step | result |\n|---|---|\n"
        f"| 1 · Extract | **{method}**, confidence {conf} · {len(df)} rows × "
        f"{len(df.columns)} cols · {secs}s |\n"
        f"| 2 · Match | **{auto}/{len(real)}** auto · {len(review)} need review"
        f"{f' · {learned} from memory' if learned else ''} |\n"
        f"| 3 · Tie-out | **{tie_icon}** — {tie_txt} |\n\n"
        f"_{int((m.tier=='skip').sum())} non-line-item rows skipped (headers, dates)._  \n"
        f"_source profile:_ `{profile[:70]}...`"
    )

    merged = df.copy()
    merged.insert(1, "→ canonical", m["canonical"].values)
    merged.insert(2, "conf", m["confidence"].values)

    choices = [f"{i} | {r.raw[:60]}" for i, r in review.iterrows()]
    ctx = df.head(80).to_string(max_colwidth=28)
    return (status, merged, fails if not fails.empty else None, profile,
            review.reset_index(), gr.update(choices=choices, value=None), ctx)


# ------------------------------------------------------------------ review
def load_candidates(sel, review_state):
    if not sel or review_state is None or len(review_state) == 0:
        return gr.update(choices=[], value=None), ""
    idx = int(str(sel).split("|")[0].strip())
    row = review_state[review_state["index"] == idx]
    if row.empty:
        return gr.update(choices=[], value=None), ""
    raw = row.iloc[0]["raw"]
    from rapidfuzz import fuzz, process as rfp
    cands = [c for c, _, _ in rfp.extract(raw, CANON, scorer=fuzz.WRatio, limit=12)]
    return gr.update(choices=cands, value=cands[0] if cands else None), f"**Source:** `{raw}`"


def ask_llm_for_match(sel, review_state):
    if not sel or review_state is None or len(review_state) == 0:
        return gr.update(), "Pick a row first."
    idx = int(str(sel).split("|")[0].strip())
    row = review_state[review_state["index"] == idx]
    if row.empty:
        return gr.update(), "Row not found."
    raw = row.iloc[0]["raw"]
    t = time.perf_counter()
    pick, conf = pc.llm_match(raw, CANON)
    dt = time.perf_counter() - t
    if not pick:
        return gr.update(), f"LLM found no confident match ({dt:.1f}s)."
    return gr.update(value=pick), f"LLM suggests **{pick}** ({dt:.1f}s)"


def save_correction(sel, chosen, profile, review_state):
    if not sel or not chosen:
        return "Pick a row and a canonical item first.", ""
    idx = int(str(sel).split("|")[0].strip())
    row = review_state[review_state["index"] == idx]
    if row.empty:
        return "Row not found.", ""
    raw = row.iloc[0]["raw"]
    mem = pc.load_memory()
    pc.remember(mem, profile, raw, chosen)
    n = len(mem.get("corrections", {}))
    return (f"Saved: `{raw}` → **{chosen}**\n\n"
            f"_Will auto-apply to every future statement from this source. "
            f"{n} correction(s) remembered._"), ""


def memory_view():
    mem = pc.load_memory()
    rows = [{"key": k.split("||")[1][:50], "→ canonical": v,
             "source profile": k.split("||")[0][:40]}
            for k, v in mem.get("corrections", {}).items()]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        {"info": ["No corrections yet - they appear here as you save them."]})


def export_xlsx(merged, fails, name="pipeline_export"):
    if merged is None or (hasattr(merged, "empty") and merged.empty):
        return None
    p = OUT / f"{name}.xlsx"
    with pd.ExcelWriter(p, engine="openpyxl") as xw:
        pd.DataFrame(merged).to_excel(xw, sheet_name="Mapped", index=False)
        if fails is not None and len(fails):
            pd.DataFrame(fails).to_excel(xw, sheet_name="TieOut_Failures", index=False)
        memory_view().to_excel(xw, sheet_name="Corrections", index=False)
    return str(p)


# ------------------------------------------------------------------ chat
def chat(msg, history, ctx, use_ctx):
    if not msg or not msg.strip():
        return history, ""
    answer, meta = pc.ask_llm(msg, ctx if use_ctx else "")
    history = (history or []) + [
        {"role": "user", "content": msg},
        {"role": "assistant", "content": answer + (f"\n\n_{meta}_" if meta else "")},
    ]
    return history, ""


# ------------------------------------------------------------------ layout
with gr.Blocks(title="Document Pipeline") as demo:
    gr.Markdown("# Financial document pipeline\nExtract → match → tie-out → correct → export. "
                "Everything runs on this machine.")
    st_profile = gr.State("")
    st_review = gr.State(None)
    st_ctx = gr.State("")

    with gr.Row():
        # ---------------- left: pipeline ----------------
        with gr.Column(scale=3):
            with gr.Row():
                pdf = gr.File(label="PDF", file_types=[".pdf"], type="filepath", scale=3)
                fuzzy = gr.Slider(70, 100, value=88, step=1, label="Auto-accept threshold", scale=1)
            go = gr.Button("Run pipeline", variant="primary")
            status = gr.Markdown()

            with gr.Tabs():
                with gr.Tab("Mapped data"):
                    table = gr.Dataframe(label="Extracted + canonical mapping", wrap=True)
                with gr.Tab("Tie-out failures"):
                    gr.Markdown("Rows whose arithmetic does not reconcile. "
                                "These indicate an extraction error, not a mapping error.")
                    tie_tbl = gr.Dataframe(wrap=True)
                with gr.Tab("Review queue"):
                    gr.Markdown("Low-confidence matches. Fix one and it is remembered "
                                "for every future statement from this source.")
                    pick = gr.Dropdown(label="Row needing review", choices=[])
                    src_lbl = gr.Markdown()
                    cand = gr.Dropdown(label="Canonical line item", choices=[])
                    with gr.Row():
                        ask_btn = gr.Button("Ask the model")
                        save_btn = gr.Button("Save correction", variant="primary")
                    save_msg = gr.Markdown()
                with gr.Tab("Memory"):
                    gr.Markdown("Every correction you have taught it.")
                    mem_tbl = gr.Dataframe(value=memory_view, wrap=True)
                    refresh_mem = gr.Button("Refresh", size="sm")

            with gr.Row():
                exp_btn = gr.Button("Export to Excel")
                xlsx = gr.File(label="Export")

        # ---------------- right: agent ----------------
        with gr.Column(scale=2):
            gr.Markdown("### Ask the agent")
            llm_md = gr.Markdown(llm_status())
            use_ctx = gr.Checkbox(value=True, label="Include the extracted document as context")
            chatbox = gr.Chatbot(height=430)
            msg = gr.Textbox(placeholder="why does row 47 not reconcile?", lines=2, label="")
            with gr.Row():
                send = gr.Button("Send", variant="primary")
                clear = gr.Button("Clear")

    go.click(run_pipeline, [pdf, fuzzy],
             [status, table, tie_tbl, st_profile, st_review, pick, st_ctx])
    pick.change(load_candidates, [pick, st_review], [cand, src_lbl])
    ask_btn.click(ask_llm_for_match, [pick, st_review], [cand, save_msg])
    save_btn.click(save_correction, [pick, cand, st_profile, st_review], [save_msg, src_lbl])
    refresh_mem.click(memory_view, None, mem_tbl)
    exp_btn.click(lambda m, f: export_xlsx(m, f), [table, tie_tbl], xlsx)
    send.click(chat, [msg, chatbox, st_ctx, use_ctx], [chatbox, msg])
    msg.submit(chat, [msg, chatbox, st_ctx, use_ctx], [chatbox, msg])
    clear.click(lambda: ([], ""), None, [chatbox, msg])

if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7862, inbrowser=False)
