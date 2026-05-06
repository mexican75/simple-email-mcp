import asyncio
import contextlib
import email
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "simple_email_mcp.py"


def load_module():
    spec = importlib.util.spec_from_file_location("simple_email_mcp_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ResolveAccountTests(unittest.TestCase):
    def test_exact_address_match_is_case_insensitive(self):
        mod = load_module()
        with mock.patch.dict(
            mod._accounts,
            {
                "Work": {"address": "Sender@Example.com"},
                "Personal": {"address": "me@example.net"},
            },
            clear=True,
        ), mock.patch.object(mod, "_refresh_runtime_config"):
            resolved = mod._resolve_account("sender@example.com")
        self.assertEqual(resolved["address"], "Sender@Example.com")

    def test_format_from_header_uses_display_name_when_present(self):
        mod = load_module()
        header = mod._format_from_header({"address": "sender@example.com", "display_name": "Sender Name"})
        self.assertEqual(header, "Sender Name <sender@example.com>")

    def test_format_from_header_uses_send_as_with_display_name(self):
        mod = load_module()
        header = mod._format_from_header(
            {
                "address": "login@example.com",
                "send_as": "alias@example.com",
                "display_name": "Sender Name",
            }
        )
        self.assertEqual(header, "Sender Name <alias@example.com>")

    def test_ambiguous_partial_match_fails_closed(self):
        mod = load_module()
        with mock.patch.dict(
            mod._accounts,
            {
                "work-us": {"address": "team@company.com"},
                "work-eu": {"address": "team-eu@company.com"},
            },
            clear=True,
        ), mock.patch.object(mod, "_refresh_runtime_config"):
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                mod._resolve_account("work")


class ImapSessionTests(unittest.TestCase):
    def test_select_failure_raises_clear_error(self):
        mod = load_module()

        class FakeConn:
            def select(self, folder, readonly=True):
                return "NO", [b"Mailbox does not exist"]

            def logout(self):
                return "BYE", [b"logged out"]

        with mock.patch.object(mod, "_imap_connect", return_value=FakeConn()), mock.patch.object(mod.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Could not select folder 'Missing'"):
                with mod._imap_session({"address": "sender@example.com"}, folder="Missing"):
                    pass


class ReplyAllTests(unittest.TestCase):
    def test_reply_all_uses_rfc_aware_address_parsing_and_excludes_alias(self):
        mod = load_module()
        original = email.message_from_string(
            "\n".join(
                [
                    'From: "Support" <support@example.com>',
                    'To: "Doe, John" <john@example.com>, Jane <jane@example.com>, Alias <alias@example.com>',
                    'Cc: "Smith, Ann" <ann@example.com>, Me <me@example.com>',
                    "Subject: Status Update",
                    "Date: Tue, 24 Mar 2026 09:00:00 +0000",
                    "Message-ID: <orig@example.com>",
                    "",
                    "Original body",
                ]
            )
        )

        class FakeConn:
            def uid(self, command, uid, query):
                self.last_call = (command, uid, query)
                return "OK", [(b"1 (RFC822 {0})", original.as_bytes())]

            def logout(self):
                return "BYE", [b"logged out"]

        @contextlib.contextmanager
        def fake_imap_session(acct, folder=None, readonly=True):
            yield FakeConn()

        captured = {}

        def fake_compose_and_send(acct, to, subject, body, **kwargs):
            captured["to"] = to
            captured["cc"] = kwargs.get("cc")
            captured["subject"] = subject
            return {"status": "sent"}

        with mock.patch.dict(mod._accounts, {"default": {"address": "me@example.com", "send_as": "alias@example.com"}}, clear=True), \
             mock.patch.object(mod, "_refresh_runtime_config"), \
             mock.patch.object(mod, "_send_code", None), \
             mock.patch.object(mod, "_imap_session", fake_imap_session), \
             mock.patch.object(mod, "_compose_and_send", side_effect=fake_compose_and_send):
            result = asyncio.run(mod._do_reply_all({"uid": "123", "folder": "INBOX", "body": "Thanks"}))

        self.assertIn('"status": "sent"', result)
        self.assertEqual(captured["to"], "support@example.com")
        self.assertEqual(captured["cc"], "john@example.com, jane@example.com, ann@example.com")
        self.assertEqual(captured["subject"], "Re: Status Update")


class AttachmentWorkflowTests(unittest.TestCase):
    def test_prepare_attachments_returns_metadata_without_reading_contents(self):
        mod = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = Path(tmpdir) / "report.txt"
            existing.write_text("hello")
            missing = Path(tmpdir) / "missing.pdf"

            result = json.loads(asyncio.run(mod._do_prepare_attachments(f"{existing}, {missing}")))

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["existing_count"], 1)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["missing"], [str(missing)])
        self.assertEqual(result["total_size_bytes"], 5)
        self.assertEqual(result["attachments"][0]["filename"], "report.txt")
        self.assertTrue(result["attachments"][0]["exists"])
        self.assertFalse(result["attachments"][1]["exists"])

    def test_save_attachment_requires_explicit_overwrite(self):
        mod = load_module()
        message = email.message_from_string(
            "\n".join(
                [
                    "From: sender@example.com",
                    "To: me@example.com",
                    "Subject: Attachment Test",
                    "MIME-Version: 1.0",
                    'Content-Type: multipart/mixed; boundary="boundary"',
                    "",
                    "--boundary",
                    'Content-Type: text/plain; charset="utf-8"',
                    "",
                    "Body",
                    "--boundary",
                    "Content-Type: text/plain",
                    'Content-Disposition: attachment; filename="test.txt"',
                    "Content-Transfer-Encoding: base64",
                    "",
                    "aGVsbG8=",
                    "--boundary--",
                ]
            )
        )

        class FakeConn:
            def uid(self, command, uid, query):
                return "OK", [(b"1 (RFC822 {0})", message.as_bytes())]

            def logout(self):
                return "BYE", [b"logged out"]

        @contextlib.contextmanager
        def fake_imap_session(acct, folder=None, readonly=True):
            yield FakeConn()

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "test.txt"
            destination.write_text("old")

            with mock.patch.dict(mod._accounts, {"default": {"address": "me@example.com"}}, clear=True), \
                 mock.patch.object(mod, "_refresh_runtime_config"), \
                 mock.patch.object(mod, "_imap_session", fake_imap_session):
                blocked = asyncio.run(
                    mod._do_save_attachment(
                        account=None,
                        uid="1",
                        folder="INBOX",
                        attachment_index=2,
                        save_path=str(destination),
                        overwrite=False,
                    )
                )
                self.assertIn("overwrite=true", blocked)
                self.assertEqual(destination.read_text(), "old")

                saved = json.loads(
                    asyncio.run(
                        mod._do_save_attachment(
                            account=None,
                            uid="1",
                            folder="INBOX",
                            attachment_index=2,
                            save_path=str(destination),
                            overwrite=True,
                        )
                    )
                )

            self.assertEqual(destination.read_text(), "hello")
            self.assertTrue(saved["overwritten"])
            self.assertEqual(saved["saved"], str(destination))


