#!/usr/bin/env python3
"""Diff the Lab Tracker sheet against the lab modules in workshop-content.

Input is the HTML copy of the `Lab Tracker` tab written by read_sheet.mjs (the HTML
is used rather than the TSV because HYPERLINK cells only carry their href there, and
because Notes cells contain newlines that break naive TSV parsing).

Outputs, in --out (default: the directory holding the sheet HTML):
    report.md      what to tell the user: missing modules, unreferenced workshops,
                   and changes to existing rows (report-only — rows are never edited)
    report.json    the same, machine-readable
    new_rows.tsv   one ready-to-paste row per missing module, in the sheet's own
                   column order (only written when something is missing)

Exit code 0 = in sync, 10 = rows to append, 1 = error.

Examples
    scripts/lab_tracker/lab_tracker.py --repo ~/repos/workshop-content \
        --sheet ~/labsync/sheet_Lab_Tracker.html --since '8 days ago'
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

BLOB = "https://github.com/codev-workshops/workshop-content/blob/main/"
STAGES = {
    1: "L1 — Foundations: understand & explore",
    2: "L2 — Guided build: implement with guardrails",
    3: "L3 — Autonomous delivery: end-to-end tasks",
    4: "L4 — Scale & automate: fleets, events, capstones",
}
STATUS_COL = "Status"
MEMBER_COUNT = 4


# ----------------------------------------------------------------------------- sheet
@dataclass
class Cell:
    text: str = ""
    hrefs: list[str] = field(default_factory=list)


class SheetHTML(HTMLParser):
    """Turns the Google-Sheets clipboard HTML into rows of Cells, expanding colspan."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[Cell]] = []
        self._row: list[Cell] | None = None
        self._cell: Cell | None = None
        self._span = 1

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td" and self._row is not None:
            self._cell = Cell()
            self._span = int(a.get("colspan") or 1)
        elif tag == "a" and self._cell is not None and a.get("href"):
            self._cell.hrefs.append(a["href"])
        elif tag == "br" and self._cell is not None:
            self._cell.text += "\n"

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None and self._row is not None:
            self._cell.text = self._cell.text.strip()
            self._row.append(self._cell)
            self._row.extend(Cell() for _ in range(self._span - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.text += data


def load_sheet(path: Path):
    p = SheetHTML()
    p.feed(path.read_text(encoding="utf-8", errors="replace"))
    for i, row in enumerate(p.rows):
        if row and row[0].text == "Lab Order":
            header = [c.text for c in row]
            break
    else:
        sys.exit("header row (first cell 'Lab Order') not found in sheet HTML")
    col = {name: idx for idx, name in enumerate(header) if name}
    col["Appears In"] = next(i for n, i in col.items() if n.startswith("Appears In"))
    data = []
    for j, row in enumerate(p.rows[i + 1 :], start=i + 2):  # sheet row numbers are 1-based
        row = row + [Cell()] * (len(header) - len(row))
        if row[col["Exercise"]].text:
            data.append((j, row))
    return header, col, data


def repo_path(href: str) -> str | None:
    if href.startswith(BLOB):
        return href[len(BLOB) :].split("#")[0].split("?")[0]
    return None


# ------------------------------------------------------------------------------ repo
@dataclass
class Module:
    path: str  # labs/<discipline>/<module>.md
    discipline: str
    title: str
    objective: str
    difficulty: str
    duration: str
    repos: list[str]
    workshops: list[tuple[str, str, str]]  # (workshop README path, workshop title, lab id)


def heading(line: str) -> tuple[int, str] | None:
    """('## Foo' -> (2, 'Foo')); None for non-heading lines."""
    stripped = line.lstrip("#")
    level = len(line) - len(stripped)
    if 0 < level <= 6 and stripped.startswith(" "):
        return level, stripped.strip()
    return None


def h1(text: str) -> str:
    for line in text.splitlines():
        if (h := heading(line)) and h[0] == 1:
            return h[1]
    return ""


def section_body(text: str, name: str, levels=(2,)) -> str:
    """Lines under the first heading whose title starts with `name`, up to the next heading of the same or higher level."""
    body: list[str] = []
    level = 0
    for line in text.splitlines():
        h = heading(line)
        if level:
            if h and h[0] <= level:
                break
            body.append(line)
        elif h and h[0] in levels and h[1].startswith(name):
            level = h[0]
    return "\n".join(body)


def section(text: str, names=("Challenge", "Objective", "Goal")) -> str:
    for n in names:
        body = section_body(text, n, levels=(2, 3)).strip()
        if body:
            para = body.split("\n\n")[0]
            return " ".join(re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", para).split())
    return ""


def readme_tables(readme: str):
    """Modules table -> {file: (difficulty, time)}; Repositories table -> {file: [repos]}."""
    meta: dict[str, tuple[str, str]] = {}
    repos: dict[str, list[str]] = {}
    for line in readme.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and (m := re.match(r"\[([^\]]+)\]\(([^)]+\.md)\)$", cells[0])):
            meta[m.group(2)] = (cells[1], cells[2])
        elif len(cells) == 2 and "](" in cells[1] and not cells[0].startswith("-"):
            for f in re.findall(r"\]\(([^)]+\.md)\)", cells[1]):
                repos.setdefault(f, []).append(cells[0])
    return meta, repos


def load_repo(root: Path) -> tuple[dict[str, Module], dict[str, str]]:
    workshops: dict[str, str] = {}  # README path -> title
    links: dict[str, list[tuple[str, str, str]]] = {}
    readmes = sorted((root / "workshops").rglob("README.md"))
    for rd in readmes:
        rel = rd.relative_to(root).as_posix()
        if rel == "workshops/README.md":
            continue
        text = rd.read_text(encoding="utf-8", errors="replace")
        title = h1(text) or rel
        if not title.startswith("Workshop:"):
            is_group_index = any(q != rd and q.is_relative_to(rd.parent) for q in readmes)
            if is_group_index or "labs/" not in text:
                continue
        workshops[rel] = title
        current_lab = ""
        for line in text.splitlines():
            if (h := heading(line)) and (m := re.search(r"\bLab ([A-Z]?\d+[A-Z]?)\b", h[1])):
                current_lab = m.group(1)
            for target in re.findall(r"\]\(((?:\.\./)+labs/[^)#]+\.md)", line):
                mod = (rd.parent / target).resolve().relative_to(root.resolve()).as_posix()
                entry = (rel, workshops[rel], current_lab)
                if entry not in links.setdefault(mod, []):
                    links[mod].append(entry)

    modules: dict[str, Module] = {}
    for disc in sorted(p for p in (root / "labs").iterdir() if p.is_dir()):
        if disc.name == "devin-features":
            continue
        readme = disc / "README.md"
        meta, repos = readme_tables(readme.read_text(encoding="utf-8", errors="replace")) if readme.exists() else ({}, {})
        for f in sorted(disc.glob("*.md")):
            if f.name == "README.md":
                continue
            rel = f.relative_to(root).as_posix()
            text = f.read_text(encoding="utf-8", errors="replace")
            difficulty, duration = meta.get(f.name, ("", ""))
            difficulty = section(text, ("Difficulty",)) or difficulty
            duration = section(text, ("Estimated Time",)) or duration
            mod_repos = [
                re.sub(r"<a [^>]*></a>", "", h[1]).strip()
                for line in section_body(text, "Repositories").splitlines()
                if (h := heading(line)) and h[0] == 3
            ]
            modules[rel] = Module(
                path=rel,
                discipline=disc.name,
                title=h1(text) or f.stem,
                objective=section(text),
                difficulty=difficulty,
                duration=duration,
                repos=mod_repos or repos.get(f.name, []),
                workshops=links.get(rel, []),
            )
    return modules, workshops


SINCE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d+ (hour|day|week|month)s? ago)$")


def git_changed_since(root: Path, since: str) -> list[str]:
    if not SINCE_RE.match(since):
        sys.exit(f"--since must look like '8 days ago' or 2026-01-31, got {since!r}")
    out = subprocess.run(
        ["git", "log", f"--since={since}", "--name-only", "--format=", "--", "labs", "workshops"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    return sorted({l for l in out.splitlines() if l.strip()})


def git_suggest_rename(root: Path, old: str) -> str | None:
    out = subprocess.run(
        ["git", "log", "--follow", "--diff-filter=R", "--name-status", "--format=", "--", old],
        cwd=root, capture_output=True, text=True,
    ).stdout
    m = re.search(r"^R\d*\t\S+\t(\S+)", out, re.M)
    return m.group(1) if m else None


# ------------------------------------------------------------------------------ diff
def norm_duration(s: str) -> str:
    m = re.search(r"(\d+)", s or "")
    return m.group(1) if m else (s or "").strip().lower()


def stage_for(difficulty: str) -> str:
    d = difficulty.lower()
    if "advanced" in d:
        return STAGES[3]
    if "intermediate" in d:
        return STAGES[2]
    return STAGES[1]


def hyperlink(url: str, label: str) -> str:
    return f'=HYPERLINK("{url}";"{label}")'


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, type=Path, help="checkout of codev-workshops/workshop-content")
    ap.add_argument("--sheet", required=True, type=Path, help="sheet_Lab_Tracker.html from read_sheet.mjs")
    ap.add_argument("--out", type=Path, help="output directory (default: next to --sheet)")
    ap.add_argument("--since", help="git --since gate for change detection, e.g. '8 days ago'; omit to always run it")
    args = ap.parse_args()
    args.repo = args.repo.resolve()
    args.sheet = args.sheet.resolve()
    if not (args.repo / "labs").is_dir() or not (args.repo / "workshops").is_dir():
        sys.exit(f"--repo {args.repo} is not a workshop-content checkout (no labs/ and workshops/)")
    if args.sheet.suffix != ".html" or not args.sheet.is_file():
        sys.exit(f"--sheet {args.sheet} must be the .html file written by read_sheet.mjs")
    out = (args.out or args.sheet.parent).resolve()
    out.mkdir(parents=True, exist_ok=True)

    header, col, data = load_sheet(args.sheet)
    modules, workshops = load_repo(args.repo)

    sheet_paths: dict[str, int] = {}
    sheet_titles: dict[str, int] = {}
    appears_hrefs: set[str] = set()
    for rownum, row in data:
        for h in row[col["Link"]].hrefs:
            if p := repo_path(h):
                sheet_paths[p] = rownum
        sheet_titles[row[col["Exercise"]].text.casefold()] = rownum
        for h in row[col["Appears In"]].hrefs:
            appears_hrefs.add(h.split("#")[0])

    missing = [m for p, m in modules.items() if p not in sheet_paths and m.title.casefold() not in sheet_titles]
    unreferenced_workshops = [(p, t) for p, t in workshops.items() if BLOB + p not in appears_hrefs]

    changes: list[dict] = []
    changed_files = git_changed_since(args.repo, args.since) if args.since else None
    skipped_changes = changed_files is not None and not changed_files
    if not skipped_changes:
        stages_seen = {row[col["Stage"]].text for _, row in data}
        for rownum, row in data:
            paths = [p for h in row[col["Link"]].hrefs if (p := repo_path(h)) and p.startswith("labs/")]
            if not paths:
                continue
            p = paths[0]
            mod = modules.get(p)
            if mod is None:
                hint = git_suggest_rename(args.repo, p) if (args.repo / ".git").exists() else None
                changes.append({"row": rownum, "field": "Link", "sheet": p, "repo": hint or "(file no longer exists)"})
                continue

            def flag(fld, sv, rv):
                changes.append({"row": rownum, "field": fld, "sheet": sv, "repo": rv})

            if row[col["Exercise"]].text.casefold() != mod.title.casefold():
                flag("Exercise", row[col["Exercise"]].text, mod.title)
            if mod.duration and norm_duration(row[col["Duration"]].text) != norm_duration(mod.duration):
                flag("Duration", row[col["Duration"]].text, mod.duration)
            sheet_repos = {r.strip() for r in re.split(r"[,\n]", row[col["Repo(s)"]].text) if r.strip()}
            # the sheet lists a curated subset of repos, so only removed repos count as a change
            if mod.repos and (gone := sheet_repos - set(mod.repos)):
                flag("Repo(s)", ", ".join(sorted(gone)) + " (no longer offered)", ", ".join(mod.repos))
            sheet_ws = {repo_path(h) for h in row[col["Appears In"]].hrefs if repo_path(h)}
            repo_ws = {w[0] for w in mod.workshops}
            if repo_ws != sheet_ws:
                flag("Appears In", "\n".join(sorted(sheet_ws)) or "—", "\n".join(sorted(repo_ws)) or "—")

    # ---------------------------------------------------------------- new rows
    if missing:
        disc_names: dict[str, str] = {}
        disc_stage: dict[str, str] = {}  # stage of the most recent row of the same discipline
        for _, row in data:
            for h in row[col["Link"]].hrefs:
                if (p := repo_path(h)) and p.startswith("labs/"):
                    disc_names.setdefault(p.split("/")[1], row[col["Discipline"]].text)
                    disc_stage[p.split("/")[1]] = row[col["Stage"]].text
        next_order = max((int(row[col["Lab Order"]].text) for _, row in data if row[col["Lab Order"]].text.isdigit()), default=0) + 1
        lines = []
        for m in missing:
            cells = [""] * len(header)
            cells[col["Lab Order"]] = str(next_order)
            next_order += 1
            cells[col["Stage"]] = disc_stage.get(m.discipline, stage_for(m.difficulty))
            cells[col["Discipline"]] = disc_names.get(m.discipline, m.discipline.replace("-", " ").title())
            cells[col["Exercise"]] = m.title
            cells[col["Objective"]] = m.objective
            cells[col["Repo(s)"]] = ", ".join(m.repos)
            cells[col["Duration"]] = re.sub(r"\bmin\b", "minutes", m.duration)
            cells[col["Best Tool"]] = "Devin Cloud"  # review per module; Desktop for explore/review, CLI when the module says so
            cells[col["Appears In"]] = "\n".join(hyperlink(BLOB + w, f"{t} — Lab {lab}" if lab else t) for w, t, lab in m.workshops) or "—"
            cells[col["Link"]] = hyperlink(BLOB + m.path, "Open exercise")
            for idx, name in enumerate(header):
                if name == STATUS_COL:
                    cells[idx] = "Not Started"
            lines.append("\t".join(c.replace("\t", " ") for c in cells))
        (out / "new_rows.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------ report
    first_empty_row = max(r for r, _ in data) + 1
    report = {
        "sheet_rows": len(data),
        "repo_modules": len(modules),
        "first_empty_row": first_empty_row,
        "missing": [{"path": m.path, "title": m.title, "discipline": m.discipline} for m in missing],
        "unreferenced_workshops": [{"path": p, "title": t} for p, t in unreferenced_workshops],
        "changes": changes,
        "change_detection_skipped": skipped_changes,
        "changed_files_since": changed_files,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    md = [f"Lab Tracker: {len(data)} rows; repo: {len(modules)} lab modules."]
    if missing:
        md.append(f"\n## Missing from the tracker ({len(missing)}) — rows in `new_rows.tsv`, paste at row {first_empty_row}")
        md += [f"- {m.title} (`{m.path}`)" for m in missing]
        md.append("Review Stage (inherited from the discipline's last row) and Best Tool before pasting.")
    else:
        md.append(f"\nTracker already in sync ({len(modules)} modules).")
    if unreferenced_workshops:
        md.append(f"\n## Workshops not referenced in `Appears In` ({len(unreferenced_workshops)}) — check the Workshops tab")
        md += [f"- {t} (`{p}`)" for p, t in unreferenced_workshops]
    if skipped_changes:
        md.append(f"\nNo commits touched `labs/` or `workshops/` since {args.since}; change detection skipped.")
    elif changes:
        md.append(f"\n## Existing entries that changed in the repo (not modified, please review) ({len(changes)})")
        md += [f"- row {c['row']} · {c['field']}: sheet `{c['sheet']}` → repo `{c['repo']}`" for c in changes]
    else:
        md.append("\nNo differences between existing rows and the repo.")
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 10 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
