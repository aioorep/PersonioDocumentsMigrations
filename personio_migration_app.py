#!/usr/bin/env python3
"""
Personio Document Migration Web App
Run with: python3 personio_migration_app.py
Then open: http://localhost:5050
"""

import json
import queue
import tempfile
import threading
from pathlib import Path

import os
import secrets
import requests
from flask import Flask, Response, jsonify, render_template_string, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ── In-memory session state (single-user local app) ──────────────────────────
state = {}

# ── Personio API helpers ──────────────────────────────────────────────────────

BASE_URL = "https://api.personio.de/v1"


def authenticate(client_id, client_secret):
    resp = requests.post(
        f"{BASE_URL}/auth",
        json={"client_id": client_id, "client_secret": client_secret},
        timeout=15,
    )
    if not resp.ok:
        raise ValueError(f"HTTP {resp.status_code}: {resp.text}")
    data = resp.json()
    if not data.get("success"):
        raise ValueError(data.get("error", {}).get("message", "Authentication failed"))
    return data["data"]["token"]


def get_employees(token):
    employees, offset, limit = [], 0, 200
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        resp = requests.get(
            f"{BASE_URL}/company/employees",
            headers=headers,
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            break
        employees.extend(data)
        if len(data) < limit:
            break
        offset += limit
    return employees


def extract_info(emp):
    attrs = emp.get("attributes", {})

    def val(f):
        v = attrs.get(f, {})
        return (v.get("value") if isinstance(v, dict) else v) or ""

    email = val("email").strip().lower()
    first = val("first_name").strip()
    last  = val("last_name").strip()
    return {
        "id":        emp.get("id") or val("id"),
        "email":     email,
        "full_name": f"{first.lower()} {last.lower()}".strip(),
        "display":   f"{first} {last}".strip(),
    }


def get_documents(token, employee_id):
    headers = {"Authorization": f"Bearer {token}"}

    # First try: dedicated documents endpoint
    resp = requests.get(
        f"{BASE_URL}/company/employees/{employee_id}/documents",
        headers=headers,
        timeout=30,
    )
    if resp.ok:
        return resp.json().get("data", [])

    # Second try: fetch employee with ?includes=documents
    resp2 = requests.get(
        f"{BASE_URL}/company/employees/{employee_id}",
        headers=headers,
        params={"includes": "documents"},
        timeout=30,
    )
    if resp2.ok:
        data = resp2.json().get("data", {})
        attrs = data.get("attributes", {})
        # Documents may be under attributes.documents or a nested key
        docs = attrs.get("documents", {})
        if isinstance(docs, dict):
            return docs.get("value", [])
        if isinstance(docs, list):
            return docs
        return []

    raise ValueError(f"HTTP {resp.status_code}: {resp.text}")


def download_document(token, document, dest_dir):
    attrs    = document.get("attributes", {})
    file_info = attrs.get("file", {})
    file_url  = file_info.get("url") if isinstance(file_info, dict) else None
    doc_id    = document.get("id")
    if not file_url:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(file_url, headers=headers, stream=True, timeout=60)
    if not resp.ok:
        return None
    filename   = attrs.get("file_name") or f"document_{doc_id}"
    local_path = dest_dir / filename
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)
    return local_path


def upload_document(token, employee_id, local_path, document):
    headers    = {"Authorization": f"Bearer {token}"}
    attrs      = document.get("attributes", {})
    category   = attrs.get("category", {})
    category_id = category.get("id") if isinstance(category, dict) else None
    with open(local_path, "rb") as f:
        files = {"file": (local_path.name, f)}
        data  = {}
        if category_id:
            data["category_id"] = str(category_id)
        resp = requests.post(
            f"{BASE_URL}/company/employees/{employee_id}/documents",
            headers=headers,
            files=files,
            data=data,
            timeout=60,
        )
    return resp.ok


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/saved-credentials", methods=["GET"])
def saved_credentials():
    """Return previously saved credentials from the session (secrets masked)."""
    return jsonify({
        "src_client_id":     session.get("src_client_id", ""),
        "src_client_secret": "••••••••" if session.get("src_client_secret") else "",
        "tgt_client_id":     session.get("tgt_client_id", ""),
        "tgt_client_secret": "••••••••" if session.get("tgt_client_secret") else "",
        "emails":            session.get("emails", ""),
        "has_secrets":       bool(session.get("src_client_secret")),
    })


