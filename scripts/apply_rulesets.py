#!/usr/bin/env python3
"""Apply the protect-default-branch ruleset to every active codev-workshops repo.

Same rules as the 92 sync targets: PR required, 1 approval, approval from someone
other than the last pusher, no deletion, no non-fast-forward. Org admins bypass;
no Integration bypass actor, so no app can merge without review.

`--bypass-team <slug>` additionally lets the members of one team bypass. It exists
for the `upstream-sync` team, whose only member is the `codev-sync-bot` machine user
that merges sync PRs; any other Team actor found on a ruleset is removed.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

ORG = "codev-workshops"
TOKEN = os.environ["GITHUB_MIRROR_PAT"]
RULES = [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": True,
        "required_review_thread_resolution": False,
        "allowed_merge_methods": ["merge", "squash", "rebase"],
    }},
]


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        f"https://api.github.com/{path}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json", "User-Agent": "ruleset-apply"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "{}")


def payload(bypass):
    return {"name": "protect-default-branch", "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": RULES, "bypass_actors": bypass}


ADMIN = [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}]

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--bypass-team", metavar="SLUG",
                help="team in the org whose members may bypass (intended: upstream-sync)")
opts = ap.parse_args()

WANTED = list(ADMIN)
if opts.bypass_team:
    team = api(f"orgs/{ORG}/teams/{opts.bypass_team}")
    members = api(f"orgs/{ORG}/teams/{opts.bypass_team}/members")
    print(f"bypass team {team['slug']} (id {team['id']}): "
          + ", ".join(m["login"] for m in members), flush=True)
    WANTED.append({"actor_id": team["id"], "actor_type": "Team", "bypass_mode": "always"})


def actor_key(a):
    return (a["actor_type"], a.get("actor_id") if a["actor_type"] != "OrganizationAdmin" else None)


def reconcile(actors):
    """Current bypass list -> the one we want: drop Integration and stray Team actors,
    keep anything else (RepositoryRole, DeployKey), add the wanted team if missing."""
    keep = [a for a in actors if a["actor_type"] not in ("Integration", "Team")]
    have = {actor_key(a) for a in keep}
    for w in WANTED:
        if actor_key(w) not in have:
            keep.append(w)
    return keep


repos, page = [], 1
while True:
    chunk = api(f"orgs/{ORG}/repos?per_page=100&page={page}&type=all")
    if not chunk:
        break
    repos += chunk
    page += 1
active = [r for r in repos if not r["archived"]]
print(f"{len(repos)} repos, {len(active)} active", flush=True)

c, notes = Counter(), []
for r in sorted(active, key=lambda r: r["name"]):
    name = r["name"]
    if r.get("size", 0) == 0 and not r.get("default_branch"):
        c["empty repo (no default branch)"] += 1
        notes.append((name, "empty"))
        continue
    try:
        rs = [x for x in api(f"repos/{ORG}/{name}/rulesets") if x["name"] == "protect-default-branch"]
    except urllib.error.HTTPError as e:
        c[f"list {e.code}"] += 1
        notes.append((name, f"list {e.code}"))
        continue
    try:
        if rs:
            full = api(f"repos/{ORG}/{name}/rulesets/{rs[0]['id']}")
            keep = reconcile(full["bypass_actors"])
            need = (full["enforcement"] != "active"
                    or sorted(map(actor_key, keep)) != sorted(map(actor_key, full["bypass_actors"]))
                    or sorted(x["type"] for x in full["rules"]) != sorted(x["type"] for x in RULES))
            if not need:
                c["already correct"] += 1
                continue
            api(f"repos/{ORG}/{name}/rulesets/{rs[0]['id']}", "PUT", payload(keep))
            c["updated"] += 1
        else:
            api(f"repos/{ORG}/{name}/rulesets", "POST", payload(WANTED))
            c["created"] += 1
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        c[f"write {e.code}"] += 1
        notes.append((name, f"write {e.code}: {detail}"))

print(c)
for n in notes:
    print(" ", n[0], "|", n[1])
sys.stdout.flush()
