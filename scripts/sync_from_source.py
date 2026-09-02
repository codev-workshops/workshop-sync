#!/usr/bin/env python3
"""Sync codev-workshops repos with their Cognition-Partner-Workshops sources.

The source default branch is the reference for every decision. Nothing is ever
force-pushed, so lab-specific commits in codev-workshops cannot be lost.

Subcommands
    status     classify every mapped pair (default; read-only)
    apply      bring every target's default branch up to date with its source: a
               fast-forward where the target has no commits of its own, otherwise a
               merge of the source default branch into it; optionally create and
               populate the repos listed under `new_repos:`
    discover   re-fingerprint both orgs by root commit and report pairs that are
               missing from, or contradicted by, the map (catches upstream renames)

Auth
    GITHUB_MIRROR_PAT  fine-grained PAT (Contents+Administration write on the target
                       org). Required for `apply --create-missing`; optional otherwise.
    Falls back to `gh auth token` for API calls and to ambient git credentials
    for pushes.

Examples
    scripts/sync_from_source.py status
    scripts/sync_from_source.py apply --dry-run
    scripts/sync_from_source.py apply --only=timesheet-app
    scripts/sync_from_source.py apply --create-missing
    scripts/sync_from_source.py discover
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

API = "https://api.github.com"
# `https://github.com:443/` is equivalent to `https://github.com/` but is not matched by
# an `insteadOf` rewrite, so token auth keeps working inside environments that route
# github.com through a credential-injecting proxy.
GIT_BASE = os.environ.get("SYNC_GITHUB_BASE", "https://github.com:443")
MAP_PATH = Path(__file__).resolve().parent.parent / "catalog" / "sync-map.yaml"

IN_SYNC, FAST_FORWARD, TARGET_AHEAD, DIVERGED = "in-sync", "fast-forward", "target-ahead", "diverged"


class MergeConflict(RuntimeError):
    """The source default branch does not merge cleanly into the target's."""


class Tokens:
    """Credentials per org.

    GITHUB_MIRROR_PAT is scoped to the target org only, so the source org is always
    read with the ambient token; mixing them up makes every source fetch 403.
    """

    def __init__(self, cfg):
        ambient = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or gh_cli_token())
        pat = os.environ.get("GITHUB_MIRROR_PAT")
        if not ambient and not pat:
            sys.exit("No GitHub token: set GH_TOKEN/GITHUB_MIRROR_PAT or authenticate the gh CLI.")
        self.src = ambient or pat
        self.tgt = pat or ambient
        self.can_create = bool(pat)
        self._by_org = {cfg["source_org"]: self.src, cfg["target_org"]: self.tgt}

    def for_org(self, org: str) -> str:
        return self._by_org.get(org, self.src)


def gh_cli_token() -> str:
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def api(path: str, method: str = "GET", body: dict | None = None, token: str = "") -> dict:
    req = urllib.request.Request(
        f"{API}/{path.lstrip('/')}",
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "workshop-sync",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or "{}")


def api_all(path: str, token: str) -> list[dict]:
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        chunk = api(f"{path}{sep}per_page=100&page={page}", token=token)
        if not chunk:
            break
        out.extend(chunk)
        page += 1
    return out


def redact(text: str) -> str:
    text = re.sub(r"(?i)\b(basic|bearer)\s+\S+", r"\1 ***", text)
    return re.sub(r"\b(github_pat_|ghp_|gho_|ghs_)\w+", r"\1***", text)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(redact(f"{' '.join(cmd)}\n{p.stderr.strip()}"))
    return p.stdout.strip()