@app.route("/api/preflight", methods=["POST"])
def preflight():
    body = request.json
    src_id     = body.get("src_client_id", "").strip()
    src_secret = body.get("src_client_secret", "").strip()
    tgt_id     = body.get("tgt_client_id", "").strip()
    tgt_secret = body.get("tgt_client_secret", "").strip()
    raw_emails = body.get("emails", "")

    # If user left secret fields as masked placeholder, reuse saved ones
    if src_secret == "••••••••":
        src_secret = session.get("src_client_secret", "")
    if tgt_secret == "••••••••":
        tgt_secret = session.get("tgt_client_secret", "")

    # Parse email list
    target_emails = {
        e.strip().lower()
        for line in raw_emails.replace(",", "\n").splitlines()
        for e in [line.strip()]
        if e and "@" in e
    }
    if not target_emails:
        return jsonify({"error": "Please provide at least one valid email address."}), 400

    try:
        src_token = authenticate(src_id, src_secret)
    except Exception as e:
        return jsonify({"error": f"Source account authentication failed: {e}"}), 401

    try:
        tgt_token = authenticate(tgt_id, tgt_secret)
    except Exception as e:
        return jsonify({"error": f"Target account authentication failed: {e}"}), 401

    # ── Save credentials to session for retry convenience ──
    session["src_client_id"]     = src_id
    session["src_client_secret"] = src_secret
    session["tgt_client_id"]     = tgt_id
    session["tgt_client_secret"] = tgt_secret
    session["emails"]            = raw_emails

    src_employees = get_employees(src_token)
    tgt_employees = get_employees(tgt_token)

    # Build target lookups
    tgt_by_email = {}
    tgt_by_name  = {}
    for emp in tgt_employees:
        info = extract_info(emp)
        if info["email"]:
            tgt_by_email[info["email"]] = info
        if info["full_name"]:
            tgt_by_name[info["full_name"]] = info

    matched     = []
    not_in_src  = []   # requested email not found in source
    not_in_tgt  = []   # found in source, no match in target

    for email in sorted(target_emails):
        # Find in source
        src_info = next(
            (extract_info(e) for e in src_employees if extract_info(e)["email"] == email),
            None,
        )
        if src_info is None:
            not_in_src.append(email)
            continue

        # Match to target
        if email in tgt_by_email:
            matched.append({
                "src":    src_info,
                "tgt":    tgt_by_email[email],
                "method": "Email",
            })
        elif src_info["full_name"] in tgt_by_name:
            matched.append({
                "src":    src_info,
                "tgt":    tgt_by_name[src_info["full_name"]],
                "method": "Name",
            })
        else:
            not_in_tgt.append(src_info)

    return jsonify({
        "matched":    matched,
        "not_in_src": not_in_src,
        "not_in_tgt": not_in_tgt,
    })


