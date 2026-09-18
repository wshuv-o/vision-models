# Local document-extraction knowledge pack

This pack holds the knowledge and reference code for turning system-generated PDF reports into
verified Excel files, fully offline. It covers rent rolls, CAM recovery, aged receivables, tax
bills, comparables and utility statements.

## What's inside

| Path | What it is | How to use it |
|------|------------|---------------|
| `DOCUMENT_EXTRACTION_KNOWLEDGE.md` | The full knowledge base: SOP, techniques, validation, Excel house style, per-document playbooks, error catalogue, and architecture advice for the local system | Put **section 0** in the agent's system prompt. Index the other sections for retrieval (each section stands alone). |
| `rent-roll-extraction.SKILL.md` | The compact skill file used for every rent-roll-type task | Load it whenever the task mentions a rent roll, CAM, recovery, or aged receivables. It is the short form of the knowledge base. |
| `reference_scripts/` | 11 Python scripts and 1 PowerShell script that produced validated outputs | Reference designs. Copy and adapt them; don't run them blind on a new template (see below). |

## The one rule that matters most

The model never types numbers from a document into Excel. Python code reads the PDF tokens,
validates them against the report's own printed totals, and writes the cells. The model's job is to
understand layouts, write or adjust the parser, and fix failing checks.

## Reference scripts

| Script | Document type |
|--------|---------------|
| `cam_extract.py` | CAM / recovery calculation. Opens a folder picker and batch-processes every PDF in the folder. |
| `rentroll_extract.py` | MRI rent roll (ALT_CMROLL_1) |
| `property_perf_rr.py` | KBS Property Performance Report rent roll |
| `borrower_rr.py`, `verify.py` | Borrower rent roll (password-protected) plus its independent verifier |
| `rr05_extract.py` | MRI rent roll with Future Rent Increases by charge code (Source + Workings) |
| `receiver_report_rr.py` | Receiver-report rent roll (Source + Workings) |
| `st_render.py`, `ocr_batch.ps1`, `st_parse.py`, `st_rerender.py`, `st_build.py` | Scanned utility statements: render, Windows OCR, parse, multi-pass reconcile, build the workbook |

Before running any of them:

1. `pip install pdfplumber pypdfium2 openpyxl pandas pillow`
2. Edit the path constants at the top of the script. Every path now reads `C:\path\to\your\folder\...`.
3. For the password-protected PDF, set the password first with `set PDF_PASSWORD=...` (the script
   asks for it if you don't).
4. Column coordinates in each script belong to that template only. For a new template, follow the
   SOP in section 3 of the knowledge base: probe, dump the layout masked, re-measure, then validate.

`ocr_batch.ps1` uses the OCR engine built into Windows 10/11, so nothing leaves the machine. Run:
`powershell -ExecutionPolicy Bypass -File ocr_batch.ps1 -Dir "C:\...\page_images"`

## Confidentiality

- Example values in the knowledge base are synthetic.
- The script copies contain no passwords, no Windows username and no real tenant names.
- Each script's docstring and output file name still mention its property or report name, and
  `receiver_report_rr.py` uses the entity name as a totals marker. Review these before pushing the
  scripts to any shared or public repository.
