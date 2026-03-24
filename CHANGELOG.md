# Changelog

## Unreleased

### Added

- `send_as` configuration field: optional alias address used as the `From` header and SMTP envelope sender, while authenticating with the original `address`. Supported in both `accounts.json` and environment variable (`SEND_AS`) configuration.
