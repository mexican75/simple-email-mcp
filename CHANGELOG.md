# Changelog

## 2.2.0

### Fixed

- Attachment detection (`read`, `save_attachment`, `get_attachment`) no longer requires `Content-Disposition: attachment`. Real-world clients such as Apple Mail send file attachments as `Content-Disposition: inline` or with no `Content-Disposition` header at all, relying on a `filename`/`name` parameter instead; these are now correctly detected as attachments rather than being silently dropped.
- Body extraction now uses the same attachment classification, so a `text/plain` or `text/html` part carrying a filename is never mistaken for the message body.
- `forward` with `include_attachments` now carries inline-dispositioned file attachments through instead of silently dropping them.

### Added

- The `attachments` list returned by `read` now includes a `disposition` field (`attachment`, `inline`, or empty) per entry, so callers can distinguish inline images (e.g. signature logos) from classic attachments.

## 2.1.1

### Fixed

- Fixed `reply` and `reply_all` for Outlook messages whose display names contain unquoted commas (for example, `Doe, Jane <jane@example.com>`).
- SMTP envelope recipients are now parsed as RFC address lists instead of being split on commas, preserving quoted display names and rejecting invalid recipients before delivery.
- Constrained the MCP runtime dependency to compatible 1.x releases; MCP 2.0 removes the FastMCP import used by this server.

## 2.1.0

### Added

- Added `validate_config` action to check `accounts.json` or environment configuration without logging into IMAP/SMTP.
- Added v1-to-v2 migration notes for clients with hardcoded old tool names.

## 2.0.0

### Changed

- Replaced the many individual MCP tools with a single `email` dispatcher tool and lazy action discovery to reduce client context use.

### Added

- Added `send_as` account configuration and `SEND_AS` environment variable support for provider-authorized sender aliases.
- Added `display_name`/`from_name` and `description` account metadata. `display_name` is used in the `From` header, and `description` is returned by `list_accounts`.

### Fixed

- `reply_all` now excludes both the login address and configured sender alias from copied recipients.
- Parameterless actions such as `list_accounts` can now be executed with `params={}` while still supporting schema discovery when `params` is omitted.
