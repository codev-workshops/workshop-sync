#!/usr/bin/env python3
"""Route upstream sync failures to a record issue and judgment items to Devin."""

import copy
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

FAILURE_KEYS = {"failed", "seed_failed", "run-crashed"}


def read_report(path):
    try:
        with path.open(encoding="utf-8") as report_file:
            report = json.load(report_file)
    except (FileNotFoundError, json.JSONDecodeError):
        return None, ["run-crashed"]
    return report, report["needs_attention"]


def log_tail(path, *, omit_failed=False):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    if omit_failed:
        lines = [line for line in lines if "FAILED" not in line]
    return "\n".join(lines[-200:])


def issue_body_intro(run_url):
    return f"Run: {run_url} (logs and `report.json` are attached as an artifact)."


def failure_body(failure_lines, run_url, apply_log):
    bullets = "\n".join(f"- {line}" for line in failure_lines)
    return (
        "The following upstream sync failures need a human and were not sent to Devin.\n\n"
        f"```\n{bullets}\n```\n\n"
        f"Run: {run_url}\n\n"
        "<details><summary>apply.log</summary>\n\n"
        f"```\n{log_tail(apply_log)}\n```\n"
        "</details>\n"
    )


def attention_body(report, judgment, failures, failure_issue_url, run_url, apply_log):
    keys_json = json.dumps(judgment)
    lines = [f"The scheduled sync run needs a look: `{keys_json}`."]
    if failures:
        failure_keys = ", ".join(failures)
        lines.extend(
            [
                "",
                f"Failures in this run ({failure_keys}) are tracked in "
                f"{failure_issue_url}; they are not part of this issue — do not act on them.",
            ]
        )
    lines.extend(
        [
            "",
            issue_body_intro(run_url),
            "",
            "```json",
            json.dumps(report, indent=2),
            "```",
            "",
            "<details><summary>apply.log</summary>",
            "",
            "```",
            log_tail(apply_log, omit_failed=True),
            "```",
            "</details>",
            "",
        ]
    )
    return "\n".join(lines)


def create_issue(repo, title, body, dry_run):
    if dry_run:
        print(f"DRY_RUN issue title: {title}")
        print("DRY_RUN issue body:")
        print(body, end="" if body.endswith("\n") else "\n")
        return f"https://github.com/{repo}/issues/DRY_RUN"

    body_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="sync-issue-", suffix=".md", delete=False
        ) as body_file:
            body_file.write(body)
            body_path = Path(body_file.name)

        command = [
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--label",
            "sync-attention",
            "--body-file",
            str(body_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            command.remove("--label")
            command.remove("sync-attention")
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("gh issue create failed after retry")
        return result.stdout.strip()
    finally:
        if body_path:
            body_path.unlink(missing_ok=True)


def main():
    report_path = Path(os.environ.get("REPORT", "report.json"))
    apply_log_path = Path(os.environ.get("APPLY_LOG", "apply.log"))
    repo = os.environ.get("REPO", "")
    run_id = os.environ.get("RUN_ID", "")
    run_url = os.environ.get("RUN_URL", "")
    dry_run = os.environ.get("DRY_RUN") == "1"

    report, attention = read_report(report_path)
    failures = [key for key in attention if key in FAILURE_KEYS]
    judgment = [key for key in attention if key not in FAILURE_KEYS]
    if not failures and not judgment:
        print("nothing needs attention")
        return 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    failure_issue_url = ""
    if failures:
        if "run-crashed" in failures:
            failure_lines = ["the sync run crashed before report.json was written"]
        else:
            apply_report = report.get("apply", {})
            failure_lines = list(apply_report.get("failed", [])) + list(
                apply_report.get("seed_failed", [])
            )
        failure_title = f"Upstream sync failed ({today}, run {run_id})"
        failure_issue_url = create_issue(
            repo, failure_title, failure_body(failure_lines, run_url, apply_log_path), dry_run
        )

    if judgment:
        filtered_report = copy.deepcopy(report)
        filtered_report["needs_attention"] = judgment
        apply_report = filtered_report.get("apply", {})
        apply_report.pop("failed", None)
        apply_report.pop("seed_failed", None)
        attention_title = f"Upstream sync needs attention ({today}, run {run_id})"
        create_issue(
            repo,
            attention_title,
            attention_body(
                filtered_report,
                judgment,
                failures,
                failure_issue_url,
                run_url,
                apply_log_path,
            ),
            dry_run,
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
