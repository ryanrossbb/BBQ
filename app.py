"""
Quote Spreadsheeter — minimal Render-deployable backend (streaming version).

Uses the Anthropic SDK's streaming interface so long-running code-execution calls
don't hit Render's 300-second HTTP timeout.

POST /api/spreadsheet-quotes
  form fields:
    plans     — comma-separated plan list (e.g., "HMO Gold, PPO Silver, HDHP")
    documents — one or more files (carrier quotes + census)
  returns: populated .xlsx file as download

GET /  — minimal HTML form for manual testing.
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

BETA_HEADERS = {
    "anthropic-beta": "code-execution-2025-05-22,files-api-2025-04-14"
}
client = anthropic.Anthropic(default_headers=BETA_HEADERS)

SKILL_DIR = Path(__file__).parent / "skill"
SKILL_PROMPT = (SKILL_DIR / "SKILL.md").read_text()
TEMPLATE_PATH = SKILL_DIR / "assets" / "Prost11_Medical_Comparison.xlsx"
TEMPLATE_BYTES = TEMPLATE_PATH.read_bytes()

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
SHARED_SECRET = os.environ.get("SHARED_SECRET")


# ─── helpers ────────────────────────────────────────────────────────────────
def is_pdf(filename: str) -> bool:
    return filename.lower().endswith(".pdf")


def pdf_to_document_block(file_storage) -> dict:
    raw = file_storage.read()
    encoded = base64.standard_b64encode(raw).decode()
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": encoded,
        },
        "title": file_storage.filename,
    }


def upload_file_get_id(file_storage_or_bytes, filename: str) -> str:
    if hasattr(file_storage_or_bytes, "read"):
        data = file_storage_or_bytes.read()
    else:
        data = file_storage_or_bytes
    uploaded = client.beta.files.upload(file=(filename, io.BytesIO(data)))
    return uploaded.id


def container_upload_block(file_id: str) -> dict:
    return {"type": "container_upload", "file_id": file_id}


def extract_xlsx_from_message(final_message) -> bytes | None:
    """Walk the streamed final message for a generated .xlsx file."""
    for block in final_message.content:
        btype = getattr(block, "type", None)
        if btype != "code_execution_tool_result":
            continue
        result = getattr(block, "content", None)
        if not result:
            continue
        inner = getattr(result, "content", None) or []
        for item in inner:
            file_id = getattr(item, "file_id", None) or (
                item.get("file_id") if isinstance(item, dict) else None
            )
            if file_id:
                try:
                    file_bytes = client.beta.files.download(file_id).read()
                    if file_bytes[:2] == b"PK":
                        return file_bytes
                except Exception as e:
                    log.warning(f"failed to download file {file_id}: {e}")
    return None


def require_auth():
    if not SHARED_SECRET:
        return None
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
  status.textContent = "Working... this can take 3–8 minutes.";
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

    # Upload template via Files API
    log.info(f"[{run_id}] uploading template via Files API")
    template_id = upload_file_get_id(TEMPLATE_BYTES, "Prost11_Medical_Comparison_TEMPLATE.xlsx")

    # Build user content
    user_content = [
        {
            "type": "text",
            "text": (
                f"Spreadsheet these quotes for the following plans: {plans}.\n\n"
                "I've attached the BrokersBloc Medical Comparison template "
                "(filename ends in TEMPLATE.xlsx) — populate a copy of it. "
                "The other files are carrier quotes and the census. "
                "Use the code execution tool to read inputs and write the populated template, "
                "then make the resulting .xlsx file available as an output."
            ),
        },
        container_upload_block(template_id),
    ]

    for f in files:
        if is_pdf(f.filename):
            user_content.append(pdf_to_document_block(f))
        else:
            log.info(f"[{run_id}] uploading non-PDF via Files API: {f.filename}")
            fid = upload_file_get_id(f, f.filename)
            user_content.append(container_upload_block(fid))

    # Stream the response so the connection stays alive while Claude works.
    # The streaming SDK handles SSE under the hood and gives us a final assembled message.
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=8000,
            system=SKILL_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            tools=[{"type": "code_execution_20250522", "name": "code_execution"}],
        ) as stream:
            # Drain the event stream — this keeps bytes flowing on the wire so
            # Render's 300s idle timeout never trips, no matter how long Claude takes.
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", "") == "tool_use":
                        log.info(f"[{run_id}] tool use started: {getattr(block, 'name', '?')}")
                elif etype == "message_stop":
                    log.info(f"[{run_id}] message_stop received")
            final_message = stream.get_final_message()
    except anthropic.APIError as e:
        log.exception(f"[{run_id}] Claude API error during stream")
        return jsonify(error=f"Claude API error: {e}"), 502

    log.info(f"[{run_id}] stream complete, stop_reason={final_message.stop_reason}")

    xlsx_bytes = extract_xlsx_from_message(final_message)
    if not xlsx_bytes:
        text_blocks = [b.text for b in final_message.content if getattr(b, "type", None) == "text"]
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
