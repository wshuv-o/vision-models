"""Lease abstraction: a template workbook plus a pile of PDFs, one row filled
per tenant.

Upload the workbook and the documents. Nothing is assigned to a tenant up
front -- every file is searched for every row, and the tenant's suite and DBA
decide what the model sees. You get the same workbook back with the grid
filled in, plus a Sources sheet saying where each value came from.

    ./.venv/Scripts/python lease_ui.py      # http://127.0.0.1:7864
"""
import os
import time

import gradio as gr
import pandas as pd

import doc_text
import group_extract as GE
import lease_extract as LE
import lease_template as LT
from extract_ui import MODELS, free_ocr_server, gpu_used_mb, vllm_running, wsl_running

_here = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_here, "outputs")


def _paths(file_obj):
    if not file_obj:
        return []
    items = file_obj if isinstance(file_obj, (list, tuple)) else [file_obj]
    return [str(getattr(i, "name", i)) for i in items if i]


def _read_ocr_cache(path):
    """Chunks from a document converted on an earlier run."""
    import save_output
    raw = GE.cache_path(path).read_text(encoding="utf-8")
    return save_output.split_pages(raw)


def inspect(template, docs):
    """Read the template and the documents. Loads no model, touches no GPU."""
    tpath = _paths(template)
    if not tpath:
        return "Upload the template workbook first.", pd.DataFrame(), pd.DataFrame()
    try:
        columns, tenants, meta = LT.read_template(tpath[0])
    except Exception as e:
        return f"Could not read that workbook: {type(e).__name__}: {e}", \
               pd.DataFrame(), pd.DataFrame()

    fill = LT.fillable_columns(columns, tenants)
    secs = LE.sections_for(fill)
    lines = [f"{len(columns)} column(s), {len(fill)} to fill, "
             f"{len(tenants)} tenant row(s)",
             "sections: " + ", ".join(f"{n}({len(c)})" for n, c in secs), ""]

    trows = [{"row": t["_row"] + 1, "tenant": LT.describe_tenant(t)} for t in tenants]

    drows, unread = [], []
    for p in _paths(docs):
        name = os.path.basename(p)
        try:
            units = doc_text.read(p)
            chars = sum(len(t) for _, t in units)
            drows.append({"document": name, "parts": len(units), "chars": chars})
        except doc_text.NeedsOCR:
            if GE.cache_path(p).exists():
                units = _read_ocr_cache(p)
                drows.append({"document": name + "  [OCR'd earlier]",
                              "parts": len(units),
                              "chars": sum(len(t) for _, t in units)})
                continue
            try:
                import pdf_pages
                pages = pdf_pages.page_count(p)
                unread.append(f"{name} (scanned, {pages} pages, "
                              f"~{pages * 2.8:.0f}s to OCR)")
            except Exception:
                unread.append(f"{name} (scanned - needs OCR)")
        except Exception as e:
            unread.append(f"{name} ({type(e).__name__})")
    lines.append(f"{len(drows)} document(s) readable")
    if unread:
        lines.append("Words are pixels - tick 'OCR scanned documents' to read "
                     "these, otherwise they are ignored:")
        lines += ["  " + u for u in unread]
    return "\n".join(lines), pd.DataFrame(trows), pd.DataFrame(drows)