class RuntimeConfigReloadTests(unittest.TestCase):
    def test_list_accounts_reflects_updated_accounts_file_without_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            config_path.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {"name": "alpha", "address": "alpha@example.com", "password": "one"},
                        ]
                    }
                )
            )

            with mock.patch.dict("os.environ", {"ACCOUNTS_FILE": str(config_path)}, clear=False):
                mod = load_module()
                first = json.loads(asyncio.run(mod._do_list_accounts()))

                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": [
                                {"name": "beta", "address": "beta@example.com", "password": "two"},
                            ]
                        }
                    )
                )
                second = json.loads(asyncio.run(mod._do_list_accounts()))

        self.assertEqual(first["accounts"][0]["name"], "alpha")
        self.assertEqual(second["accounts"][0]["name"], "beta")

    def test_list_accounts_includes_display_name_and_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            config_path.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "name": "alpha",
                                "address": "alpha@example.com",
                                "password": "one",
                                "send_as": "alias@example.com",
                                "display_name": "Alpha Sender",
                                "description": "Main personal mailbox",
                            },
                        ]
                    }
                )
            )

            with mock.patch.dict("os.environ", {"ACCOUNTS_FILE": str(config_path)}, clear=False):
                mod = load_module()
                result = json.loads(asyncio.run(mod._do_list_accounts()))

        self.assertEqual(result["accounts"][0]["display_name"], "Alpha Sender")
        self.assertEqual(result["accounts"][0]["description"], "Main personal mailbox")
        self.assertEqual(result["accounts"][0]["send_as"], "alias@example.com")

    def test_env_send_as_applies_to_default_account(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_config = Path(tmpdir) / "missing-accounts.json"
            env = {
                "ACCOUNTS_FILE": str(missing_config),
                "EMAIL_ADDRESS": "login@example.com",
                "EMAIL_PASSWORD": "secret",
                "SEND_AS": "alias@example.com",
            }

            with mock.patch.dict("os.environ", env, clear=False):
                mod = load_module()

        self.assertEqual(mod._accounts["default"]["address"], "login@example.com")
        self.assertEqual(mod._accounts["default"]["send_as"], "alias@example.com")

    def test_send_code_changes_apply_without_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "accounts.json"
            config_path.write_text(
                json.dumps(
                    {
                        "send_code": "first-code",
                        "accounts": [
                            {"name": "alpha", "address": "alpha@example.com", "password": "one"},
                        ],
                    }
                )
            )

            with mock.patch.dict("os.environ", {"ACCOUNTS_FILE": str(config_path)}, clear=False):
                mod = load_module()

                with mock.patch.object(mod, "_compose_and_send", return_value={"status": "sent"}):
                    first_attempt = asyncio.run(
                        mod._do_send(
                            {
                                "confirmation_code": "first-code",
                                "account": "alpha",
                                "to": "dest@example.com",
                                "subject": "test",
                                "body": "hello",
                            }
                        )
                    )

                    config_path.write_text(
                        json.dumps(
                            {
                                "send_code": "second-code",
                                "accounts": [
                                    {"name": "alpha", "address": "alpha@example.com", "password": "one"},
                                ],
                            }
                        )
                    )

                    stale_code_attempt = asyncio.run(
                        mod._do_send(
                            {
                                "confirmation_code": "first-code",
                                "account": "alpha",
                                "to": "dest@example.com",
                                "subject": "test",
                                "body": "hello",
                            }
                        )
                    )

                    fresh_code_attempt = asyncio.run(
                        mod._do_send(
                            {
                                "confirmation_code": "second-code",
                                "account": "alpha",
                                "to": "dest@example.com",
                                "subject": "test",
                                "body": "hello",
                            }
                        )
                    )

        self.assertIn('"status": "sent"', first_attempt)
        self.assertIn("Invalid confirmation code", stale_code_attempt)
        self.assertIn('"status": "sent"', fresh_code_attempt)


class SendAsTests(unittest.TestCase):
    def test_compose_and_send_uses_send_as_for_headers_envelope_and_result(self):
        mod = load_module()
        captured = {}

        def fake_smtp_send(acct, sender, recipients, mime_str):
            captured["sender"] = sender
            captured["recipients"] = recipients
            captured["message"] = email.message_from_string(mime_str)

        acct = {
            "address": "login@example.com",
            "send_as": "alias@example.net",
            "display_name": "Sender Name",
            "smtp_security": "ssl",
        }

        with mock.patch.object(mod, "_smtp_send", side_effect=fake_smtp_send), \
             mock.patch.object(mod, "_save_to_sent", return_value=None):
            result = mod._compose_and_send(
                acct,
                to="dest@example.com",
                subject="hello",
                body="body",
                cc="copy@example.com",
            )

        self.assertEqual(result["from"], "alias@example.net")
        self.assertEqual(captured["sender"], "alias@example.net")
        self.assertEqual(captured["recipients"], ["dest@example.com", "copy@example.com"])
        self.assertEqual(captured["message"]["From"], "Sender Name <alias@example.net>")
        self.assertTrue(captured["message"]["Message-ID"].endswith("@example.net>"))

    def test_dispatcher_discovers_and_runs_list_accounts(self):
        mod = load_module()
        with mock.patch.dict(
            mod._accounts,
            {"default": {"address": "login@example.com", "send_as": "alias@example.com", "imap_host": "imap", "smtp_host": "smtp"}},
            clear=True,
        ), mock.patch.object(mod, "_refresh_runtime_config"):
            schema = json.loads(asyncio.run(mod.email_dispatcher("list_accounts")))
            result = json.loads(asyncio.run(mod.email_dispatcher("list_accounts", {})))

        self.assertIn("params", schema)
        self.assertEqual(result["accounts"][0]["send_as"], "alias@example.com")


if __name__ == "__main__":
    unittest.main()
