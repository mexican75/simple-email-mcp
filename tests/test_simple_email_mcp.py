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
    def test_reply_all_uses_rfc_aware_address_parsing(self):
        mod = load_module()
        original = email.message_from_string(
            "\n".join(
                [
                    'From: "Support" <support@example.com>',
                    'To: "Doe, John" <john@example.com>, Jane <jane@example.com>',
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

        with mock.patch.dict(mod._accounts, {"default": {"address": "me@example.com"}}, clear=True), \
             mock.patch.object(mod, "_refresh_runtime_config"), \
             mock.patch.object(mod, "_send_code", None), \
             mock.patch.object(mod, "_imap_session", fake_imap_session), \
             mock.patch.object(mod, "_compose_and_send", side_effect=fake_compose_and_send):
            params = mod.ReplyAllEmailInput(uid="123", folder="INBOX", body="Thanks")
            result = asyncio.run(mod.email_reply_all(params))

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

            params = mod.PrepareAttachmentsInput(attachments=f"{existing}, {missing}")
            result = json.loads(asyncio.run(mod.email_prepare_attachments(params)))

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
                params = mod.SaveAttachmentInput(uid="1", attachment_index=2, save_path=str(destination))
                blocked = asyncio.run(mod.email_save_attachment(params))
                self.assertIn("overwrite=true", blocked)
                self.assertEqual(destination.read_text(), "old")

                overwrite_params = mod.SaveAttachmentInput(uid="1", attachment_index=2, save_path=str(destination), overwrite=True)
                saved = json.loads(asyncio.run(mod.email_save_attachment(overwrite_params)))

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
                first = json.loads(asyncio.run(mod.email_list_accounts(mod.ListAccountsInput())))

                config_path.write_text(
                    json.dumps(
                        {
                            "accounts": [
                                {"name": "beta", "address": "beta@example.com", "password": "two"},
                            ]
                        }
                    )
                )
                second = json.loads(asyncio.run(mod.email_list_accounts(mod.ListAccountsInput())))

        self.assertEqual(first["accounts"][0]["name"], "alpha")
        self.assertEqual(second["accounts"][0]["name"], "beta")

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
                        mod.email_send_email(
                            mod.SendEmailInput(
                                confirmation_code="first-code",
                                account="alpha",
                                to="dest@example.com",
                                subject="test",
                                body="hello",
                            )
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
                        mod.email_send_email(
                            mod.SendEmailInput(
                                confirmation_code="first-code",
                                account="alpha",
                                to="dest@example.com",
                                subject="test",
                                body="hello",
                            )
                        )
                    )

                    fresh_code_attempt = asyncio.run(
                        mod.email_send_email(
                            mod.SendEmailInput(
                                confirmation_code="second-code",
                                account="alpha",
                                to="dest@example.com",
                                subject="test",
                                body="hello",
                            )
                        )
                    )

        self.assertIn('"status": "sent"', first_attempt)
        self.assertIn("Invalid confirmation code", stale_code_attempt)
        self.assertIn('"status": "sent"', fresh_code_attempt)


if __name__ == "__main__":
    unittest.main()
