"""Fixture test for the Lab Tracker diff: a tiny sheet HTML + repo tree, no network."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "lab_tracker"))
import lab_tracker as LT  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lab_tracker" / "lab_tracker.py"
WS = "workshops/by-tech-role/development/app-maintenance/README.md"

HEADER = [
    "Lab Order", "Stage", "Discipline", "Exercise", "Objective", "Repo(s)", "Duration", "Best Tool",
    "Appears In (workshop — lab)", "Link",
    "Status", "Date", "Notes", "Sessions",
    "Status", "Date", "Notes", "Sessions",
    "Status", "Date", "Notes", "Sessions",
    "Status", "Date", "Notes", "Sessions",
]


def link(path, label):
    return f'<a class="in-cell-link" href="{LT.BLOB}{path}">{label}</a>'


def tr(cells):
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def sheet_html(rows):
    title = '<tr><td></td><td></td><td></td><td></td><td colspan="22">Hands-On Exercise Tracker</td></tr>'
    return "<table>" + title + tr(HEADER) + "".join(tr(r) for r in rows) + "</table>"


def data_row(order, exercise, repos, duration, workshops, path, notes="a<br>b"):
    cells = [str(order), "L1 — Foundations", "Architecture Design", exercise, "obj", repos, duration,
             "Devin Cloud", "".join(link(w, "W") for w in workshops), link(path, "Open exercise")]
    return cells + ["Not Started", "", notes, ""] * 4


MODULE = """# {title}

## Challenge

Do the [thing](x.md) well.

## Difficulty

Intermediate

## Estimated Time

45 minutes

## Repositories

### <a id="timesheet-app"></a>timesheet-app

**Repository:** [timesheet-app](https://github.com/codev-workshops/timesheet-app)

### calcom
"""

README = """# Architecture Design

| Module | Difficulty | Time |
|--------|-----------|------|
| [Alpha](alpha.md) | Intermediate | 45 min |
| [Beta](beta.md) | Advanced | 60 min |
"""

WORKSHOP = """# Workshop: App Maintenance

## Lab 1 — Alpha
- [Alpha](../../../../labs/architecture-design/alpha.md)

## Lab 2 — Beta
- [Beta](../../../../labs/architecture-design/beta.md)
"""


class LabTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        for rel, text in {
            "labs/architecture-design/README.md": README,
            "labs/architecture-design/alpha.md": MODULE.format(title="Alpha"),
            "labs/architecture-design/beta.md": MODULE.format(title="Beta"),
            "labs/devin-features/ignored.md": "# ignored",
            "workshops/README.md": "# Workshops",
            "workshops/by-tech-role/README.md": "# Workshops by Technical Role\n- labs/ index",
            WS: WORKSHOP,
        }.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                       cwd=self.repo, check=True)

    def run_diff(self, rows, *extra):
        sheet = self.tmp / "sheet.html"
        sheet.write_text(sheet_html(rows), encoding="utf-8")
        out = self.tmp / "out"
        r = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo), "--sheet", str(sheet),
                            "--out", str(out), *extra], capture_output=True, text=True, encoding="utf-8")
        self.assertIn(r.returncode, (0, 10), r.stderr)
        return r.returncode, json.loads((out / "report.json").read_text(encoding="utf-8")), out

    def test_in_sync(self):
        rows = [data_row(1, "Alpha", "timesheet-app", "45 minutes", [WS], "labs/architecture-design/alpha.md"),
                data_row(2, "Beta", "calcom", "45 minutes", [WS], "labs/architecture-design/beta.md")]
        code, rep, out = self.run_diff(rows)
        self.assertEqual(code, 0)
        self.assertEqual(rep["sheet_rows"], 2)
        self.assertEqual(rep["repo_modules"], 2)  # devin-features and READMEs excluded
        self.assertEqual(rep["missing"], [])
        self.assertEqual(rep["unreferenced_workshops"], [])  # index READMEs are not workshops
        self.assertEqual(rep["changes"], [])
        self.assertFalse((out / "new_rows.tsv").exists())

    def test_missing_module_produces_row(self):
        rows = [data_row(1, "Alpha", "timesheet-app", "45 minutes", [WS], "labs/architecture-design/alpha.md")]
        code, rep, out = self.run_diff(rows)
        self.assertEqual(code, 10)
        self.assertEqual([m["path"] for m in rep["missing"]], ["labs/architecture-design/beta.md"])
        self.assertEqual(rep["first_empty_row"], 4)
        cells = (out / "new_rows.tsv").read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual(len(cells), 26)
        self.assertEqual(cells[0], "2")
        self.assertEqual(cells[1], "L1 — Foundations")  # inherited from sibling row
        self.assertEqual(cells[2], "Architecture Design")
        self.assertEqual(cells[3], "Beta")
        self.assertEqual(cells[4], "Do the thing well.")
        self.assertEqual(cells[5], "timesheet-app, calcom")
        self.assertEqual(cells[6], "45 minutes")
        self.assertIn(f'=HYPERLINK("{LT.BLOB}{WS}";"Workshop: App Maintenance — Lab 2")', cells[8])
        self.assertEqual(cells[9], f'=HYPERLINK("{LT.BLOB}labs/architecture-design/beta.md";"Open exercise")')
        self.assertEqual([cells[i] for i in (10, 14, 18, 22)], ["Not Started"] * 4)

    def test_changes_reported_and_since_gate(self):
        rows = [data_row(1, "Alpha (old)", "timesheet-app, gone-repo", "30 minutes", [], "labs/architecture-design/alpha.md"),
                data_row(2, "Beta", "calcom", "45 minutes", [WS], "labs/architecture-design/gamma.md")]
        _, rep, _ = self.run_diff(rows)
        fields = {(c["row"], c["field"]) for c in rep["changes"]}
        self.assertEqual(fields, {(3, "Exercise"), (3, "Repo(s)"), (3, "Duration"), (3, "Appears In"), (4, "Link")})
        self.assertNotIn("labs/architecture-design/beta.md", [m["path"] for m in rep["missing"]])  # matched by title

        _, rep, _ = self.run_diff(rows, "--since", "2099-01-01")
        self.assertTrue(rep["change_detection_skipped"])
        self.assertEqual(rep["changes"], [])


if __name__ == "__main__":
    unittest.main()
