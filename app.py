"""
Quote Spreadsheeter — minimal Render-deployable backend.

POST /api/spreadsheet-quotes
  form fields:
    plans     — comma-separated plan list (e.g., "HMO Gold, PPO Silver, HDHP")
    documents — one or more files (carrier quotes + census)
  returns: populated .xlsx file as download

GET /  — returns a tiny HTML form for manual testing.
GET /healthz — health check for Render.
"""

import base64
import io
import logging
import os
import uuid
from pathlib import Path

import anthropic
from flask import Flask, request, send_file, jsonify, Response

# ─── setup ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("quote-spreadsheeter")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

SKILL_DIR = Path(__file__).parent / "skill"
SKILL_PROMPT = (SKILL_DIR / "SKILL.md").read_text()
TEMPLATE_PATH = SKILL_DIR / "assets" / "Prost11_Medical_Comparison.xlsx"
TEMPLATE_BYTES = TEMPLATE_PATH.read_bytes()  # cache once at startup

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
SHARED_SECRET = os.environ.get("SHARED_SECRET")  # set in Render env vars


# ─── helpers ────────────────────────────────────────────────────────────────
def media_type_for(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name.endswith(".xls"):
        return "application/vnd.ms-excel"
    if name.endswith(".csv"):
        return "text/csv"
    return "application/octet-stream"


def file_to_block(file_storage) -> dict:
    """Encode an uploaded file into an Anthropic content block."""
    raw = file_storage.read()
    encoded = base64.standard_b64encode(raw).decode()
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": media_type_for(file_storage.filename),
            "data": encoded,
        },
        "title": file_storage.filename,
    }


def template_as_block() -> dict:
    """The blank template, sent so Claude has something to populate."""
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "data": base64.standard_b64encode(TEMPLATE_BYTES).decode(),
        },
        "title": "Prost11_Medical_Comparison_TEMPLATE.xlsx",
    }


def extract_xlsx_from_response(response) -> bytes | None:
    """
    Walk the response content blocks looking for a generated .xlsx file.
    Code-execution tool returns files as code_execution_tool_result blocks
    containing file references with file_id we can fetch.
    """
    for block in response.content:
        # Code execution results contain file outputs
        if getattr(block, "type", None) == "code_execution_tool_result":
            result = getattr(block, "content", None)
            if result and getattr(result, "type", None) == "code_execution_result":
                for f in getattr(result, "content", []) or []:
                    if getattr(f, "type", None) == "code_execution_output":
                        file_id = getattr(f, "file_id", None)
                        if file_id:
                            file_bytes = client.beta.files.download(file_id).read()
                            # Heuristic: xlsx files start with PK (zip magic)
                            if file_bytes[:2] == b"PK":
                                return file_bytes
    return None


def require_auth():
    """Simple shared-secret check. Returns None if OK, or a Response if not."""
    if not SHARED_SECRET:
        return None  # auth disabled (dev mode)
    sent = request.headers.get("X-Shared-Secret") or request.form.get("secret")
    if sent != SHARED_SECRET:
        return Response("Unauthorized", status=401)
    return None


# ─── routes ─────────────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return jsonify(status="ok", model=MODEL)


@app.get("/")
def index():
    """Minimal HTML form for manual testing. Replace with your real frontend."""
    return """<!doctype html>
<html><head><title>Quote Spreadsheeter — Test</title>
<style>body{font:14px system-ui;max-width:560px;margin:40px auto;padding:0 16px}
label{display:block;margin:16px 0 4px;font-weight:600}
textarea,input{width:100%;padding:8px;font:inherit;box-sizing:border-box}
button{margin-top:20px;padding:10px 20px;font:inherit;cursor:pointer}
#status{margin-top:20px;color:#555}</style></head>
<body><h1>Quote Spreadsheeter</h1>
<form id="f">
  <label>Plans to include (comma-separated)</label>
  <textarea name="plans" rows="2" placeholder="HMO Gold, PPO Silver, HDHP" required></textarea>
  <label>Carrier quotes + census file</label>
  <input type="file" name="documents" multiple required>
  <label>Shared secret (if required)</label>
  <input type="password" name="secret" placeholder="leave blank if not set">
  <button>Generate Spreadsheet</button>
</form>
<div id="status"></div>
<script>
document.getElementById("f").onsubmit = async (e) => {
  e.preventDefault();
  const status = document.getElementById("status");
  status.textContent = "Working... this can take 1–3 minutes.";
  try {
    const res = await fetch("/api/spreadsheet-quotes", {method:"POST", body:new FormData(e.target)});
    if (!res.ok) { status.textContent = "Error: " + (await res.text()); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement("a"),
      {href:url, download:"Medical_Comparison.xlsx"});
    a.click();
    status.textContent = "Done — file downloaded.";
  } catch (err) { status.textContent = "Error: " + err.message; }
};
</script></body></html>"""


@app.post("/api/spreadsheet-quotes")
def spreadsheet_quotes():
    auth_err = require_auth()
    if auth_err:
        return auth_err

    plans = request.form.get("plans", "").strip()
    files = request.files.getlist("documents")

    if not plans:
        return jsonify(error="Missing 'plans' field"), 400
    if not files:
        return jsonify(error="No files uploaded"), 400

    run_id = uuid.uuid4().hex[:8]
    log.info(f"[{run_id}] starting run: {len(files)} files, plans={plans!r}")

    # Build the user message: instructions + template + uploaded files
    user_content = [
        {
            "type": "text",
            "text": (
                f"Spreadsheet these quotes for the following plans: {plans}.\n\n"
                "I've attached the BrokersBloc Medical Comparison template "
                "(filename ends in TEMPLATE.xlsx) — populate a copy of it. "
                "The other files are carrier quotes and the census."
            ),
        },
        template_as_block(),
    ]
    for f in files:
        user_content.append(file_to_block(f))

    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SKILL_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            tools=[{"type": "code_execution_20250522", "name": "code_execution"}],
            betas=["code-execution-2025-05-22", "files-api-2025-04-14"],
        )
    except anthropic.APIError as e:
        log.exception(f"[{run_id}] Claude API error")
        return jsonify(error=f"Claude API error: {e}"), 502

    log.info(f"[{run_id}] response received, stop_reason={response.stop_reason}")

    xlsx_bytes = extract_xlsx_from_response(response)
    if not xlsx_bytes:
        # Surface Claude's text reply so caller can see what happened
        text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        msg = "\n".join(text_blocks) or "No file produced and no text response."
        log.warning(f"[{run_id}] no xlsx produced. Claude said: {msg[:500]}")
        return jsonify(error="No spreadsheet was produced.", claude_response=msg), 422

    log.info(f"[{run_id}] returning xlsx, {len(xlsx_bytes)} bytes")
    return send_file(
        io.BytesIO(xlsx_bytes),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"Medical_Comparison_{run_id}.xlsx",
    )


if __name__ == "__main__":
    # Local dev only. In production, gunicorn runs the app.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
