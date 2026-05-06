#!/usr/bin/env python3
"""
MCP Server for multi-account Email (IMAP/SMTP).

A provider-agnostic email MCP server that works with any IMAP/SMTP email provider.
Supports multiple accounts, attachments (file-based and base64 inline), calendar
invites, HTML emails, and optional send confirmation gates.

Configuration via accounts.json:
{
  "send_code": "OPTIONAL_CODE",
  "accounts": [
    {
      "name": "my-account",
      "address": "me@example.com",
      "password": "app-password",
      "provider": "purelymail"
    }
  ]
}
"""

import os, json, email, imaplib, smtplib, ssl, re, time, base64, mimetypes
from contextlib import contextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime, formatdate, make_msgid, getaddresses, formataddr
from typing import Optional, Dict, Any
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("simple-email-mcp")

PROVIDER_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "purelymail": {"imap_host": "imap.purelymail.com", "imap_port": 993, "smtp_host": "smtp.purelymail.com", "smtp_port": 465, "smtp_security": "ssl"},
    "domainfactory": {"imap_host": "sslin.de", "imap_port": 993, "smtp_host": "sslout.de", "smtp_port": 465, "smtp_security": "ssl"},
    "gmail": {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl"},
    "outlook": {"imap_host": "outlook.office365.com", "imap_port": 993, "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls"},
}

_accounts: Dict[str, Dict[str, Any]] = {}
_send_code: Optional[str] = None
_sent_folder_cache: Dict[str, str] = {}

def _load_accounts() -> None:
    global _accounts, _send_code, _sent_folder_cache
    _accounts = {}
    _send_code = None
    _sent_folder_cache = {}
    config_path = os.environ.get("ACCOUNTS_FILE", str(Path(__file__).parent / "accounts.json"))
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            raw = json.load(f)
        code = raw.get("send_code", "")
        _send_code = code if code else None
        for acct in raw.get("accounts", []):
            name = acct["name"]
            provider = acct.get("provider", "")
            defaults = PROVIDER_DEFAULTS.get(provider, {})
            smtp_port = acct.get("smtp_port", defaults.get("smtp_port", 465))
            smtp_security = acct.get("smtp_security", defaults.get("smtp_security", "starttls" if smtp_port == 587 else "ssl"))
            address = acct["address"]
            _accounts[name] = {
                "address": address, "password": acct["password"],
                "send_as": acct.get("send_as") or address,
                "display_name": acct.get("display_name") or acct.get("from_name") or "",
                "description": acct.get("description") or "",
                "imap_host": acct.get("imap_host", defaults.get("imap_host", "imap.example.com")),
                "imap_port": acct.get("imap_port", defaults.get("imap_port", 993)),
                "smtp_host": acct.get("smtp_host", defaults.get("smtp_host", "smtp.example.com")),
                "smtp_port": smtp_port,
                "smtp_security": smtp_security,
            }
        return
    _send_code = os.environ.get("SEND_CODE") or None
    addr = os.environ.get("EMAIL_ADDRESS", "")
    pwd = os.environ.get("EMAIL_PASSWORD", "")
    if addr and pwd:
        smtp_port = int(os.environ.get("SMTP_PORT", "465"))
        _accounts["default"] = {
            "address": addr, "password": pwd,
            "send_as": os.environ.get("SEND_AS", addr),
            "display_name": os.environ.get("EMAIL_DISPLAY_NAME", ""),
            "description": os.environ.get("EMAIL_DESCRIPTION", ""),
            "imap_host": os.environ.get("IMAP_HOST", "imap.example.com"),
            "imap_port": int(os.environ.get("IMAP_PORT", "993")),
            "smtp_host": os.environ.get("SMTP_HOST", "smtp.example.com"),
            "smtp_port": smtp_port,
            "smtp_security": os.environ.get("SMTP_SECURITY", "starttls" if smtp_port == 587 else "ssl"),
        }

_load_accounts()

def _refresh_runtime_config() -> None:
    _load_accounts()

def _check_confirmation_code(confirmation_code: Optional[str]) -> Optional[str]:
    _refresh_runtime_config()
    if _send_code:
        if not confirmation_code:
            return "BLOCKED: A confirmation code is required. Show the draft to the user and ask for their code."
        if confirmation_code.strip() != _send_code.strip():
            return "BLOCKED: Invalid confirmation code. The email was NOT sent."
    return None