@app.route("/api/migrate", methods=["GET"])
def migrate():
    """SSE stream — sends progress events to the browser.
    Re-authenticates and re-matches using session-stored credentials
    so it works correctly even after a process restart on Railway.
    """

    src_id     = session.get("src_client_id", "")
    src_secret = session.get("src_client_secret", "")
    tgt_id     = session.get("tgt_client_id", "")
    tgt_secret = session.get("tgt_client_secret", "")
    raw_emails = session.get("emails", "")

    if not src_id or not src_secret or not tgt_id or not tgt_secret:
        def err():
            yield "data: " + json.dumps({"type": "error", "message": "Session expired. Please go back and run the pre-flight check again."}) + "\n\n"
        return Response(err(), mimetype="text/event-stream")

    # Re-authenticate
    try:
        src_token = authenticate(src_id, src_secret)
        tgt_token = authenticate(tgt_id, tgt_secret)
    except Exception as e:
        def err():
            yield "data: " + json.dumps({"type": "error", "message": f"Re-authentication failed: {e}"}) + "\n\n"
        return Response(err(), mimetype="text/event-stream")

    # Re-fetch and re-match employees
    src_employees = get_employees(src_token)
    tgt_employees = get_employees(tgt_token)

    target_emails = {
        e.strip().lower()
        for line in raw_emails.replace(",", "\n").splitlines()
        for e in [line.strip()]
        if e and "@" in e
    }

    tgt_by_email = {}
    tgt_by_name  = {}
    for emp in tgt_employees:
        info = extract_info(emp)
        if info["email"]:
            tgt_by_email[info["email"]] = info
        if info["full_name"]:
            tgt_by_name[info["full_name"]] = info

    matched = []
    for email in target_emails:
        src_info = next(
            (extract_info(e) for e in src_employees if extract_info(e)["email"] == email),
            None,
        )
        if src_info is None:
            continue
        if email in tgt_by_email:
            matched.append({"src": src_info, "tgt": tgt_by_email[email]})
        elif src_info["full_name"] in tgt_by_name:
            matched.append({"src": src_info, "tgt": tgt_by_name[src_info["full_name"]]})

    if not matched:
        def err():
            yield "data: " + json.dumps({"type": "error", "message": "No matched employees found. Please run the pre-flight check again."}) + "\n\n"
        return Response(err(), mimetype="text/event-stream")

    q = queue.Queue()

    def worker():
        total = success = failed = 0
        q.put({"type": "info", "message": f"Matched {len(matched)} employee(s). Starting document transfer..."})
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            for item in matched:
                src = item["src"]
                tgt = item["tgt"]
                try:
                    documents = get_documents(src_token, src["id"])
                except ValueError as e:
                    q.put({"type": "doc_error", "name": src["display"], "reason": str(e)})
                    continue
                if not documents:
                    q.put({"type": "employee", "name": src["display"], "docs": 0})
                    continue
                q.put({"type": "employee", "name": src["display"], "docs": len(documents)})
                for doc in documents:
                    total += 1
                    attrs    = doc.get("attributes", {})
                    doc_name = attrs.get("file_name", f"doc_{doc.get('id')}")
                    local_file = download_document(src_token, doc, tmp_path)
                    if not local_file:
                        failed += 1
                        q.put({"type": "doc", "name": doc_name, "status": "failed", "reason": "download"})
                        continue
                    ok = upload_document(tgt_token, tgt["id"], local_file, doc)
                    local_file.unlink(missing_ok=True)
                    if ok:
                        success += 1
                        q.put({"type": "doc", "name": doc_name, "status": "ok"})
                    else:
                        failed += 1
                        q.put({"type": "doc", "name": doc_name, "status": "failed", "reason": "upload"})

        q.put({"type": "done", "total": total, "success": success, "failed": failed})

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def stream():
        while True:
            msg = q.get()
            yield "data: " + json.dumps(msg) + "\n\n"
            if msg["type"] == "done":
                break

    return Response(stream(), mimetype="text/event-stream")


