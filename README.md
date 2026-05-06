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
- **Compact MCP surface** — one `email` tool with lazy action discovery to reduce client context use

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
      "send_as": "alias@company.com",
      "display_name": "Jane Doe",
      "description": "Primary work mailbox",
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

Config is reloaded on each tool call, so changes to `accounts.json` such as rotating `send_code` take effect without restarting the MCP server.

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `send_code` | No | If set, users must provide this code to send emails. Omit or set to `""` to disable. |
| `name` | Yes | Short identifier for the account (used in tool calls) |
| `address` | Yes | Email address used for IMAP/SMTP login |
| `send_as` | No | Alias address to use as the `From` address, SMTP envelope sender, and Message-ID domain. Defaults to `address`. The alias must be authorized by your email provider. |
| `display_name` / `from_name` | No | Friendly sender name used in the `From` header, e.g. `Jane Doe <alias@example.com>` |
| `description` | No | Human-readable label shown by the `list_accounts` action to help clients choose the right mailbox |
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
SEND_AS=alias@example.com
EMAIL_DISPLAY_NAME="Jane Doe"
EMAIL_DESCRIPTION="Primary mailbox"
SEND_CODE=optional
```

## Tools

Version 2 exposes a single MCP tool named `email`. Call it with only an `action` to discover that action's parameters, then call it again with `params`.

```json
{"action": "send"}
```

```json
{
  "action": "send",
  "params": {
    "account": "work",
    "to": "recipient@example.com",
    "subject": "Hello",
    "body": "Message body"
  }
}
```

| Action | Description |
|--------|-------------|
| `validate_config` | Validate config without logging into IMAP/SMTP |
| `list_accounts` | List configured accounts |
| `list_folders` | List IMAP folders for an account |
| `list_emails` | List recent emails in a folder |
| `search` | Search emails using IMAP criteria |
| `read` | Read full email content by UID |
| `get_attachment` | Download an attachment as base64 |
| `prepare_attachments` | Inspect local attachment paths before sending |
| `save_attachment` | Save an attachment directly to disk (preferred for large files) |
| `send` | Send an email (text, HTML, attachments, calendar invites) |
| `reply` | Reply to an email (auto-sets recipient, subject, threading, quotes body) |
| `reply_all` | Reply all (sender to To, other recipients to CC, quotes body) |
| `forward` | Forward an email with original attachments |
| `move` | Move an email between folders |
| `mark` | Mark as read/unread/flagged/unflagged |

`list_accounts` returns the exact account names plus any configured `send_as`, `display_name`, and `description`, so clients can use the explicit account token instead of guessing partial matches.

### Configuration validation

Use `validate_config` after editing `accounts.json` or environment variables. It checks required fields, email-like addresses, ports, SMTP security, providers, and placeholder hosts without exposing passwords or logging into IMAP/SMTP.

```json
{
  "action": "validate_config",
  "params": {}
}
```

### Migrating from v1

Most users do not need to change their MCP client configuration. Keep the same `simple-email-mcp` command and restart the client after upgrading.

The breaking change only affects clients or scripts that call exact v1 tool names such as `email_send_email` or `email_read_email`. In v2, use the single `email` tool with an action instead:

| v1 tool | v2 action |
|---------|-----------|
| `email_list_accounts` | `email` with `action: "list_accounts"` |
| `email_send_email` | `email` with `action: "send"` |
| `email_read_email` | `email` with `action: "read"` |
| `email_search_emails` | `email` with `action: "search"` |
| `email_forward` | `email` with `action: "forward"` |
| `email_reply_all` | `email` with `action: "reply_all"` |

### Sending with attachments

**Preflight metadata only** (recommended before send):
```json
attachments: "/path/to/file.pdf, /path/to/doc.xlsx"
```
Call `prepare_attachments` first to verify resolved paths, file names, sizes, MIME types, and missing files without loading contents into context.

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

If `send_code` is set in `accounts.json`, the AI must show the email draft to the user and wait for them to provide the code before sending. This is useful as a workflow checkpoint to reduce accidental sends.

Important: this is not a hard security boundary if the MCP process and the AI runtime can both read the same config source. In that setup, the AI may be able to read the code from `accounts.json` or environment variables. Remove or clear `send_code` to disable the checkpoint.

## Testing

Run the regression suite from the repo root:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Security

- Passwords are stored in `accounts.json` — **add it to `.gitignore`**
- The `send_code` gate is a user-intent checkpoint, not a hard secret, unless the AI cannot read the config source that contains it
- No passwords are exposed via the `list_accounts` action
- **File attachments**: The `attachments` parameter reads files from paths the AI provides. If the MCP server runs with broad filesystem access, the AI could theoretically attach and send any readable file. Use `attachments_inline` (base64) in sandboxed environments, or restrict filesystem access at the OS/container level.
- **Saving attachments**: `save_attachment` fails if the target file already exists unless `overwrite=true` is set explicitly.

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