def _resolve_account(account: Optional[str]) -> Dict[str, Any]:
    _refresh_runtime_config()
    if not _accounts:
        raise RuntimeError("No email accounts configured.")
    if not account:
        if len(_accounts) == 1:
            return next(iter(_accounts.values()))
        available = ", ".join(f"'{n}' ({c['address']})" for n, c in _accounts.items())
        raise ValueError(f"Multiple accounts configured. You must specify which one: {available}")
    key = account.lower().strip()
    for name, cfg in _accounts.items():
        if key == name.lower() or key == cfg["address"].lower():
            return cfg
    partial_matches = []
    for name, cfg in _accounts.items():
        if key in name.lower() or key in cfg["address"].lower():
            partial_matches.append((name, cfg))
    if len(partial_matches) == 1:
        return partial_matches[0][1]
    if len(partial_matches) > 1:
        available = ", ".join(f"'{name}' ({cfg['address']})" for name, cfg in partial_matches)
        raise ValueError(f"Account '{account}' is ambiguous. Matches: {available}")
    available = ", ".join(f"'{n}'" for n in _accounts)
    raise ValueError(f"Account '{account}' not found. Available: {available}")

def _imap_connect(acct: Dict[str, Any]) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(acct["imap_host"], acct["imap_port"], ssl_context=ctx)
    conn.login(acct["address"], acct["password"])
    return conn

@contextmanager
def _imap_session(acct: Dict[str, Any], folder: Optional[str] = None, readonly: bool = True):
    last_err = None
    for attempt in range(3):
        try:
            conn = _imap_connect(acct)
            if folder:
                status, data = conn.select(folder, readonly=readonly)
                if status != "OK":
                    detail = ""
                    if data and data[0]:
                        detail = data[0].decode("utf-8", errors="replace") if isinstance(data[0], bytes) else str(data[0])
                    raise RuntimeError(f"Could not select folder '{folder}'" + (f": {detail}" if detail else ""))
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
    else:
        raise last_err
    try:
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass

def _decode_imap_utf7(s: str) -> str:
    result = []
    i = 0
    while i < len(s):
        if s[i] == '&':
            j = s.index('-', i + 1) if '-' in s[i + 1:] else len(s)
            if j == i + 1:
                result.append('&')
            else:
                encoded = s[i + 1:j].replace(',', '/')
                encoded += '=' * (4 - len(encoded) % 4) if len(encoded) % 4 else ''
                try:
                    result.append(base64.b64decode(encoded).decode('utf-16-be'))
                except Exception:
                    result.append(s[i:j + 1])
            i = j + 1
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)

_IMAP_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]+)"\s+(?P<name>.+)')

def _parse_imap_list_response(raw_list: list) -> list[dict]:
    folders = []
    for item in raw_list:
        if not isinstance(item, bytes):
            continue
        line = item.decode("utf-8", errors="replace")
        m = _IMAP_LIST_RE.match(line)
        if m:
            raw_name = m.group("name").strip().strip('"')
            folders.append({"name": _decode_imap_utf7(raw_name), "raw_name": raw_name, "delimiter": m.group("delim"), "flags": m.group("flags")})
        else:
            parts = line.rsplit(None, 1)
            if parts:
                raw_name = parts[-1].strip('"')
                folders.append({"name": _decode_imap_utf7(raw_name), "raw_name": raw_name, "delimiter": ".", "flags": ""})
    return folders

