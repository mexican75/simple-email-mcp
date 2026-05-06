# Changelog

## 2.0.0

### Changed

- Replaced the many individual MCP tools with a single `email` dispatcher tool and lazy action discovery to reduce client context use.

### Added

- Added `send_as` account configuration and `SEND_AS` environment variable support for provider-authorized sender aliases.
- Added `display_name`/`from_name` and `description` account metadata. `display_name` is used in the `From` header, and `description` is returned by `list_accounts`.

### Fixed

- `reply_all` now excludes both the login address and configured sender alias from copied recipients.
- Parameterless actions such as `list_accounts` can now be executed with `params={}` while still supporting schema discovery when `params` is omitted.