def auth_header(token: str) -> list[str]:
    """Pass the token via http.extraheader so it never lands in a remote URL or log."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


def git_auth(argv: list[str], token: str, cwd: Path | None = None) -> str:
    """Run a network git command with an explicit token, falling back to ambient creds."""
    try:
        return run(["git", *auth_header(token), *argv], cwd=cwd)
    except RuntimeError as exc:
        if token and any(s in str(exc) for s in ("could not read Username", "Authentication failed", "403")):
            return run(["git", *argv], cwd=cwd)
        raise


class Repo:
    """A throwaway working copy holding both sides' commit graphs (blobless/treeless)."""

    def __init__(self, cfg, source: str, target: str, tk: "Tokens"):
        self.cfg, self.source, self.target, self.tk = cfg, source, target, tk
        self.dir = Path(tempfile.mkdtemp(prefix="wsync-"))
        run(["git", "init", "-q", "."], cwd=self.dir)
        run(["git", "remote", "add", "s", f"{GIT_BASE}/{cfg['source_org']}/{source}"], cwd=self.dir)
        run(["git", "remote", "add", "t", f"{GIT_BASE}/{cfg['target_org']}/{target}"], cwd=self.dir)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fetch(self, remote: str, branch: str, deep: bool = False):
        flt = [] if deep else ["--filter=tree:0"]
        token = self.tk.src if remote == "s" else self.tk.tgt
        git_auth(["fetch", "-q", *flt, remote, branch], token, cwd=self.dir)

    def counts(self, sb: str, tb: str) -> tuple[int, int]:
        ahead = int(run(["git", "rev-list", "--count", f"t/{tb}..s/{sb}"], cwd=self.dir))
        behind = int(run(["git", "rev-list", "--count", f"s/{sb}..t/{tb}"], cwd=self.dir))
        return ahead, behind

    def push(self, sb: str, tb: str):
        git_auth(["push", "t", f"s/{sb}:refs/heads/{tb}"], self.tk.tgt, cwd=self.dir)

    def push_branch(self, sb: str, branch: str):
        git_auth(["push", "t", f"s/{sb}:refs/heads/{branch}"], self.tk.tgt, cwd=self.dir)

    def merge(self, sb: str, tb: str) -> str:
        """Merge the source default branch into a local copy of the target's.

        Target-only commits survive: this is a merge, never a reset. Raises
        MergeConflict and leaves no half-merged state when the histories collide.
        """
        run(["git", "checkout", "-q", "-B", "wsync-merge", f"t/{tb}"], cwd=self.dir)
        ident = ["-c", "user.name=workshop-sync", "-c", "user.email=workshop-sync@codev-workshops"]
        p = subprocess.run(["git", *ident, "merge", "--no-ff", f"s/{sb}",
                            "-m", f"Merge {self.cfg['source_org']}/{self.source}@{sb} into {tb}"],
                           cwd=self.dir, text=True, capture_output=True)
        if p.returncode:
            run(["git", "merge", "--abort"], cwd=self.dir, check=False)
            raise MergeConflict(redact(p.stdout.strip() or p.stderr.strip()))
        return run(["git", "rev-parse", "--short", "HEAD"], cwd=self.dir)

    def push_head(self, tb: str):
        git_auth(["push", "t", f"HEAD:refs/heads/{tb}"], self.tk.tgt, cwd=self.dir)


def default_branch(org: str, repo: str, tk: Tokens) -> str:
    return api(f"repos/{org}/{repo}", token=tk.for_org(org))["default_branch"]


def classify(cfg, pair, tk) -> dict:
    source, target = pair["source"], pair["target"]
    r = Repo(cfg, source, target, tk)
    try:
        sb = default_branch(cfg["source_org"], source, tk)
        tb = default_branch(cfg["target_org"], target, tk)
        r.fetch("s", sb)
        r.fetch("t", tb)
        ahead, behind = r.counts(sb, tb)
        state = (IN_SYNC if ahead == behind == 0 else
                 FAST_FORWARD if behind == 0 else
                 TARGET_AHEAD if ahead == 0 else DIVERGED)
        return dict(source=source, target=target, sb=sb, tb=tb, ahead=ahead, behind=behind,
                    state=state, repo=r)
    except Exception as exc:
        r.close()
        return dict(source=source, target=target, sb="?", tb="?", ahead=0, behind=0,
                    state=f"error: {redact(str(exc))}".splitlines()[-1][:120], repo=None)


def open_sync_pr(cfg, res, tk, dry_run: bool) -> str:
    import datetime
    branch = f"sync/{datetime.date.today().isoformat()}"
    if dry_run:
        return f"would open PR from `{branch}`"
    res["repo"].push_branch(res["sb"], branch)
    pr = api(f"repos/{cfg['target_org']}/{res['target']}/pulls", "POST", {
        "title": f"Sync {res['ahead']} commit(s) from {cfg['source_org']}/{res['source']}",
        "head": branch,
        "base": res["tb"],
        "body": (f"Automated sync from `{cfg['source_org']}/{res['source']}@{res['sb']}`.\n\n"
                 f"- {res['ahead']} commit(s) only in the source\n"
                 f"- {res['behind']} commit(s) only here\n\n"
                 "The automated merge conflicts, so it needs a human resolution; the sync job "
                 "never force-pushes."),
    }, token=tk.tgt)
    return pr["html_url"]