def _decode_header_value(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for fragment, charset in parts:
        if isinstance(fragment, bytes):
            decoded.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(fragment)
    return " ".join(decoded)

def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        text_part = html_part = None
        for part in msg.walk():
            ct = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp:
                continue
            if ct == "text/plain" and text_part is None:
                text_part = part
            elif ct == "text/html" and html_part is None:
                html_part = part
        chosen = text_part or html_part
        if chosen:
            payload = chosen.get_payload(decode=True)
            charset = chosen.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace") if payload else ""
        return ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace") if payload else ""

def _list_attachments(msg: email.message.Message) -> list[dict]:
    attachments = []
    if not msg.is_multipart():
        return attachments
    for idx, part in enumerate(msg.walk()):
        disp = str(part.get("Content-Disposition", ""))
        if "attachment" in disp:
            filename = _decode_header_value(part.get_filename())
            size = len(part.get_payload(decode=True) or b"")
            attachments.append({"index": idx, "filename": filename or "(unnamed)", "size_bytes": size, "content_type": part.get_content_type()})
    return attachments

def _msg_to_summary(msg: email.message.Message, uid: str) -> dict:
    date_str = msg.get("Date", "")
    try:
        date_formatted = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M")
    except Exception:
        date_formatted = date_str
    return {"uid": uid, "from": _decode_header_value(msg.get("From")), "to": _decode_header_value(msg.get("To")), "subject": _decode_header_value(msg.get("Subject")), "date": date_formatted, "has_attachments": msg.get_content_type() == "multipart/mixed"}

def _find_sent_folder(conn: imaplib.IMAP4_SSL, acct: Dict[str, Any]) -> str:
    cache_key = acct["address"]
    if cache_key in _sent_folder_cache:
        return _sent_folder_cache[cache_key]
    try:
        status, folder_list = conn.list()
        if status == "OK" and folder_list:
            parsed = _parse_imap_list_response(folder_list)
            for f in parsed:
                if "\\sent" in f["flags"].lower():
                    _sent_folder_cache[cache_key] = f["raw_name"]
                    return f["raw_name"]
            for f in parsed:
                if f["raw_name"].lower() in ("sent", "inbox.sent", "sent items", "sent messages"):
                    _sent_folder_cache[cache_key] = f["raw_name"]
                    return f["raw_name"]
    except Exception:
        pass
    return "Sent"

def _save_to_sent(acct: Dict[str, Any], mime_message: str) -> Optional[str]:
    """Returns a warning string on failure, None on success."""
    try:
        with _imap_session(acct) as conn:
            sent_folder = _find_sent_folder(conn, acct)
            date_time = imaplib.Time2Internaldate(time.time())
            status, response = conn.append(sent_folder, "\\Seen", date_time, mime_message.encode("utf-8"))
            if status != "OK":
                return f"Email sent, but failed to save to Sent folder ({status})"
    except Exception as e:
        return f"Email sent, but failed to save to Sent folder ({e})"
    return None

def _smtp_send(acct: Dict[str, Any], sender: str, recipients: list[str], mime_str: str) -> None:
    ctx = ssl.create_default_context()
    if acct.get("smtp_security") == "starttls":
        with smtplib.SMTP(acct["smtp_host"], acct["smtp_port"]) as server:
            server.starttls(context=ctx)
            server.login(acct["address"], acct["password"])
            server.sendmail(sender, recipients, mime_str)
    else:
        with smtplib.SMTP_SSL(acct["smtp_host"], acct["smtp_port"], context=ctx) as server:
            server.login(acct["address"], acct["password"])
            server.sendmail(sender, recipients, mime_str)

def _quote_body(body: str, sender: str, date: str) -> str:
    quoted = "\n".join(f"> {line}" for line in body.splitlines())
    return f"On {date}, {sender} wrote:\n{quoted}"

def _parse_address_list(*fields: Optional[str]) -> list[str]:
    addresses = []
    seen = set()
    for _, addr in getaddresses([field for field in fields if field]):
        normalized = addr.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            addresses.append(addr.strip())
    return addresses

def _split_attachment_paths(file_attachments: Optional[str]) -> list[str]:
    if not file_attachments:
        return []
    return [filepath.strip() for filepath in file_attachments.split(",") if filepath.strip()]

def _collect_attachment_metadata(file_attachments: Optional[str]) -> tuple[list[dict], int]:
    attachment_info = []
    total_size = 0
    for filepath in _split_attachment_paths(file_attachments):
        resolved_path = str(Path(filepath).expanduser().resolve(strict=False))
        exists = os.path.isfile(filepath)
        size_bytes = os.path.getsize(filepath) if exists else None
        content_type, _ = mimetypes.guess_type(filepath)
        if content_type is None:
            content_type = "application/octet-stream"
        entry = {
            "path": filepath,
            "resolved_path": resolved_path,
            "filename": os.path.basename(filepath),
            "exists": exists,
            "size_bytes": size_bytes,
            "content_type": content_type,
        }
        attachment_info.append(entry)
        if size_bytes is not None:
            total_size += size_bytes
    return attachment_info, total_size

def _sender_address(acct: Dict[str, Any]) -> str:
    return acct.get("send_as") or acct["address"]

def _format_from_header(acct: Dict[str, Any]) -> str:
    sender = _sender_address(acct)
    display_name = (acct.get("display_name") or "").strip()
    if display_name:
        return formataddr((display_name, sender))
    return sender

def _compose_and_send(
    acct: Dict[str, Any], to: str, subject: str, body: str,
    body_html: Optional[str] = None, cc: Optional[str] = None, bcc: Optional[str] = None,
    in_reply_to: Optional[str] = None, references: Optional[str] = None,
    file_attachments: Optional[str] = None, inline_attachments: Optional[str] = None,
    calendar_ics: Optional[str] = None, forwarded_parts: Optional[list] = None,
) -> dict:
    has_attachments = bool(file_attachments and file_attachments.strip()) or bool(inline_attachments) or bool(forwarded_parts)
    # Build body part
    if calendar_ics:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "plain", "utf-8"))
        ics_part = MIMEText(calendar_ics, "calendar", "utf-8")
        ics_part.replace_header("Content-Type", "text/calendar; method=REQUEST; charset=utf-8")
        body_part.attach(ics_part)
    elif body_html:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body, "plain", "utf-8"))
        body_part.attach(MIMEText(body_html, "html", "utf-8"))
    else:
        body_part = MIMEText(body, "plain", "utf-8")
    # Only wrap in multipart/mixed if there are attachments
    if has_attachments:
        mime = MIMEMultipart("mixed")
        mime.attach(body_part)
    else:
        mime = body_part
    sender = _sender_address(acct)
    mime["From"] = _format_from_header(acct)
    mime["To"] = to
    mime["Subject"] = subject
    mime["Date"] = formatdate(localtime=True)
    mime["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
    if cc: mime["Cc"] = cc
    if in_reply_to: mime["In-Reply-To"] = in_reply_to
    if references: mime["References"] = references
    if file_attachments:
        for filepath in file_attachments.split(","):
            filepath = filepath.strip()
            if not filepath: continue
            if not os.path.isfile(filepath):
                return {"error": f"Attachment not found: {filepath}"}
            filename = os.path.basename(filepath)
            ctype, _ = mimetypes.guess_type(filepath)
            if ctype is None: ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(filepath, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            mime.attach(part)
    if inline_attachments:
        try:
            for att in json.loads(inline_attachments):
                filename = att.get("filename", "attachment")
                content_type = att.get("content_type", "application/octet-stream")
                maintype, subtype = content_type.split("/", 1)
                file_data = base64.b64decode(att["content_base64"])
                part = MIMEBase(maintype, subtype)
                part.set_payload(file_data)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=filename)
                mime.attach(part)
        except json.JSONDecodeError:
            return {"error": "attachments_inline must be a valid JSON array."}
        except Exception as e:
            return {"error": f"Error processing inline attachment: {e}"}
    if forwarded_parts:
        for part in forwarded_parts:
            mime.attach(part)
    recipients = [a.strip() for a in to.split(",")]
    if cc: recipients += [a.strip() for a in cc.split(",")]
    if bcc: recipients += [a.strip() for a in bcc.split(",")]
    mime_str = mime.as_string()
    _smtp_send(acct, sender, recipients, mime_str)
    sent_warning = _save_to_sent(acct, mime_str)
    result = {"status": "sent", "from": sender, "to": to, "subject": subject}
    if cc: result["cc"] = cc
    if bcc: result["bcc"] = bcc
    if sent_warning: result["warning"] = sent_warning
    return result

# ─── Action schemas for lazy discovery ──────────────────────────────────────
ACTION_SCHEMAS: Dict[str, Any] = {
    "list_accounts":      {"description": "List configured email accounts.", "params": {}},
    "list_folders":       {"description": "List IMAP folders.", "params": {"account": "str, optional"}},
    "list_emails":        {"description": "List recent emails.", "params": {"account": "str, optional", "folder": "str, default=INBOX", "limit": "int, default=20, max=100"}},
    "search":             {"description": "Search emails (IMAP: FROM, TO, SUBJECT, BODY, SINCE, BEFORE, UNSEEN, SEEN, FLAGGED). Example: 'FROM john SINCE 01-Feb-2025'", "params": {"query": "str, required", "account": "str, optional", "folder": "str, default=INBOX", "limit": "int, default=20"}},
    "read":               {"description": "Read full email by UID.", "params": {"uid": "str, required", "account": "str, optional", "folder": "str, default=INBOX"}},
    "send":               {"description": "Send email. confirmation_code required if send_code configured.", "params": {"to": "str, required", "subject": "str, required", "body": "str, required", "account": "str, optional", "cc": "str, optional", "bcc": "str, optional", "body_html": "str, optional", "attachments": "str, optional (comma-separated paths)", "attachments_inline": 'str, optional (JSON: [{"filename":"f.pdf","content_base64":"...","content_type":"..."}])', "calendar_ics": "str, optional", "confirmation_code": "str, optional"}},
    "reply":              {"description": "Reply to an email.", "params": {"uid": "str, required", "body": "str, required", "account": "str, optional", "folder": "str, default=INBOX", "body_html": "str, optional", "attachments": "str, optional", "confirmation_code": "str, optional"}},
    "reply_all":          {"description": "Reply to all recipients.", "params": {"uid": "str, required", "body": "str, required", "account": "str, optional", "folder": "str, default=INBOX", "body_html": "str, optional", "attachments": "str, optional", "confirmation_code": "str, optional"}},
    "forward":            {"description": "Forward an email.", "params": {"uid": "str, required", "to": "str, required", "account": "str, optional", "folder": "str, default=INBOX", "body": "str, optional", "include_attachments": "bool, default=true", "attachments": "str, optional", "confirmation_code": "str, optional"}},
    "move":               {"description": "Move email to another folder.", "params": {"uid": "str, required", "destination_folder": "str, required", "account": "str, optional", "source_folder": "str, default=INBOX"}},
    "mark":               {"description": "Mark email read/unread/flag/unflag.", "params": {"uid": "str, required", "action": "read|unread|flag|unflag", "account": "str, optional", "folder": "str, default=INBOX"}},
    "save_attachment":    {"description": "Save attachment to disk.", "params": {"uid": "str, required", "attachment_index": "int, required", "save_path": "str, required", "account": "str, optional", "folder": "str, default=INBOX", "overwrite": "bool, default=false"}},
    "get_attachment":     {"description": "Get attachment as base64 (prefer save_attachment).", "params": {"uid": "str, required", "attachment_index": "int, required", "account": "str, optional", "folder": "str, default=INBOX"}},
    "prepare_attachments":{"description": "Inspect local files before attaching.", "params": {"attachments": "str, required (comma-separated paths)"}},
}

# ─── Internal handlers ───────────────────────────────────────────────────────

async def _do_list_accounts() -> str:
    _refresh_runtime_config()
    if not _accounts:
        return "No accounts configured. See README for setup."
    accounts_info = []
    for name, cfg in _accounts.items():
        info = {
            "name": name,
            "address": cfg["address"],
            "send_as": _sender_address(cfg),
            "imap_host": cfg["imap_host"],
            "smtp_host": cfg["smtp_host"],
        }
        if cfg.get("display_name"): info["display_name"] = cfg["display_name"]
        if cfg.get("description"): info["description"] = cfg["description"]
        accounts_info.append(info)
    return json.dumps({"accounts": accounts_info, "count": len(accounts_info)}, indent=2)

async def _do_list_folders(account: Optional[str] = None) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct) as conn:
            status, folder_list = conn.list()
            if status != "OK":
                return "Error: Could not retrieve folder list."
            parsed = _parse_imap_list_response(folder_list)
            folder_info = []
            for f in parsed:
                entry = {"name": f["name"], "raw_name": f["raw_name"]}
                flags_lower = f["flags"].lower()
                if "\\sent" in flags_lower: entry["type"] = "sent"
                elif "\\trash" in flags_lower or "\\deleted" in flags_lower: entry["type"] = "trash"
                elif "\\drafts" in flags_lower: entry["type"] = "drafts"
                elif "\\junk" in flags_lower or "\\spam" in flags_lower: entry["type"] = "spam"
                elif "\\archive" in flags_lower: entry["type"] = "archive"
                elif f["raw_name"].upper() == "INBOX": entry["type"] = "inbox"
                folder_info.append(entry)
            return json.dumps({"account": acct["address"], "folders": folder_info, "count": len(folder_info), "hint": "Use 'raw_name' in folder parameters."}, indent=2)
    except Exception as e:
        return f"Error listing folders: {e}"