def run(template, docs, model, char_budget, num_ctx, second_pass=True,
        allow_ocr=False, progress=gr.Progress()):
    log, empty = [], pd.DataFrame()

    def status(m):
        log.append(f"[{time.strftime('%H:%M:%S')}] {m}")
        return "\n".join(log)

    tpath, dpaths = _paths(template), _paths(docs)
    if not tpath:
        yield "Upload the template workbook first.", empty, None
        return
    if not dpaths:
        yield "Upload at least one PDF.", empty, None
        return

    try:
        columns, tenants, meta = LT.read_template(tpath[0])
    except Exception as e:
        yield f"Could not read that workbook: {type(e).__name__}: {e}", empty, None
        return
    if not tenants:
        yield "No tenant rows found under the header.", empty, None
        return

    if vllm_running() or wsl_running():
        yield status(f"stage 1 holds the GPU ({gpu_used_mb()}MB); freeing"), empty, None
        before, after = free_ocr_server()
        yield status(f"GPU freed: {before}MB -> {after}MB"), empty, None

    # Anything whose words are pixels is converted first, in one batch, and
    # the GPU is handed back before the language model is asked for anything --
    # vLLM and Ollama cannot both be resident on a 16GB card.
    need_ocr = []
    for p in dpaths:
        try:
            doc_text.read(p)
        except doc_text.NeedsOCR:
            if not GE.cache_path(p).exists():
                need_ocr.append(p)
        except Exception:
            pass

    if need_ocr and allow_ocr:
        import pdf_pages
        recs = []
        for p in need_ocr:
            try:
                pages = pdf_pages.page_count(p)
            except Exception:
                pages = 1
            recs.append({"path": p, "pages": pages, "route": "ocr"})
        total = sum(r["pages"] for r in recs)
        yield (status(f"OCR: {len(recs)} scanned document(s), {total} page(s), "
                      f"~{total * 2.8:.0f}s - starting PaddleOCR-VL"), empty, None)
        try:
            done, declined = GE.run_ocr_phase(
                recs, log=lambda m: print(m, flush=True),
                confirm=lambda rec: True, page_limit=10 ** 6)
            # run_ocr_phase only pkills vllm, and WSL2 keeps holding the card
            # until the distro itself stops -- which Windows nvidia-smi does
            # not show. Skipping this cost five of eight sections to HTTP 500
            # while gpt-oss failed to allocate.
            before, after = free_ocr_server()
            yield (status(f"OCR finished: {len(done)} converted. "
                          f"GPU released: {before}MB -> {after}MB"), empty, None)
        except Exception as e:
            yield status(f"OCR failed: {type(e).__name__}: {e}"), empty, None
    elif need_ocr:
        yield (status(f"{len(need_ocr)} scanned document(s) will be ignored - "
                      f"tick 'OCR scanned documents' to read them"), empty, None)

    files, skipped = [], []
    for p in dpaths:
        try:
            units = doc_text.read(p)
            if units:
                files.append((p, units))
        except doc_text.NeedsOCR:
            if GE.cache_path(p).exists():
                units = _read_ocr_cache(p)
                if units:
                    files.append((p, units))
                    continue
            skipped.append(f"{os.path.basename(p)} (scanned)")
        except Exception as e:
            skipped.append(f"{os.path.basename(p)} ({type(e).__name__})")
    yield (status(f"{len(files)} readable document(s), {len(tenants)} tenant(s)"
                  + (f"; ignoring {len(skipped)}: {', '.join(skipped)}" if skipped else "")),
           empty, None)
    if not files:
        yield status("nothing readable - are these scanned PDFs?"), empty, None
        return

    msgs, results = [], []

    def log_cb(m):
        print(m, flush=True)        # also to the server log, for diagnosis
        msgs.append(m)

    t0 = time.time()
    for rec in LE.run_tenants(files, columns, tenants, model=model,
                              char_budget=int(char_budget), num_ctx=int(num_ctx),
                              log=log_cb, second_pass=bool(second_pass)):
        results.append(rec)
        while msgs:
            status(msgs.pop(0))
        progress(len(results) / max(len(tenants), 1))
        flat = [{"tenant": r["_label"], "filled": len(r["values"]),
                 **{k: v for k, v in list(r["values"].items())[:8]}}
                for r in results]
        yield "\n".join(log), pd.DataFrame(flat), None

    while msgs:
        status(msgs.pop(0))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR,
                       f"lease_filled_{time.strftime('%Y%m%d_%H%M%S')}.xlsx")
    try:
        n = LE.fill_workbook(tpath[0], out, columns, results)
        status(f"wrote {n} value(s) into {os.path.basename(out)} (+ Sources sheet)")
    except Exception as e:
        status(f"could not write the workbook: {type(e).__name__}: {e}")
        out = None

    el = time.time() - t0
    flat = [{"tenant": r["_label"], "filled": len(r["values"]),
             **{k: v for k, v in list(r["values"].items())[:8]}} for r in results]
    yield (status(f"DONE: {len(results)} tenant(s) in {el:.1f}s "
                  f"({el/max(len(results),1):.1f}s each)"),
           pd.DataFrame(flat), out)


def _serve_opts():
    host = os.environ.get("VM_HOST", "127.0.0.1")
    user, password = os.environ.get("VM_USER"), os.environ.get("VM_PASS")
    auth = (user, password) if user and password else None
    share = os.environ.get("VM_SHARE", "").lower() in ("1", "true", "yes")
    if host != "127.0.0.1" and not auth:
        print(f"WARNING: listening on {host} with no VM_USER/VM_PASS", flush=True)
    if share and not auth:
        print("REFUSING a public link with no password", flush=True)
        share = False
    return host, auth, share


with gr.Blocks(title="Lease abstraction") as demo:
    gr.Markdown(
        "# Lease abstraction\n"
        "Upload your **template workbook** and the **PDFs**. Every document is "
        "searched for every tenant - nothing needs organising first - and you "
        "get the workbook back with one row filled per tenant, plus a Sources "
        "sheet recording where each value came from.\n\n"
        "Runs are **reproducible** - fixed seed and temperature, so the same "
        "documents give the same answers every time. The **second pass** "
        "re-asks only the fields that came back empty, over pages the first "
        "pass did not use."
    )
    with gr.Row():
        with gr.Column(scale=1):
            tpl_in = gr.File(label="Template workbook (.xlsx)",
                             file_types=[".xlsx", ".xlsm"])
            docs_in = gr.File(label="Lease documents (PDF / DOCX)",
                              file_count="multiple",
                              file_types=[".pdf", ".docx", ".xlsx", ".txt"])
            model_in = gr.Dropdown(choices=MODELS, value="gpt-oss:20b",
                                   label="Model")
            with gr.Row():
                budget_in = gr.Slider(8000, 60000, value=30000, step=2000,
                                      label="Characters per section")
                ctx_in = gr.Slider(8192, 65536, value=16384, step=4096,
                                   label="Context tokens")
            ocr_in = gr.Checkbox(
                value=False,
                label="OCR scanned documents (PaddleOCR-VL, ~2.8s a page)")
            pass2_in = gr.Checkbox(
                value=True,
                label="Second pass - re-ask only the fields that came back "
                      "empty, over different pages")
            inspect_btn = gr.Button("1. Inspect")
            run_btn = gr.Button("2. Run", variant="primary")
            file_out = gr.File(label="Filled workbook")
        with gr.Column(scale=2):
            status_out = gr.Textbox(label="Status", lines=16, max_lines=16,
                                    autoscroll=True)
            tenants_out = gr.Dataframe(label="Tenant rows found", wrap=True)
            docs_out = gr.Dataframe(label="Documents read", wrap=True)

    inspect_btn.click(inspect, inputs=[tpl_in, docs_in],
                      outputs=[status_out, tenants_out, docs_out])
    run_btn.click(run, inputs=[tpl_in, docs_in, model_in, budget_in, ctx_in,
                               pass2_in, ocr_in],
                  outputs=[status_out, tenants_out, file_out])

if __name__ == "__main__":
    _host, _auth, _share = _serve_opts()
    demo.queue().launch(server_name=_host, server_port=7864,
                        inbrowser=(_host == "127.0.0.1"), auth=_auth,
                        share=_share)
