import contextlib
import io
import json
import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

from journey_auth import AuthError, browser_token, clean_token, prompt_token


class AuthTests(unittest.TestCase):
    def test_hidden_prompt_rejects_noninteractive_input(self):
        with patch("journey_auth.sys.stdin.isatty", return_value=False):
            with self.assertRaises(AuthError):
                prompt_token()

    def test_token_input_rejects_control_characters_without_echoing(self):
        marker = "synthetic-sensitive-marker"
        with self.assertRaises(AuthError) as caught:
            clean_token(marker + "\nmalformed")
        self.assertNotIn(marker, str(caught.exception))
        self.assertEqual("synthetic", clean_token("Bearer synthetic"))

    def test_browser_origin_cannot_point_to_another_service(self):
        for origin in ("https://evil.example", "https://journey.cloud.evil.example", "http://journey.cloud", "https://journey.cloud/app"):
            with self.subTest(origin=origin), self.assertRaises(AuthError):
                browser_token(origin)

    def test_loopback_handoff_checks_origin_and_does_not_log_credential(self):
        ready = threading.Event()
        captured = []
        result = []
        errors = []

        def output(message, **kwargs):
            captured.append(message)
            ready.set()

        def receive():
            try:
                result.append(browser_token(timeout=10))
            except Exception as exc:
                errors.append(type(exc).__name__)
                ready.set()

        with patch("builtins.print", side_effect=output):
            worker = threading.Thread(target=receive, daemon=True)
            worker.start()
            self.assertTrue(ready.wait(3))
            self.assertEqual([], errors)
            url = json.loads(re.search(r"const url = (\"[^\"]+\");", captured[0])[1])
            parsed = urllib.parse.urlsplit(url)
            local_origin = f"{parsed.scheme}://{parsed.netloc}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=2) as page:
                html = page.read().decode()
                self.assertIn("e.origin !== expected", html)
                self.assertIn("e.source !== window.opener", html)
                self.assertEqual("no-store", page.headers["Cache-Control"])
            invalid = urllib.request.Request(url, data=b"synthetic-credential", headers={"Origin": "https://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(invalid, timeout=2)
            self.assertEqual(403, caught.exception.code)
            caught.exception.close()
            valid = urllib.request.Request(url, data=b"synthetic-credential", headers={"Origin": local_origin})
            with opener.open(valid, timeout=2) as response:
                self.assertEqual(b"OK", response.read())
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(["synthetic-credential"], result)
        self.assertNotIn("synthetic-credential", "".join(captured))


if __name__ == "__main__":
    unittest.main()
