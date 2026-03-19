# simple-email-mcp

A provider-agnostic MCP server for email (IMAP/SMTP). Works with any email provider — Purelymail, Gmail, Outlook, DomainFactory, or any standard IMAP/SMTP server.

Built for [Claude Desktop](https://claude.ai), [Claude Code](https://claude.com/claude-code), and any MCP-compatible client.

## Features

- **Multi-account** — manage multiple email accounts from different providers
- **Read, search, list** — full IMAP support with folder browsing
- **Send emails** — plain text, HTML, or both (multipart/alternative)
- **Attachments** — send via file path or base64-encoded inline data
- **Download attachments** — extract attachments from received emails as base64
- **Calendar invites** — send proper ICS invitations with Accept/Decline buttons
- **Save to Sent** — automatically saves sent emails to the Sent folder via IMAP
- **Optional send gate** — configurable confirmation code to prevent accidental sends
- **International folders** — handles UTF-7 encoded folder names (German, etc.)

## Quick Start

### 1. Install

```bash
pip install simple-email-mcp
```

Or from source:

```bash
git clone https://github.com/mexican75/simple-email-mcp.git
cd simple-email-mcp
pip install .
```

### 2. Create `accounts.json`

```json
{
  "accounts": [
    {
      "name": "personal",
      "address": "me@example.com",
      "password": "your-app-password",
      "provider": "gmail"
    }
  ]
}
```

### 3. Add to your client

**Claude Code** (global, all projects):

```bash
claude mcp add email -s user -e ACCOUNTS_FILE=/path/to/accounts.json -- simple-email-mcp
```

**Claude Desktop** — add to config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "email": {
      "command": "simple-email-mcp"
    }
  }
}
```

Or if running from source:

```json
{
  "mcpServers": {
    "email": {
      "command": "python",
      "args": ["/path/to/simple_email_mcp.py"]
    }
  }
}
```

### 4. Restart your client

## Configuration

### accounts.json

```json
{
  "send_code": "MYSECRETCODE",
  "accounts": [
    {
      "name": "work",
      "address": "me@company.com",
      "password": "app-password",
      "provider": "outlook"
    },
    {
      "name": "personal",
      "address": "me@gmail.com",
      "password": "app-password",
      "provider": "gmail"
    },
    {
      "name": "custom",
      "address": "me@mydomain.com",
      "password": "password",
      "imap_host": "mail.mydomain.com",
      "imap_port": 993,
      "smtp_host": "mail.mydomain.com",
      "smtp_port": 587,
      "smtp_security": "starttls"
    }
  ]
}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `send_code` | No | If set, users must provide this code to send emails. Omit or set to `""` to disable. |
| `name` | Yes | Short identifier for the account (used in tool calls) |
| `address` | Yes | Email address |
| `password` | Yes | Password or app-specific password |
| `provider` | No | Preset: `gmail`, `outlook`, `purelymail`, `domainfactory` |
| `imap_host` | No | Custom IMAP server (overrides provider default) |
| `imap_port` | No | Custom IMAP port (default: 993) |
| `smtp_host` | No | Custom SMTP server (overrides provider default) |
| `smtp_port` | No | Custom SMTP port (default: 465) |
| `smtp_security` | No | `ssl` (port 465) or `starttls` (port 587). Auto-detected from port if omitted. |

### Environment variables (single account)

Instead of `accounts.json`, you can configure a single account via environment variables:

```
EMAIL_ADDRESS=me@example.com
EMAIL_PASSWORD=password
IMAP_HOST=imap.example.com
SMTP_HOST=smtp.example.com
SMTP_SECURITY=ssl
SEND_CODE=optional
```

## Tools

| Tool | Description |
|------|-------------|
| `email_list_accounts` | List configured accounts |
| `email_list_folders` | List IMAP folders for an account |
| `email_list_emails` | List recent emails in a folder |
| `email_search_emails` | Search emails using IMAP criteria |
| `email_read_email` | Read full email content by UID |
| `email_get_attachment` | Download an attachment as base64 |
| `email_save_attachment` | Save an attachment directly to disk (preferred for large files) |
| `email_send_email` | Send an email (text, HTML, attachments, calendar invites) |
| `email_move_email` | Move an email between folders |
| `email_mark_email` | Mark as read/unread/flagged/unflagged |

### Sending with attachments

**File path** (when the MCP server has filesystem access):
```
attachments: "/path/to/file.pdf, /path/to/doc.xlsx"
```

**Base64 inline** (when the caller is in a sandbox):
```json
attachments_inline: [{"filename": "report.pdf", "content_base64": "JVBERi0...", "content_type": "application/pdf"}]
```

### Sending calendar invites

Pass raw ICS content via `calendar_ics`. The email is structured as `multipart/alternative` so clients display Accept/Decline buttons:

```
calendar_ics: "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n..."
```

### Send confirmation gate

If `send_code` is set in `accounts.json`, the AI must show the email draft to the user and wait for them to provide the code before sending. This prevents accidental sends. Remove or clear `send_code` to disable.

## Security

- Passwords are stored in `accounts.json` — **add it to `.gitignore`**
- The `send_code` gate prevents the AI from sending emails without user approval
- No passwords are exposed via the `email_list_accounts` tool
- **File attachments**: The `attachments` parameter reads files from paths the AI provides. If the MCP server runs with broad filesystem access, the AI could theoretically attach and send any readable file. Use `attachments_inline` (base64) in sandboxed environments, or restrict filesystem access at the OS/container level.

## Provider Notes

### Gmail
Use an [App Password](https://support.google.com/accounts/answer/185833) (not your Google password). Enable IMAP in Gmail settings.

### Outlook / Microsoft 365
Use an [App Password](https://support.microsoft.com/en-us/account-billing/using-app-passwords-with-apps-that-don-t-support-two-step-verification-5896ed9b-4263-e681-128a-a6f2979a7944) or enable basic auth for IMAP/SMTP.

### Purelymail
Use your Purelymail account password directly.

## License

MIT — see [LICENSE](LICENSE)

## Authors

- Ramon Ramirez ([@mexican75](https://github.com/mexican75))
