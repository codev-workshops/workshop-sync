"""Tests for upstream sync escalation routing."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "escalate.py"


class EscalationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.report_path = self.root / "report.json"
        self.log_path = self.root / "apply.log"
        self.log_path.write_text("updated: example\nFAILED example\nleft open: item\n")
        self.env = os.environ.copy()
        self.env.update(
            {
                "REPORT": str(self.report_path),
                "APPLY_LOG": str(self.log_path),
                "REPO": "codev-workshops/workshop-sync",
                "RUN_ID": "123",
                "RUN_URL": "https://github.com/codev-workshops/workshop-sync/actions/runs/123",
                "DRY_RUN": "1",
            }
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def write_report(self, report):
        self.report_path.write_text(json.dumps(report))

    def issue_blocks(self, output):
        return re.findall(
            r"DRY_RUN issue title: (.*?)\nDRY_RUN issue body:\n"
            r"(.*?)(?=\nDRY_RUN issue title:|\nDRY_RUN Slack payload:|\Z)",
            output,
            flags=re.DOTALL,
        )

    def test_missing_report_creates_failure_issue_and_slack_only(self):
        self.env["SLACK_WEBHOOK_URL"] = "https://hooks.example.invalid/token"
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        blocks = self.issue_blocks(result.stdout)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0][0].startswith("Upstream sync failed ("))
        self.assertNotIn("Upstream sync needs attention", result.stdout)
        self.assertIn("the sync run crashed before report.json was written", blocks[0][1])
        self.assertIn("DRY_RUN Slack payload:", result.stdout)

    def test_seed_failure_and_unmapped_are_split_and_filtered(self):
        self.write_report(
            {
                "apply": {
                    "updated": [],
                    "pushed": [],
                    "merged": [],
                    "left_open": [],
                    "conflicts": [],
                    "no_snapshot": [],
                    "failed": [],
                    "seed_failed": [
                        "dbx-migration-plugin -> dbx-migration-plugin: error: failed to push "
                        "some refs to 'https://github.com:443/codev-workshops/dbx-migration-plugin'"
                    ],
                },
                "needs_attention": ["seed_failed", "unmapped"],
                "discover": {
                    "gone": [],
                    "unmapped": ["DWHMigrationDemo", "aws2-modules"],
                },
            }
        )
        self.env["SLACK_WEBHOOK_URL"] = "https://hooks.example.invalid/token"
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        blocks = self.issue_blocks(result.stdout)
        self.assertEqual(len(blocks), 2)
        failure_title, failure_body = blocks[0]
        attention_title, attention_body = blocks[1]
        self.assertTrue(failure_title.startswith("Upstream sync failed ("))
        self.assertTrue(attention_title.startswith("Upstream sync needs attention ("))
        self.assertIn("were sent to Slack and are tracked in", attention_body)
        filtered_json = re.search(r"```json\n(.*?)\n```", attention_body, flags=re.DOTALL)
        self.assertIsNotNone(filtered_json)
        report = json.loads(filtered_json.group(1))
        self.assertEqual(report["needs_attention"], ["unmapped"])
        self.assertNotIn("failed", report["apply"])
        self.assertNotIn("seed_failed", report["apply"])
        log_tail = attention_body.split("<details><summary>apply.log</summary>", 1)[1]
        self.assertNotIn("FAILED", log_tail)
        self.assertIn("DRY_RUN Slack payload:", result.stdout)

    def test_unmapped_only_creates_attention_issue_without_slack(self):
        self.write_report({"needs_attention": ["unmapped"], "apply": {}, "discover": {}})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        blocks = self.issue_blocks(result.stdout)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0][0].startswith("Upstream sync needs attention ("))
        self.assertNotIn("Slack payload", result.stdout)

    def test_empty_attention_does_nothing(self):
        self.write_report({"needs_attention": [], "apply": {}, "discover": {}})
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "nothing needs attention")

    def test_failed_only_creates_failure_issue_and_slack(self):
        self.write_report(
            {
                "needs_attention": ["failed"],
                "apply": {"failed": ["one failed operation"], "seed_failed": []},
            }
        )
        self.env["SLACK_WEBHOOK_URL"] = "https://hooks.example.invalid/token"
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        blocks = self.issue_blocks(result.stdout)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0][0].startswith("Upstream sync failed ("))
        self.assertIn("- one failed operation", blocks[0][1])
        self.assertIn("DRY_RUN Slack payload:", result.stdout)

    def test_missing_webhook_warns_and_failure_exit_is_preserved(self):
        self.write_report(
            {"needs_attention": ["failed"], "apply": {"failed": ["operation failed"]}}
        )
        result = self.run_script()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(
            "::warning::SLACK_SYNC_WEBHOOK_URL is not set; Slack alert skipped", result.stdout
        )

    def test_posts_json_payload_to_local_webhook_and_retries_issue_without_label(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received.append((self.headers.get("Content-Type"), self.rfile.read(length)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        gh_bin = self.root / "bin"
        gh_bin.mkdir()
        gh_stub = gh_bin / "gh"
        gh_stub.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "with open(os.environ['GH_CALLS'], 'a') as calls:\n"
            "    calls.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "if '--label' in sys.argv:\n"
            "    sys.exit(1)\n"
            "print('https://github.com/codev-workshops/workshop-sync/issues/21')\n"
        )
        gh_stub.chmod(0o755)
        calls_path = self.root / "gh-calls.txt"
        self.write_report(
            {"needs_attention": ["failed"], "apply": {"failed": ["operation failed"]}}
        )
        self.env.update(
            {
                "DRY_RUN": "0",
                "SLACK_WEBHOOK_URL": f"http://127.0.0.1:{server.server_port}/incoming",
                "GH_CALLS": str(calls_path),
                "PATH": f"{gh_bin}{os.pathsep}{self.env['PATH']}",
            }
        )
        try:
            result = self.run_script()
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(received), 1)
        content_type, body = received[0]
        self.assertEqual(content_type, "application/json")
        payload = json.loads(body)
        self.assertIn("text", payload)
        self.assertIn("issue #21", payload["text"])
        gh_calls = calls_path.read_text().splitlines()
        self.assertEqual(len(gh_calls), 2)
        self.assertIn("--label", gh_calls[0])
        self.assertNotIn("--label", gh_calls[1])


if __name__ == "__main__":
    unittest.main()