def cmd_status(cfg, mp, tk, args) -> list[dict]:
    results = []
    pairs = [p for p in mp["pairs"] if not args.only or args.only in (p["source"], p["target"])]
    for pair in pairs:
        if pair.get("sync", cfg["sync"]) == "off":
            continue
        res = classify(cfg, pair, tk)
        results.append(res)
        print(f"  {res['state']:<12} {res['source']} -> {res['target']}"
              f"  (+{res['ahead']} source / +{res['behind']} target)", flush=True)
        if res["repo"] and not args.keep:
            res["repo"].close()
            res["repo"] = None
    return results


def cmd_apply(cfg, mp, tk, args):
    print("== existing pairs ==")
    args.keep = True
    results = cmd_status(cfg, mp, tk, args)
    changed, prs, skipped, failed = [], [], [], []
    for res in results:
        pair = next(p for p in mp["pairs"] if p["source"] == res["source"] and p["target"] == res["target"])
        policy = pair.get("sync", cfg["sync"])
        try:
            if res["state"] == FAST_FORWARD:
                if args.dry_run:
                    print(f"  DRY-RUN would fast-forward {res['target']} by {res['ahead']} commit(s)")
                else:
                    res["repo"].fetch("s", res["sb"], deep=True)
                    res["repo"].push(res["sb"], res["tb"])
                    print(f"  pushed {res['ahead']} commit(s) -> {res['target']}")
                changed.append(res)
            elif res["state"] == DIVERGED:
                res["repo"].fetch("s", res["sb"], deep=True)
                res["repo"].fetch("t", res["tb"], deep=True)
                try:
                    sha = res["repo"].merge(res["sb"], res["tb"])
                except MergeConflict as exc:
                    print(f"  CONFLICT merging into {res['target']}: "
                          f"{str(exc).splitlines()[0][:120]}")
                    if policy == "pr-on-diverge":
                        prs.append((res, open_sync_pr(cfg, res, tk, args.dry_run)))
                        print(f"  PR for human resolution: {prs[-1][1]}")
                    else:
                        skipped.append(res)
                else:
                    if args.dry_run:
                        print(f"  DRY-RUN would merge {res['ahead']} commit(s) into "
                              f"{res['target']} ({sha}), keeping its {res['behind']} own commit(s)")
                    else:
                        res["repo"].push_head(res["tb"])
                        print(f"  merged {res['ahead']} commit(s) -> {res['target']} ({sha}), "
                              f"kept its {res['behind']} own commit(s)")
                    changed.append(res)
            elif res["state"] == TARGET_AHEAD:
                skipped.append(res)
        except Exception as exc:
            # One unpushable repo (branch protection, push protection, revoked scope)
            # must never abort the rest of the run.
            failed.append(res)
            print(f"  FAILED {res['target']}: {redact(str(exc)).splitlines()[-1][:200]}")
        finally:
            if res["repo"]:
                res["repo"].close()
                res["repo"] = None

    if args.create_missing:
        print("== new repos ==")
        create_missing(cfg, mp, tk, args)

    print(f"\nupdated: {len(changed)} | PRs: {len(prs)} | "
          f"left for review: {len(skipped)} | failed: {len(failed)}")
    for res in failed:
        print(f"  failed: {res['source']} -> {res['target']}")


def create_missing(cfg, mp, tk, args):
    if not tk.can_create:
        print("  GITHUB_MIRROR_PAT is not set — repo creation needs Administration:write; skipping")
        return
    existing = {r["name"] for r in api_all(f"orgs/{cfg['target_org']}/repos?type=all", tk.tgt)}
    for entry in mp.get("new_repos") or []:
        source = entry["source"]
        target = entry.get("target", source)
        if args.only and args.only not in (source, target):
            continue
        if args.dry_run:
            print(f"  DRY-RUN would mirror {source} -> {cfg['target_org']}/{target}"
                  + (" (existing empty repo)" if target in existing else " (new repo)"))
            continue
        if target not in existing:
            src_meta = api(f"repos/{cfg['source_org']}/{source}", token=tk.for_org(cfg["source_org"]))
            api(f"orgs/{cfg['target_org']}/repos", "POST", {
                "name": target,
                "description": f"Mirror of {cfg['source_org']}/{source}",
                "private": src_meta["private"],
                "auto_init": False,
            }, token=tk.tgt)
        try:
            mirror(cfg, source, target, tk)
            print(f"  mirrored {source} -> {target}")
        except Exception as exc:
            print(f"  FAILED {source} -> {target}: "
                  f"{redact(str(exc)).splitlines()[-1][:200]}")