async def _do_list_emails(account: Optional[str], folder: str, limit: int) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct, folder=folder) as conn:
            status, data = conn.uid("search", None, "ALL")
            if status != "OK":
                return f"Error: Could not search folder '{folder}'."
            uids = data[0].split() if data[0] else []
            uids = uids[-limit:]
            uids.reverse()
            results = []
            for uid_bytes in uids:
                uid = uid_bytes.decode()
                status, msg_data = conn.uid("fetch", uid, "(RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]: continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if isinstance(raw, bytes):
                    results.append(_msg_to_summary(email.message_from_bytes(raw), uid))
            return json.dumps({"account": acct["address"], "folder": folder, "count": len(results), "emails": results}, indent=2)
    except Exception as e:
        return f"Error listing emails: {e}"

async def _do_search(account: Optional[str], query: str, folder: str, limit: int) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct, folder=folder) as conn:
            try:
                status, data = conn.uid("search", None, query)
            except imaplib.IMAP4.error as e:
                return f"Error: Invalid IMAP search query '{query}': {e}"
            if status != "OK":
                return f"Error: Search failed for query '{query}'."
            uids = data[0].split() if data[0] else []
            uids = uids[-limit:]
            uids.reverse()
            results = []
            for uid_bytes in uids:
                uid = uid_bytes.decode()
                status, msg_data = conn.uid("fetch", uid, "(RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]: continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if isinstance(raw, bytes):
                    results.append(_msg_to_summary(email.message_from_bytes(raw), uid))
            return json.dumps({"account": acct["address"], "query": query, "folder": folder, "count": len(results), "emails": results}, indent=2)
    except Exception as e:
        return f"Error searching emails: {e}"

async def _do_read(account: Optional[str], uid: str, folder: str) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct, folder=folder) as conn:
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            body = _extract_body(msg)
            attachments = _list_attachments(msg)
            date_str = msg.get("Date", "")
            try:
                date_formatted = parsedate_to_datetime(date_str).strftime("%Y-%m-%d %H:%M:%S %Z")
            except Exception:
                date_formatted = date_str
            return json.dumps({"account": acct["address"], "uid": uid, "from": _decode_header_value(msg.get("From")), "to": _decode_header_value(msg.get("To")), "cc": _decode_header_value(msg.get("Cc")), "subject": _decode_header_value(msg.get("Subject")), "date": date_formatted, "message_id": msg.get("Message-ID", ""), "in_reply_to": msg.get("In-Reply-To", ""), "body": body[:50000], "attachments": attachments}, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error reading email: {e}"

async def _do_send(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        in_reply_to = references = None
        if p.get("reply_to_uid"):
            try:
                with _imap_session(acct, folder=p.get("reply_to_folder", "INBOX")) as conn:
                    status, msg_data = conn.uid("fetch", p["reply_to_uid"], "(RFC822.HEADER)")
                    if status == "OK" and msg_data and msg_data[0]:
                        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                        if isinstance(raw, bytes):
                            orig_id = email.message_from_bytes(raw).get("Message-ID", "")
                            if orig_id:
                                in_reply_to = orig_id
                                references = orig_id
            except Exception: pass
        result = _compose_and_send(acct, p["to"], p["subject"], p["body"], body_html=p.get("body_html"), cc=p.get("cc"), bcc=p.get("bcc"), in_reply_to=in_reply_to, references=references, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"), calendar_ics=p.get("calendar_ics"))
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error sending email: {e}"

async def _do_reply(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        folder = p.get("folder", "INBOX")
        with _imap_session(acct, folder=folder) as conn:
            status, msg_data = conn.uid("fetch", p["uid"], "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {p['uid']}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            orig = email.message_from_bytes(raw)
        orig_from = _decode_header_value(orig.get("From"))
        orig_subject = _decode_header_value(orig.get("Subject"))
        orig_date = orig.get("Date", "")
        orig_body = _extract_body(orig)
        orig_msg_id = orig.get("Message-ID", "")
        _, reply_addr = parseaddr(orig_from)
        subject = orig_subject if re.match(r'(?i)^Re:\s', orig_subject) else f"Re: {orig_subject}"
        full_body = f"{p['body']}\n\n{_quote_body(orig_body, orig_from, orig_date)}"
        full_body_html = None
        if p.get("body_html"):
            quoted_html = orig_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            full_body_html = f"{p['body_html']}<br><br><div style='border-left:2px solid #ccc;padding-left:10px;color:#555;'>On {orig_date}, {orig_from} wrote:<br>{quoted_html}</div>"
        result = _compose_and_send(acct, reply_addr, subject, full_body, body_html=full_body_html, in_reply_to=orig_msg_id, references=orig_msg_id, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"))
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error replying: {e}"

async def _do_reply_all(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        folder = p.get("folder", "INBOX")
        with _imap_session(acct, folder=folder) as conn:
            status, msg_data = conn.uid("fetch", p["uid"], "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {p['uid']}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            orig = email.message_from_bytes(raw)
        orig_from = _decode_header_value(orig.get("From"))
        orig_to = _decode_header_value(orig.get("To"))
        orig_cc_h = _decode_header_value(orig.get("Cc"))
        orig_subject = _decode_header_value(orig.get("Subject"))
        orig_date = orig.get("Date", "")
        orig_body = _extract_body(orig)
        orig_msg_id = orig.get("Message-ID", "")
        _, reply_addr = parseaddr(orig_from)
        my_addrs = {acct["address"].lower(), _sender_address(acct).lower()}
        cc_addrs = []
        seen_cc: set = set()
        for addr in _parse_address_list(orig_to, orig_cc_h):
            normalized = addr.lower()
            if normalized in my_addrs or normalized == reply_addr.lower() or normalized in seen_cc:
                continue
            seen_cc.add(normalized)
            cc_addrs.append(addr)
        subject = orig_subject if re.match(r'(?i)^Re:\s', orig_subject) else f"Re: {orig_subject}"
        full_body = f"{p['body']}\n\n{_quote_body(orig_body, orig_from, orig_date)}"
        full_body_html = None
        if p.get("body_html"):
            quoted_html = orig_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            full_body_html = f"{p['body_html']}<br><br><div style='border-left:2px solid #ccc;padding-left:10px;color:#555;'>On {orig_date}, {orig_from} wrote:<br>{quoted_html}</div>"
        result = _compose_and_send(acct, reply_addr, subject, full_body, body_html=full_body_html, cc=", ".join(cc_addrs) if cc_addrs else None, in_reply_to=orig_msg_id, references=orig_msg_id, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"))
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error replying all: {e}"

async def _do_forward(p: Dict[str, Any]) -> str:
    blocked = _check_confirmation_code(p.get("confirmation_code"))
    if blocked:
        return blocked
    try:
        acct = _resolve_account(p.get("account"))
        folder = p.get("folder", "INBOX")
        with _imap_session(acct, folder=folder) as conn:
            status, msg_data = conn.uid("fetch", p["uid"], "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {p['uid']}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            orig = email.message_from_bytes(raw)
        orig_from = _decode_header_value(orig.get("From"))
        orig_to = _decode_header_value(orig.get("To"))
        orig_subject = _decode_header_value(orig.get("Subject"))
        orig_date = orig.get("Date", "")
        orig_body = _extract_body(orig)
        subject = orig_subject if re.match(r'(?i)^Fwd?:\s', orig_subject) else f"Fwd: {orig_subject}"
        fwd_header = f"---------- Forwarded message ----------\nFrom: {orig_from}\nDate: {orig_date}\nSubject: {orig_subject}\nTo: {orig_to}\n\n"
        body = p.get("body", "")
        full_body = f"{body}\n\n{fwd_header}{orig_body}" if body else f"{fwd_header}{orig_body}"
        full_body_html = None
        if p.get("body_html"):
            fwd_header_html = f"<b>---------- Forwarded message ----------</b><br>From: {orig_from}<br>Date: {orig_date}<br>Subject: {orig_subject}<br>To: {orig_to}<br><br>"
            orig_body_html = orig_body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
            full_body_html = f"{p['body_html']}<br><br>{fwd_header_html}{orig_body_html}"
        forwarded_parts = []
        if p.get("include_attachments", True) and orig.is_multipart():
            for part in orig.walk():
                if "attachment" in str(part.get("Content-Disposition", "")):
                    forwarded_parts.append(part)
        result = _compose_and_send(acct, p["to"], subject, full_body, body_html=full_body_html, file_attachments=p.get("attachments"), inline_attachments=p.get("attachments_inline"), forwarded_parts=forwarded_parts)
        if "error" in result:
            return f"Error: {result['error']}"
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error forwarding: {e}"

async def _do_move(account: Optional[str], uid: str, source_folder: str, destination_folder: str) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct, folder=source_folder, readonly=False) as conn:
            status, _ = conn.uid("copy", uid, destination_folder)
            if status != "OK":
                return f"Error: Could not copy email to '{destination_folder}'."
            conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            return json.dumps({"status": "moved", "account": acct["address"], "uid": uid, "from_folder": source_folder, "to_folder": destination_folder}, indent=2)
    except Exception as e:
        return f"Error moving email: {e}"

async def _do_mark(account: Optional[str], uid: str, folder: str, action: str) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct, folder=folder, readonly=False) as conn:
            flag_map = {"read": ("+FLAGS", "(\\Seen)"), "unread": ("-FLAGS", "(\\Seen)"), "flag": ("+FLAGS", "(\\Flagged)"), "unflag": ("-FLAGS", "(\\Flagged)")}
            if action.lower() not in flag_map:
                return f"Error: action must be one of: {', '.join(flag_map)}"
            op, flag = flag_map[action.lower()]
            status, _ = conn.uid("store", uid, op, flag)
            if status != "OK":
                return f"Error: Could not mark email as '{action}'."
            return json.dumps({"status": "success", "account": acct["address"], "uid": uid, "action": action}, indent=2)
    except Exception as e:
        return f"Error marking email: {e}"

async def _do_save_attachment(account: Optional[str], uid: str, folder: str, attachment_index: int, save_path: str, overwrite: bool) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct, folder=folder) as conn:
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            for idx, part in enumerate(msg.walk()):
                if idx == attachment_index:
                    if "attachment" not in str(part.get("Content-Disposition", "")):
                        return f"Error: Part at index {idx} is not an attachment."
                    filename = _decode_header_value(part.get_filename()) or "(unnamed)"
                    payload = part.get_payload(decode=True) or b""
                    save_dir = os.path.dirname(save_path)
                    if save_dir and not os.path.isdir(save_dir):
                        os.makedirs(save_dir, exist_ok=True)
                    existed_before = os.path.exists(save_path)
                    if existed_before and not overwrite:
                        return f"Error: File already exists at '{save_path}'. Re-run with overwrite=true to replace."
                    with open(save_path, "wb") as f:
                        f.write(payload)
                    return json.dumps({"saved": save_path, "filename": filename, "content_type": part.get_content_type(), "size_bytes": len(payload), "overwritten": existed_before}, indent=2)
            return f"Error: No part found at index {attachment_index}."
    except Exception as e:
        return f"Error saving attachment: {e}"

async def _do_get_attachment(account: Optional[str], uid: str, folder: str, attachment_index: int) -> str:
    try:
        acct = _resolve_account(account)
        with _imap_session(acct, folder=folder) as conn:
            status, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            for idx, part in enumerate(msg.walk()):
                if idx == attachment_index:
                    if "attachment" not in str(part.get("Content-Disposition", "")):
                        return f"Error: Part at index {idx} is not an attachment."
                    filename = _decode_header_value(part.get_filename()) or "(unnamed)"
                    payload = part.get_payload(decode=True) or b""
                    return json.dumps({"filename": filename, "content_type": part.get_content_type(), "size_bytes": len(payload), "content_base64": base64.b64encode(payload).decode("ascii")}, indent=2)
            return f"Error: No part found at index {attachment_index}."
    except Exception as e:
        return f"Error getting attachment: {e}"

async def _do_prepare_attachments(attachments: str) -> str:
    try:
        attachment_info, total_size = _collect_attachment_metadata(attachments)
        missing = [item["path"] for item in attachment_info if not item["exists"]]
        return json.dumps({"attachments": attachment_info, "count": len(attachment_info), "existing_count": sum(1 for item in attachment_info if item["exists"]), "missing_count": len(missing), "missing": missing, "total_size_bytes": total_size}, indent=2)
    except Exception as e:
        return f"Error preparing attachments: {e}"

# ─── Single dispatcher tool ──────────────────────────────────────────────────

@mcp.tool(name="email")
async def email_dispatcher(action: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Email client (IMAP/SMTP). Call with just action to discover its parameters. Actions: list_accounts, list_folders, list_emails, search, read, send, reply, reply_all, forward, move, mark, save_attachment, get_attachment, prepare_attachments."""
    if params is None:
        if action not in ACTION_SCHEMAS:
            return json.dumps({"error": f"Unknown action '{action}'", "available_actions": list(ACTION_SCHEMAS.keys())})
        return json.dumps({"action": action, **ACTION_SCHEMAS[action]})
    p = params
    if action == "list_accounts":
        return await _do_list_accounts()
    elif action == "list_folders":
        return await _do_list_folders(p.get("account"))
    elif action == "list_emails":
        return await _do_list_emails(p.get("account"), p.get("folder", "INBOX"), int(p.get("limit", 20)))
    elif action == "search":
        if "query" not in p: return json.dumps({"error": "Missing required param: query", **ACTION_SCHEMAS["search"]})
        return await _do_search(p.get("account"), p["query"], p.get("folder", "INBOX"), int(p.get("limit", 20)))
    elif action == "read":
        if "uid" not in p: return json.dumps({"error": "Missing required param: uid", **ACTION_SCHEMAS["read"]})
        return await _do_read(p.get("account"), p["uid"], p.get("folder", "INBOX"))
    elif action == "send":
        for req in ("to", "subject", "body"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["send"]})
        return await _do_send(p)
    elif action == "reply":
        for req in ("uid", "body"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["reply"]})
        return await _do_reply(p)
    elif action == "reply_all":
        for req in ("uid", "body"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["reply_all"]})
        return await _do_reply_all(p)
    elif action == "forward":
        for req in ("uid", "to"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["forward"]})
        return await _do_forward(p)
    elif action == "move":
        for req in ("uid", "destination_folder"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["move"]})
        return await _do_move(p.get("account"), p["uid"], p.get("source_folder", "INBOX"), p["destination_folder"])
    elif action == "mark":
        for req in ("uid", "action"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["mark"]})
        return await _do_mark(p.get("account"), p["uid"], p.get("folder", "INBOX"), p["action"])
    elif action == "save_attachment":
        for req in ("uid", "attachment_index", "save_path"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["save_attachment"]})
        return await _do_save_attachment(p.get("account"), p["uid"], p.get("folder", "INBOX"), int(p["attachment_index"]), p["save_path"], bool(p.get("overwrite", False)))
    elif action == "get_attachment":
        for req in ("uid", "attachment_index"):
            if req not in p: return json.dumps({"error": f"Missing required param: {req}", **ACTION_SCHEMAS["get_attachment"]})
        return await _do_get_attachment(p.get("account"), p["uid"], p.get("folder", "INBOX"), int(p["attachment_index"]))
    elif action == "prepare_attachments":
        if "attachments" not in p: return json.dumps({"error": "Missing required param: attachments", **ACTION_SCHEMAS["prepare_attachments"]})
        return await _do_prepare_attachments(p["attachments"])
    else:
        return json.dumps({"error": f"Unknown action '{action}'", "available_actions": list(ACTION_SCHEMAS.keys())})

def main():
    mcp.run()

if __name__ == "__main__":
    main()
