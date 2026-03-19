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
from email.utils import parsedate_to_datetime, formatdate, make_msgid
from typing import Optional, Dict, Any
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, field_validator
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
    global _accounts, _send_code
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
            _accounts[name] = {
                "address": acct["address"], "password": acct["password"],
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
            "imap_host": os.environ.get("IMAP_HOST", "imap.example.com"),
            "imap_port": int(os.environ.get("IMAP_PORT", "993")),
            "smtp_host": os.environ.get("SMTP_HOST", "smtp.example.com"),
            "smtp_port": smtp_port,
            "smtp_security": os.environ.get("SMTP_SECURITY", "starttls" if smtp_port == 587 else "ssl"),
        }

_load_accounts()

def _resolve_account(account: Optional[str]) -> Dict[str, Any]:
    if not _accounts:
        raise RuntimeError("No email accounts configured.")
    if not account:
        return next(iter(_accounts.values()))
    key = account.lower().strip()
    if key in _accounts:
        return _accounts[key]
    for name, cfg in _accounts.items():
        if key in name.lower():
            return cfg
    for name, cfg in _accounts.items():
        if key in cfg["address"].lower():
            return cfg
    available = ", ".join(f"'{n}'" for n in _accounts)
    raise ValueError(f"Account '{account}' not found. Available: {available}")

def _imap_connect(acct: Dict[str, Any]) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(acct["imap_host"], acct["imap_port"], ssl_context=ctx)
    conn.login(acct["address"], acct["password"])
    return conn

@contextmanager
def _imap_session(acct: Dict[str, Any], folder: Optional[str] = None, readonly: bool = True):
    conn = _imap_connect(acct)
    try:
        if folder:
            conn.select(folder, readonly=readonly)
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

# ─── Input models ───────────────────────────────────────────────────────────
class ListAccountsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

class ListFoldersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")

class ListEmailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")
    folder: str = Field(default="INBOX", description="IMAP folder")
    limit: int = Field(default=20, description="Max emails to return", ge=1, le=100)

class SearchEmailsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")
    query: str = Field(..., description="IMAP search query (e.g., 'FROM john', 'SUBJECT invoice', 'UNSEEN')", min_length=1)
    folder: str = Field(default="INBOX", description="IMAP folder to search in")
    limit: int = Field(default=20, ge=1, le=100, description="Max results")

class ReadEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")
    uid: str = Field(..., description="UID of the email", min_length=1)
    folder: str = Field(default="INBOX", description="IMAP folder")

class GetAttachmentInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")
    uid: str = Field(..., description="UID of the email containing the attachment", min_length=1)
    folder: str = Field(default="INBOX", description="IMAP folder")
    attachment_index: int = Field(..., description="Index of the attachment (from email_read_email)", ge=0)

class SaveAttachmentInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")
    uid: str = Field(..., description="UID of the email containing the attachment", min_length=1)
    folder: str = Field(default="INBOX", description="IMAP folder")
    attachment_index: int = Field(..., description="Index of the attachment (from email_read_email)", ge=0)
    save_path: str = Field(..., description="Full file path where to save the attachment (e.g., '/tmp/report.pdf')", min_length=1)

class SendEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    confirmation_code: Optional[str] = Field(default=None, description="Security code. Required only if send_code is set in accounts.json. Show draft to user first, get their code.")
    account: Optional[str] = Field(default=None, description="Account to send FROM.")
    to: str = Field(..., description="Recipient(s), comma-separated", min_length=3)
    subject: str = Field(..., description="Subject line", max_length=500)
    body: str = Field(..., description="Plain text body", min_length=1)
    body_html: Optional[str] = Field(default=None, description="HTML body (sends multipart/alternative)")
    cc: Optional[str] = Field(default=None, description="CC recipients")
    bcc: Optional[str] = Field(default=None, description="BCC recipients")
    reply_to_uid: Optional[str] = Field(default=None, description="UID to reply to")
    reply_to_folder: Optional[str] = Field(default="INBOX", description="Folder of reply-to email")
    attachments: Optional[str] = Field(default=None, description="Comma-separated file paths to attach")
    attachments_inline: Optional[str] = Field(default=None, description='JSON array: [{"filename":"f.pdf","content_base64":"...","content_type":"application/pdf"}]')
    calendar_ics: Optional[str] = Field(default=None, description="Raw ICS content for calendar invite (Accept/Decline buttons)")

    @field_validator("to")
    @classmethod
    def validate_to(cls, v: str) -> str:
        for addr in v.split(","):
            if "@" not in addr.strip():
                raise ValueError(f"Invalid email: {addr.strip()}")
        return v

class MoveEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")
    uid: str = Field(..., description="UID of the email")
    source_folder: str = Field(default="INBOX", description="Current folder")
    destination_folder: str = Field(..., description="Target folder")

class MarkEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    account: Optional[str] = Field(default=None, description="Account name or partial match.")
    uid: str = Field(..., description="UID of the email")
    folder: str = Field(default="INBOX", description="Folder")
    action: str = Field(..., description="'read', 'unread', 'flag', 'unflag'")

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"read", "unread", "flag", "unflag"}
        if v.lower() not in allowed:
            raise ValueError(f"Must be one of: {', '.join(allowed)}")
        return v.lower()

# ─── Tools ──────────────────────────────────────────────────────────────────

@mcp.tool(name="email_list_accounts", annotations={"title": "List Email Accounts", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
async def email_list_accounts(params: ListAccountsInput) -> str:
    """List all configured email accounts.\n\nShows the account name (used in the 'account' parameter of other tools)\nand the associated email address. Does NOT expose passwords.\n"""
    if not _accounts:
        return "No accounts configured. See README for setup."
    accounts_info = [{"name": n, "address": c["address"], "imap_host": c["imap_host"], "smtp_host": c["smtp_host"]} for n, c in _accounts.items()]
    return json.dumps({"accounts": accounts_info, "count": len(accounts_info)}, indent=2)

@mcp.tool(name="email_list_folders", annotations={"title": "List Email Folders", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_list_folders(params: ListFoldersInput) -> str:
    """List all available IMAP folders/mailboxes for an account.\n\nReturns folder names (both display name and raw IMAP name) you can use\nwith other email tools. The 'raw_name' is what you pass to folder parameters.\nHandles international folder names (German, etc.) via IMAP modified UTF-7.\n"""
    try:
        acct = _resolve_account(params.account)
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
            return json.dumps({"account": acct["address"], "folders": folder_info, "count": len(folder_info), "hint": "Use 'raw_name' when passing folder names to other email tools."}, indent=2)
    except Exception as e:
        return f"Error listing folders: {e}"

@mcp.tool(name="email_list_emails", annotations={"title": "List Recent Emails", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_list_emails(params: ListEmailsInput) -> str:
    """List the most recent emails in a folder.\n\nReturns summaries (UID, from, to, subject, date, has_attachments)\nfor the N most recent emails, newest first.\n\nUse the UID from results with email_read_email to get full content.\n"""
    try:
        acct = _resolve_account(params.account)
        with _imap_session(acct, folder=params.folder) as conn:
            status, data = conn.uid("search", None, "ALL")
            if status != "OK":
                return f"Error: Could not search folder '{params.folder}'."
            uids = data[0].split() if data[0] else []
            uids = uids[-params.limit:]
            uids.reverse()
            results = []
            for uid_bytes in uids:
                uid = uid_bytes.decode()
                status, msg_data = conn.uid("fetch", uid, "(RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]: continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if isinstance(raw, bytes):
                    results.append(_msg_to_summary(email.message_from_bytes(raw), uid))
            return json.dumps({"account": acct["address"], "folder": params.folder, "count": len(results), "emails": results}, indent=2)
    except Exception as e:
        return f"Error listing emails: {e}"

@mcp.tool(name="email_search_emails", annotations={"title": "Search Emails", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_search_emails(params: SearchEmailsInput) -> str:
    """Search emails using IMAP search criteria.\n\nSupports standard IMAP search keys:\n  - FROM <sender>      - emails from a specific sender\n  - TO <recipient>     - emails to a specific recipient\n  - SUBJECT <text>     - subject contains text\n  - BODY <text>        - body contains text\n  - SINCE <date>       - since date (format: 01-Jan-2025)\n  - BEFORE <date>      - before date\n  - UNSEEN             - unread emails\n  - SEEN / FLAGGED / ALL\n\nCombine: 'FROM john SUBJECT meeting SINCE 01-Feb-2025'\n"""
    try:
        acct = _resolve_account(params.account)
        with _imap_session(acct, folder=params.folder) as conn:
            try:
                status, data = conn.uid("search", None, params.query)
            except imaplib.IMAP4.error as e:
                return f"Error: Invalid IMAP search query '{params.query}': {e}"
            if status != "OK":
                return f"Error: Search failed for query '{params.query}'. Check query syntax."
            uids = data[0].split() if data[0] else []
            uids = uids[-params.limit:]
            uids.reverse()
            results = []
            for uid_bytes in uids:
                uid = uid_bytes.decode()
                status, msg_data = conn.uid("fetch", uid, "(RFC822.HEADER)")
                if status != "OK" or not msg_data or not msg_data[0]: continue
                raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                if isinstance(raw, bytes):
                    results.append(_msg_to_summary(email.message_from_bytes(raw), uid))
            return json.dumps({"account": acct["address"], "query": params.query, "folder": params.folder, "count": len(results), "emails": results}, indent=2)
    except Exception as e:
        return f"Error searching emails: {e}"

@mcp.tool(name="email_read_email", annotations={"title": "Read Email", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_read_email(params: ReadEmailInput) -> str:
    """Read the full content of an email by its UID.\n\nReturns headers, body text, and attachment metadata (with index for downloading).\nUse email_get_attachment with the attachment index to download attachment content.\n"""
    try:
        acct = _resolve_account(params.account)
        with _imap_session(acct, folder=params.folder) as conn:
            status, msg_data = conn.uid("fetch", params.uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {params.uid}."
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
            return json.dumps({"account": acct["address"], "uid": params.uid, "from": _decode_header_value(msg.get("From")), "to": _decode_header_value(msg.get("To")), "cc": _decode_header_value(msg.get("Cc")), "subject": _decode_header_value(msg.get("Subject")), "date": date_formatted, "message_id": msg.get("Message-ID", ""), "in_reply_to": msg.get("In-Reply-To", ""), "body": body[:50000], "attachments": attachments}, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error reading email: {e}"

@mcp.tool(name="email_get_attachment", annotations={"title": "Get Email Attachment", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_get_attachment(params: GetAttachmentInput) -> str:
    """Download an email attachment as base64-encoded content.\n\nUse email_read_email first to get the list of attachments with their indices,\nthen call this tool with the desired attachment_index.\n\nReturns JSON with filename, content_type, size_bytes, and content_base64.\n"""
    try:
        acct = _resolve_account(params.account)
        with _imap_session(acct, folder=params.folder) as conn:
            status, msg_data = conn.uid("fetch", params.uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {params.uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            for idx, part in enumerate(msg.walk()):
                if idx == params.attachment_index:
                    disp = str(part.get("Content-Disposition", ""))
                    if "attachment" not in disp:
                        return f"Error: Part at index {idx} is not an attachment."
                    filename = _decode_header_value(part.get_filename()) or "(unnamed)"
                    payload = part.get_payload(decode=True) or b""
                    return json.dumps({"filename": filename, "content_type": part.get_content_type(), "size_bytes": len(payload), "content_base64": base64.b64encode(payload).decode("ascii")}, indent=2)
            return f"Error: No part found at index {params.attachment_index}."
    except Exception as e:
        return f"Error getting attachment: {e}"

@mcp.tool(name="email_save_attachment", annotations={"title": "Save Email Attachment to Disk", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_save_attachment(params: SaveAttachmentInput) -> str:
    """Save an email attachment directly to disk without returning its content.\n\nPreferred over email_get_attachment for large files — avoids flooding the\nconversation with base64 data. Use email_read_email first to see attachment\nindices, then call this with the desired index and a save path.\n"""
    try:
        acct = _resolve_account(params.account)
        with _imap_session(acct, folder=params.folder) as conn:
            status, msg_data = conn.uid("fetch", params.uid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                return f"Error: Could not fetch email UID {params.uid}."
            raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
            if not isinstance(raw, bytes):
                return "Error: Unexpected response format."
            msg = email.message_from_bytes(raw)
            for idx, part in enumerate(msg.walk()):
                if idx == params.attachment_index:
                    disp = str(part.get("Content-Disposition", ""))
                    if "attachment" not in disp:
                        return f"Error: Part at index {idx} is not an attachment."
                    filename = _decode_header_value(part.get_filename()) or "(unnamed)"
                    payload = part.get_payload(decode=True) or b""
                    save_dir = os.path.dirname(params.save_path)
                    if save_dir and not os.path.isdir(save_dir):
                        os.makedirs(save_dir, exist_ok=True)
                    with open(params.save_path, "wb") as f:
                        f.write(payload)
                    return json.dumps({"saved": params.save_path, "filename": filename, "content_type": part.get_content_type(), "size_bytes": len(payload)}, indent=2)
            return f"Error: No part found at index {params.attachment_index}."
    except Exception as e:
        return f"Error saving attachment: {e}"

@mcp.tool(name="email_send_email", annotations={"title": "Send Email", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def email_send_email(params: SendEmailInput) -> str:
    """Send an email via SMTP.\n\nIf send_code is configured in accounts.json, a confirmation_code is required.\nShow the user the full draft (to, subject, body) and wait for their code.\n\nSupports: plain text, HTML, file attachments, base64 inline attachments,\nand calendar invites (ICS with Accept/Decline buttons).\n\nAll sent emails are automatically saved to the Sent folder.\n"""
    if _send_code:
        if not params.confirmation_code:
            return "BLOCKED: A confirmation code is required. Show the draft to the user and ask for their code."
        if params.confirmation_code.strip() != _send_code.strip():
            return "BLOCKED: Invalid confirmation code. The email was NOT sent."
    try:
        acct = _resolve_account(params.account)
        mime = MIMEMultipart("mixed")
        mime["From"] = acct["address"]
        mime["To"] = params.to
        mime["Subject"] = params.subject
        mime["Date"] = formatdate(localtime=True)
        mime["Message-ID"] = make_msgid(domain=acct["address"].split("@")[1])
        if params.cc: mime["Cc"] = params.cc
        if params.reply_to_uid:
            try:
                with _imap_session(acct, folder=params.reply_to_folder or "INBOX") as conn:
                    status, msg_data = conn.uid("fetch", params.reply_to_uid, "(RFC822.HEADER)")
                    if status == "OK" and msg_data and msg_data[0]:
                        raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                        if isinstance(raw, bytes):
                            orig_id = email.message_from_bytes(raw).get("Message-ID", "")
                            if orig_id:
                                mime["In-Reply-To"] = orig_id
                                mime["References"] = orig_id
            except Exception: pass
        if params.calendar_ics:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(params.body, "plain", "utf-8"))
            ics_part = MIMEText(params.calendar_ics, "calendar", "utf-8")
            ics_part.replace_header("Content-Type", "text/calendar; method=REQUEST; charset=utf-8")
            alt.attach(ics_part)
            mime.attach(alt)
        elif params.body_html:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(params.body, "plain", "utf-8"))
            alt.attach(MIMEText(params.body_html, "html", "utf-8"))
            mime.attach(alt)
        else:
            mime.attach(MIMEText(params.body, "plain", "utf-8"))
        if params.attachments:
            for filepath in params.attachments.split(","):
                filepath = filepath.strip()
                if not filepath: continue
                if not os.path.isfile(filepath):
                    return f"Error: Attachment not found: {filepath}"
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
        if params.attachments_inline:
            try:
                for att in json.loads(params.attachments_inline):
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
                return "Error: attachments_inline must be a valid JSON array."
            except Exception as e:
                return f"Error processing inline attachment: {e}"
        recipients = [a.strip() for a in params.to.split(",")]
        if params.cc: recipients += [a.strip() for a in params.cc.split(",")]
        if params.bcc: recipients += [a.strip() for a in params.bcc.split(",")]
        mime_str = mime.as_string()
        _smtp_send(acct, acct["address"], recipients, mime_str)
        sent_warning = _save_to_sent(acct, mime_str)
        result = {"status": "sent", "from": acct["address"], "to": params.to, "subject": params.subject, "cc": params.cc, "bcc": params.bcc}
        if sent_warning:
            result["warning"] = sent_warning
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error sending email: {e}"

@mcp.tool(name="email_move_email", annotations={"title": "Move Email", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_move_email(params: MoveEmailInput) -> str:
    """Move an email to another folder (e.g., INBOX -> Trash or Archive)."""
    try:
        acct = _resolve_account(params.account)
        with _imap_session(acct, folder=params.source_folder, readonly=False) as conn:
            status, _ = conn.uid("copy", params.uid, params.destination_folder)
            if status != "OK":
                return f"Error: Could not copy email to '{params.destination_folder}'."
            conn.uid("store", params.uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
            return json.dumps({"status": "moved", "account": acct["address"], "uid": params.uid, "from_folder": params.source_folder, "to_folder": params.destination_folder}, indent=2)
    except Exception as e:
        return f"Error moving email: {e}"

@mcp.tool(name="email_mark_email", annotations={"title": "Mark Email", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def email_mark_email(params: MarkEmailInput) -> str:
    """Mark an email as read, unread, flagged, or unflagged."""
    try:
        acct = _resolve_account(params.account)
        with _imap_session(acct, folder=params.folder, readonly=False) as conn:
            flag_map = {"read": ("+FLAGS", "(\\Seen)"), "unread": ("-FLAGS", "(\\Seen)"), "flag": ("+FLAGS", "(\\Flagged)"), "unflag": ("-FLAGS", "(\\Flagged)")}
            op, flag = flag_map[params.action]
            status, _ = conn.uid("store", params.uid, op, flag)
            if status != "OK":
                return f"Error: Could not mark email as '{params.action}'."
            return json.dumps({"status": "success", "account": acct["address"], "uid": params.uid, "action": params.action}, indent=2)
    except Exception as e:
        return f"Error marking email: {e}"

def main():
    mcp.run()

if __name__ == "__main__":
    main()