# ── HTML / CSS / JS (single-file app) ─────────────────────────────────────────

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Personio Document Migration</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f0f2f5;
      color: #1a1a2e;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 16px;
    }

    header {
      text-align: center;
      margin-bottom: 32px;
    }
    header h1 { font-size: 1.8rem; font-weight: 700; color: #1a1a2e; }
    header p  { color: #666; margin-top: 6px; font-size: 0.95rem; }

    .card {
      background: #fff;
      border-radius: 16px;
      box-shadow: 0 4px 24px rgba(0,0,0,0.08);
      padding: 32px;
      width: 100%;
      max-width: 720px;
      margin-bottom: 24px;
    }

    .step-indicator {
      display: flex;
      gap: 8px;
      margin-bottom: 28px;
      justify-content: center;
    }
    .step-dot {
      width: 10px; height: 10px;
      border-radius: 50%;
      background: #ddd;
      transition: background 0.3s;
    }
    .step-dot.active   { background: #4f46e5; }
    .step-dot.done     { background: #10b981; }

    h2 { font-size: 1.2rem; font-weight: 600; margin-bottom: 20px; color: #1a1a2e; }

    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

    .field-group { display: flex; flex-direction: column; gap: 6px; }
    label { font-size: 0.85rem; font-weight: 500; color: #555; }

    input[type=text], input[type=password], textarea {
      border: 1.5px solid #e2e8f0;
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 0.95rem;
      outline: none;
      transition: border-color 0.2s;
      width: 100%;
      font-family: inherit;
    }
    input:focus, textarea:focus { border-color: #4f46e5; }
    textarea { resize: vertical; min-height: 120px; }

    .section-title {
      font-size: 0.78rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: #9ca3af;
      margin: 20px 0 12px;
    }

    .divider {
      height: 1px;
      background: #f1f5f9;
      margin: 20px 0;
    }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 11px 24px;
      border-radius: 8px;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: opacity 0.2s, transform 0.1s;
    }
    .btn:active { transform: scale(0.98); }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-primary   { background: #4f46e5; color: #fff; }
    .btn-primary:hover:not(:disabled)   { background: #4338ca; }
    .btn-success   { background: #10b981; color: #fff; }
    .btn-success:hover:not(:disabled)   { background: #059669; }
    .btn-outline   { background: transparent; color: #4f46e5; border: 1.5px solid #4f46e5; }
    .btn-outline:hover:not(:disabled)   { background: #f5f3ff; }
    .btn-row { display: flex; gap: 12px; justify-content: flex-end; margin-top: 24px; }

    .alert {
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 0.9rem;
      margin-bottom: 20px;
    }
    .alert-error   { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
    .alert-warning { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
    .alert-info    { background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }

    /* Preflight table */
    table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
    th {
      text-align: left;
      padding: 8px 12px;
      background: #f8fafc;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #64748b;
      border-bottom: 1px solid #e2e8f0;
    }
    td { padding: 10px 12px; border-bottom: 1px solid #f1f5f9; }
    tr:last-child td { border-bottom: none; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 99px;
      font-size: 0.78rem;
      font-weight: 600;
    }
    .badge-green  { background: #dcfce7; color: #16a34a; }
    .badge-yellow { background: #fef9c3; color: #a16207; }
    .badge-red    { background: #fee2e2; color: #dc2626; }

    /* Progress log */
    #log {
      background: #0f172a;
      color: #e2e8f0;
      border-radius: 10px;
      padding: 20px;
      font-family: "JetBrains Mono", "Fira Code", monospace;
      font-size: 0.82rem;
      line-height: 1.7;
      max-height: 360px;
      overflow-y: auto;
    }
    .log-ok     { color: #34d399; }
    .log-fail   { color: #f87171; }
    .log-emp    { color: #818cf8; font-weight: 600; margin-top: 8px; }
    .log-skip   { color: #94a3b8; }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
      margin-top: 20px;
    }
    .summary-box {
      border-radius: 10px;
      padding: 20px;
      text-align: center;
    }
    .summary-box .num  { font-size: 2rem; font-weight: 700; }
    .summary-box .lbl  { font-size: 0.82rem; color: #666; margin-top: 4px; }
    .box-total  { background: #eff6ff; }
    .box-ok     { background: #f0fdf4; }
    .box-fail   { background: #fef2f2; }
    .num-total  { color: #1d4ed8; }
    .num-ok     { color: #16a34a; }
    .num-fail   { color: #dc2626; }

    .spinner {
      display: inline-block;
      width: 18px; height: 18px;
      border: 2px solid rgba(255,255,255,0.4);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .hidden { display: none !important; }
  </style>
</head>
<body>

<header>
  <h1>🔄 Personio Document Migration</h1>
  <p>Migrate documents from a source Personio account to a target account.</p>
</header>

<!-- ── Step indicators ── -->
<div class="step-indicator" id="stepDots">
  <div class="step-dot active" id="dot1"></div>
  <div class="step-dot"        id="dot2"></div>
  <div class="step-dot"        id="dot3"></div>
</div>

<!-- ═══════════════════════════════════════════════════════
     STEP 1 — Credentials + emails
════════════════════════════════════════════════════════ -->
<div class="card" id="step1">
  <h2>Step 1 — API Credentials &amp; Employee Emails</h2>

  <div id="step1Error" class="alert alert-error hidden"></div>

  <div class="section-title">Source Personio Account (WGroup)</div>
  <div class="grid-2">
    <div class="field-group">
      <label>Client ID</label>
      <input type="text" id="srcClientId" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
    </div>
    <div class="field-group">
      <label>Client Secret</label>
      <input type="password" id="srcClientSecret" placeholder="••••••••••••••••" />
    </div>
  </div>

  <div class="section-title">Target Personio Account (AIOO Tech)</div>
  <div class="grid-2">
    <div class="field-group">
      <label>Client ID</label>
      <input type="text" id="tgtClientId" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
    </div>
    <div class="field-group">
      <label>Client Secret</label>
      <input type="password" id="tgtClientSecret" placeholder="••••••••••••••••" />
    </div>
  </div>

  <div class="divider"></div>

  <div class="field-group">
    <label>AIOO Team Employee Emails</label>
    <textarea id="emailList" placeholder="Paste one email per line (or comma-separated):&#10;alice@aiootech.com&#10;bob@aiootech.com&#10;carol@aiootech.com"></textarea>
    <span style="font-size:0.8rem;color:#9ca3af;margin-top:4px;">One email per line, or comma-separated.</span>
  </div>

  <div class="btn-row">
    <button class="btn btn-primary" id="btnPreflight" onclick="runPreflight()">
      Run Pre-flight Check →
    </button>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════
     STEP 2 — Pre-flight report
════════════════════════════════════════════════════════ -->
<div class="card hidden" id="step2">
  <h2>Step 2 — Pre-flight Matching Report</h2>

  <div id="preflightWarning" class="alert alert-warning hidden"></div>

  <div id="matchedSection" class="hidden">
    <div class="section-title">✅ Matched Employees</div>
    <table>
      <thead>
        <tr>
          <th>Email</th>
          <th>Source Name</th>
          <th>Target Name</th>
          <th>Match Method</th>
        </tr>
      </thead>
      <tbody id="matchedRows"></tbody>
    </table>
  </div>

  <div id="notInTgtSection" class="hidden" style="margin-top:20px">
    <div class="section-title">⚠️ Found in Source — No Match in Target</div>
    <div class="alert alert-warning">
      These employees were found in the source account but have no matching profile
      in the target. Their documents <strong>cannot be migrated</strong> until their
      profiles are created in the target account.
    </div>
    <table>
      <thead><tr><th>Email</th><th>Source Name</th></tr></thead>
      <tbody id="notInTgtRows"></tbody>
    </table>
  </div>

  <div id="notInSrcSection" class="hidden" style="margin-top:20px">
    <div class="section-title">❌ Email Not Found in Source</div>
    <table>
      <thead><tr><th>Email</th><th>Status</th></tr></thead>
      <tbody id="notInSrcRows"></tbody>
    </table>
  </div>

  <div class="btn-row">
    <button class="btn btn-outline" onclick="goBack()">← Back</button>
    <button class="btn btn-success" id="btnMigrate" onclick="startMigration()">
      Confirm &amp; Start Migration →
    </button>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════
     STEP 3 — Migration progress
════════════════════════════════════════════════════════ -->
<div class="card hidden" id="step3">
  <h2>Step 3 — Migration in Progress</h2>

  <div class="alert alert-info" style="margin-bottom:16px">
    Please keep this window open until the migration completes.
  </div>

  <div id="log"></div>

  <div id="summarySection" class="hidden">
    <div class="divider" style="margin-top:20px"></div>
    <h2 style="margin-bottom:0">Migration Complete</h2>
    <div class="summary-grid">
      <div class="summary-box box-total">
        <div class="num num-total" id="sumTotal">0</div>
        <div class="lbl">Total documents</div>
      </div>
      <div class="summary-box box-ok">
        <div class="num num-ok" id="sumOk">0</div>
        <div class="lbl">Successfully migrated</div>
      </div>
      <div class="summary-box box-fail">
        <div class="num num-fail" id="sumFail">0</div>
        <div class="lbl">Failed</div>
      </div>
    </div>
    <div class="btn-row" style="margin-top:24px">
      <button class="btn btn-primary" onclick="location.reload()">Start a new migration</button>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════════════════════
     JavaScript
════════════════════════════════════════════════════════ -->
<script>
  // ── Load saved credentials on page load ──────────────────────────────────
  window.addEventListener("DOMContentLoaded", async () => {
    try {
      const res  = await fetch("/api/saved-credentials");
      const data = await res.json();
      if (data.src_client_id) {
        document.getElementById("srcClientId").value     = data.src_client_id;
        document.getElementById("tgtClientId").value     = data.tgt_client_id;
        document.getElementById("emailList").value       = data.emails;
        if (data.has_secrets) {
          document.getElementById("srcClientSecret").value = "••••••••";
          document.getElementById("tgtClientSecret").value = "••••••••";
          showSavedBanner();
        }
      }
    } catch(e) { /* silently ignore */ }
  });

  function showSavedBanner() {
    const banner = document.createElement("div");
    banner.className = "alert alert-info";
    banner.style.marginBottom = "16px";
    banner.innerHTML = "🔁 <strong>Credentials restored from your last session.</strong> The secret fields are pre-filled — leave them as-is to reuse them, or retype to change.";
    document.getElementById("step1").insertBefore(banner, document.getElementById("step1").children[1]);
  }

  function setStep(n) {
    [1,2,3].forEach(i => {
      document.getElementById("step" + i).classList.toggle("hidden", i !== n);
      const dot = document.getElementById("dot" + i);
      dot.classList.remove("active","done");
      if (i < n)  dot.classList.add("done");
      if (i === n) dot.classList.add("active");
    });
  }

  function showError(id, msg) {
    const el = document.getElementById(id);
    el.textContent = msg;
    el.classList.remove("hidden");
  }
  function hideError(id) { document.getElementById(id).classList.add("hidden"); }

  // ── Pre-flight ────────────────────────────────────────────────────────────
  async function runPreflight() {
    hideError("step1Error");
    const btn = document.getElementById("btnPreflight");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Checking…';

    const payload = {
      src_client_id:     document.getElementById("srcClientId").value.trim(),
      src_client_secret: document.getElementById("srcClientSecret").value.trim(),
      tgt_client_id:     document.getElementById("tgtClientId").value.trim(),
      tgt_client_secret: document.getElementById("tgtClientSecret").value.trim(),
      emails:            document.getElementById("emailList").value,
    };

    try {
      const res  = await fetch("/api/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();

      if (!res.ok) {
        showError("step1Error", data.error || "An error occurred.");
        return;
      }

      buildPreflightReport(data);
      setStep(2);
    } catch(e) {
      showError("step1Error", "Network error: " + e.message);
    } finally {
      btn.disabled = false;
      btn.innerHTML = "Run Pre-flight Check →";
    }
  }

  function buildPreflightReport({ matched, not_in_src, not_in_tgt }) {
    // Matched
    const mBody = document.getElementById("matchedRows");
    mBody.innerHTML = "";
    if (matched.length) {
      document.getElementById("matchedSection").classList.remove("hidden");
      matched.forEach(({ src, tgt, method }) => {
        const badgeClass = method === "Email" ? "badge-green" : "badge-yellow";
        mBody.innerHTML += `
          <tr>
            <td>${src.email}</td>
            <td>${src.display}</td>
            <td>${tgt.display}</td>
            <td><span class="badge ${badgeClass}">${method}</span></td>
          </tr>`;
      });
    }

    // Not in target
    const ntBody = document.getElementById("notInTgtRows");
    ntBody.innerHTML = "";
    if (not_in_tgt.length) {
      document.getElementById("notInTgtSection").classList.remove("hidden");
      not_in_tgt.forEach(s => {
        ntBody.innerHTML += `<tr><td>${s.email}</td><td>${s.display}</td></tr>`;
      });
    }

    // Not in source
    const nsBody = document.getElementById("notInSrcRows");
    nsBody.innerHTML = "";
    if (not_in_src.length) {
      document.getElementById("notInSrcSection").classList.remove("hidden");
      not_in_src.forEach(email => {
        nsBody.innerHTML += `
          <tr>
            <td>${email}</td>
            <td><span class="badge badge-red">Not found in source</span></td>
          </tr>`;
      });
    }

    if (!matched.length) {
      document.getElementById("preflightWarning").textContent =
        "No employees could be matched. Please check the credentials and email list.";
      document.getElementById("preflightWarning").classList.remove("hidden");
      document.getElementById("btnMigrate").disabled = true;
    }
  }

  // ── Migration ─────────────────────────────────────────────────────────────
  function goBack() { setStep(1); }

  function startMigration() {
    setStep(3);
    const log = document.getElementById("log");
    log.innerHTML = "";

    const es = new EventSource("/api/migrate");

    es.onmessage = function(e) {
      const msg = JSON.parse(e.data);

      if (msg.type === "info") {
        log.innerHTML += `<div class="log-skip">${msg.message}</div>`;
      }

      if (msg.type === "employee") {
        const text = msg.docs === 0
          ? `<div class="log-skip">⏭  ${msg.name} — no documents</div>`
          : `<div class="log-emp">👤 ${msg.name} (${msg.docs} doc${msg.docs > 1 ? "s" : ""})</div>`;
        log.innerHTML += text;
      }

      if (msg.type === "doc_error") {
        log.innerHTML += `<div class="log-fail">👤 ${msg.name} — ❌ Could not fetch documents: ${msg.reason}</div>`;
      }

      if (msg.type === "doc") {
        if (msg.status === "ok") {
          log.innerHTML += `<div class="log-ok">   ✅ ${msg.name}</div>`;
        } else {
          log.innerHTML += `<div class="log-fail">   ❌ ${msg.name} — ${msg.reason} failed</div>`;
        }
      }

      if (msg.type === "error") {
        log.innerHTML += `<div class="log-fail">ERROR: ${msg.message}</div>`;
        es.close();
      }

      if (msg.type === "done") {
        log.innerHTML += `<div class="log-ok" style="margin-top:12px;font-weight:700">🏁 Done.</div>`;
        document.getElementById("sumTotal").textContent = msg.total;
        document.getElementById("sumOk").textContent    = msg.success;
        document.getElementById("sumFail").textContent  = msg.failed;
        document.getElementById("summarySection").classList.remove("hidden");
        es.close();
      }

      log.scrollTop = log.scrollHeight;
    };

    es.onerror = function() {
      log.innerHTML += `<div class="log-fail">Connection lost.</div>`;
      es.close();
    };
  }
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5050))
    print("╔══════════════════════════════════════════════════════╗")
    print("║     Personio Migration App                           ║")
    print(f"║     Open http://localhost:{port} in your browser      ║")
    print("╚══════════════════════════════════════════════════════╝")
    app.run(host="0.0.0.0", port=port, debug=False)
