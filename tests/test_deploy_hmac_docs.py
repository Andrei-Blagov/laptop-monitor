from __future__ import annotations

import json
import re
import time
import unittest
from pathlib import Path

from integrations.n8n import sign_webhook, verify_webhook_signature
from stores.registry import all_adapters, enabled_slugs

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_README = ROOT / "deploy" / "README.md"
HMAC_MD = ROOT / "deploy" / "n8n" / "HMAC.md"
RECEIVER_JSON = ROOT / "deploy" / "n8n" / "pipeline-event-receiver.json"


class DeployReadmeStoreSyncTests(unittest.TestCase):
    def test_deploy_readme_matches_registry(self) -> None:
        text = DEPLOY_README.read_text(encoding="utf-8")
        enabled = set(enabled_slugs())
        self.assertEqual(
            enabled, {"regard", "andpro", "kns", "citilink"}
        )
        for adapter in all_adapters():
            # Table row form: | Name | **enabled**| or | Name | disabled |
            if adapter.enabled:
                self.assertRegex(
                    text,
                    rf"\|\s*{re.escape(adapter.display_name)}\s*\|\s*\*\*enabled\*\*",
                    msg=f"{adapter.slug} should be marked enabled in deploy/README",
                )
                self.assertIn(adapter.collection_mode, text)
                self.assertIn(adapter.reliability, text)
            else:
                self.assertRegex(
                    text,
                    rf"\|\s*{re.escape(adapter.display_name)}\s*\|\s*disabled\b",
                    msg=f"{adapter.slug} should be marked disabled in deploy/README",
                )
        self.assertIn("Playwright Chromium", text)
        self.assertIn("fail-safe", text.lower())


class HmacContractDocTests(unittest.TestCase):
    def test_no_json_stringify_fallback(self) -> None:
        hmac_text = HMAC_MD.read_text(encoding="utf-8")
        receiver = RECEIVER_JSON.read_text(encoding="utf-8")
        # Must explicitly forbid reconstructed body
        self.assertIn("FORBIDDEN", hmac_text)
        self.assertIn("JSON.stringify", hmac_text)
        self.assertIn("fail closed", hmac_text.lower())
        # Must not recommend using JSON.stringify as the rawBody source
        self.assertNotRegex(
            hmac_text,
            r"rawBody\s*=\s*[^\n]*JSON\.stringify",
        )
        self.assertNotRegex(
            hmac_text,
            r"\|\|\s*JSON\.stringify",
        )
        self.assertIn("NEVER use JSON.stringify", receiver)
        self.assertIn("exact raw body", receiver.lower())

    def test_cutover_checklist_section_present(self) -> None:
        text = HMAC_MD.read_text(encoding="utf-8")
        self.assertIn("Verify on production n8n before enabling webhook", text)
        self.assertIn("n8n version", text.lower())
        self.assertIn("deliberately bad", text.lower())


class HmacVerifierBehaviorTests(unittest.TestCase):
    def test_valid_signature_accept(self) -> None:
        body = b'{"event":"pipeline.completed","status":"success"}'
        ts, sig = sign_webhook(body, "secret-test", timestamp="1700000000")
        self.assertTrue(
            verify_webhook_signature(
                body, "secret-test", ts, sig, now=1700000000
            )
        )

    def test_bad_signature_reject(self) -> None:
        body = b'{"event":"pipeline.completed"}'
        ts, _ = sign_webhook(body, "secret-test", timestamp="1700000000")
        self.assertFalse(
            verify_webhook_signature(
                body, "secret-test", ts, "00" * 32, now=1700000000
            )
        )

    def test_timestamp_skew_reject(self) -> None:
        body = b'{"event":"pipeline.completed"}'
        ts, sig = sign_webhook(body, "secret-test", timestamp="1700000000")
        self.assertFalse(
            verify_webhook_signature(
                body,
                "secret-test",
                ts,
                sig,
                max_skew_seconds=300,
                now=1700000000 + 301,
            )
        )

    def test_parsed_body_reencode_not_equivalent(self) -> None:
        """Documented contract: re-serialized JSON is not the signed raw body."""
        raw = b'{"b":1,"a":2}'
        ts, sig = sign_webhook(raw, "secret-test", timestamp="1700000000")
        parsed = json.loads(raw.decode("utf-8"))
        reencoded = json.dumps(parsed, separators=(",", ":")).encode("utf-8")
        # Different key order after loads+dumps in Python 3.7+ preserves order,
        # so force a different serialization:
        reencoded_pretty = json.dumps(parsed, indent=2).encode("utf-8")
        self.assertNotEqual(raw, reencoded_pretty)
        self.assertFalse(
            verify_webhook_signature(
                reencoded_pretty, "secret-test", ts, sig, now=1700000000
            )
        )
        self.assertTrue(
            verify_webhook_signature(raw, "secret-test", ts, sig, now=1700000000)
        )

    def test_empty_body_with_signature_for_nonempty_fails(self) -> None:
        nonempty = b'{"event":"x"}'
        ts, sig = sign_webhook(nonempty, "secret", timestamp=str(int(time.time())))
        self.assertFalse(
            verify_webhook_signature(b"", "secret", ts, sig)
        )


if __name__ == "__main__":
    unittest.main()