def mirror(cfg, source: str, target: str, tk: Tokens):
    """Copy every branch and tag into the (empty) target repo.

    Branches and tags only: `--mirror` would also try to write `refs/pull/*`,
    which GitHub rejects as hidden refs.

    A token without the Workflows permission cannot push commits that touch
    `.github/workflows/`, so that failure is reported rather than worked around
    by rewriting history.
    """
    work = Path(tempfile.mkdtemp(prefix="wmirror-"))
    try:
        git_auth(["clone", "-q", "--mirror",
                  f"{GIT_BASE}/{cfg['source_org']}/{source}", str(work / "src.git")], tk.src)
        try:
            git_auth(["push", f"{GIT_BASE}/{cfg['target_org']}/{target}",
                      "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"],
                     tk.tgt, cwd=work / "src.git")
        except RuntimeError as exc:
            if "workflow" in str(exc).lower():
                raise RuntimeError(
                    f"{source}: history contains .github/workflows/ — the token needs the "
                    "Workflows permission to mirror this repo") from exc
            raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def root_commit(org: str, repo: str, token: str) -> str | None:
    """Oldest commit on the default branch, via the Link header's last page."""
    req = urllib.request.Request(
        f"{API}/repos/{org}/{repo}/commits?per_page=1",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "workshop-sync"})
    try:
        with urllib.request.urlopen(req) as resp:
            link = resp.headers.get("Link", "")
            first = json.loads(resp.read())
    except urllib.error.HTTPError:
        return None
    last = ""
    for part in link.split(","):
        if 'rel="last"' in part:
            last = part.split("page=")[-1].split(">")[0]
    if not last:
        return first[0]["sha"] if first else None
    page = api(f"repos/{org}/{repo}/commits?per_page=1&page={last}", token=token)
    return page[0]["sha"] if page else None


def cmd_discover(cfg, mp, tk, args):
    src = {r["name"] for r in api_all(f"orgs/{cfg['source_org']}/repos?type=all", tk.src)}
    tgt = {r["name"] for r in api_all(f"orgs/{cfg['target_org']}/repos?type=all", tk.tgt)}
    mapped_src = {p["source"] for p in mp["pairs"]} | {e["source"] for e in mp.get("new_repos") or []}
    mapped_tgt = {p["target"] for p in mp["pairs"]}

    gone = mapped_src - src
    if gone:
        print("source repos in the map that no longer exist (renamed or deleted upstream):")
        for g in sorted(gone):
            print(f"  - {g}")

    unmapped = sorted(src - mapped_src)
    if unmapped:
        print("\nsource repos missing from the map — matching by root commit:")
        roots = {}
        for t in sorted(tgt - mapped_tgt):
            r = root_commit(cfg["target_org"], t, tk.tgt)
            if r:
                roots.setdefault(r, []).append(t)
        for s in unmapped:
            r = root_commit(cfg["source_org"], s, tk.src)
            match = roots.get(r or "", [])
            print(f"  - {s}: " + (f"shares history with {', '.join(match)}" if match
                                  else "no counterpart -> add under new_repos:"))
    if not gone and not unmapped:
        print("map is complete: every source repo is either paired or listed under new_repos")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="status", choices=["status", "apply", "discover"])
    ap.add_argument("--dry-run", action="store_true", help="apply: report what would change, change nothing")
    ap.add_argument("--only", help="restrict to one repo (source or target name)")
    ap.add_argument("--create-missing", action="store_true", help="apply: also create/populate new_repos")
    ap.add_argument("--map", default=str(MAP_PATH))
    args = ap.parse_args()
    args.keep = False

    mp = yaml.safe_load(open(args.map))
    cfg = mp["defaults"]
    tk = Tokens(cfg)
    {"status": cmd_status, "apply": cmd_apply, "discover": cmd_discover}[args.command](cfg, mp, tk, args)


if __name__ == "__main__":
    main()
