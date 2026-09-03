# Upstream Sync

How `codev-workshops` repos are kept current with their `Cognition-Partner-Workshops`
originals: [`catalog/sync-map.yaml`](catalog/sync-map.yaml) says what is paired with what,
and [`scripts/sync_from_source.py`](scripts/sync_from_source.py) does the work. A bi-weekly
Devin automation runs it against this repo.

(Repo provenance and the workshop catalog itself live in `codev-workshops/workshop-content`;
this repo only holds the sync map and tooling, so that the catalog can be an exact mirror of
its upstream.)

## Rules

The sync is one-directional and copies *content*, never history:

- **Never write back to `Cognition-Partner-Workshops`** — no pushes, branches, PRs or settings
  changes. It is the read-only source of truth.
- **Upstream history is never copied.** Workshop targets carry only their own history: a chain
  of snapshot commits. Upstream repos are ~3.5 GB of history that no workshop needs (`calcom`
  alone was 1 GB), and a snapshot keeps the working content identical without it.
- **Branch scope: the target default branch, plus `main` and `develop` when they exist on both
  sides.** No other branch is ever read or written; tags, releases, issues, PRs and repo
  settings are out of scope.
- **Target commits are preserved.** Each sync three-way merges the new upstream content against
  the previously recorded upstream content, so lab work committed in `codev-workshops` survives.
  Only a real content conflict is escalated to a human — renaming/archiving a target is never
  part of a sync run.
- A sync only ever appends a commit to the target branch. The single exception is the one-time
  `squash` command, which is what removed the imported upstream history in the first place.

### How a snapshot sync works

Every snapshot commit records the source commit its content came from:

```
Sync content from Cognition-Partner-Workshops/<repo>@main

Upstream-Commit: 05d54eb8…
```

A run reads that marker from the target branch (`base`), fetches the current source commit
(`theirs`, `--depth=1` — one tree, no history) and the target head (`ours`), and merges the
three *trees*. The result is committed on top of the target head with a new marker. Because
nothing is compared by commit ancestry, the two repos share no commits at all.

| Target branch vs. source content | behaviour |
| --- | --- |
| marker matches the source commit, or trees identical | nothing |
| source moved on | three-way merge of trees, one new commit appended |
| the merge conflicts | reported for a human; nothing pushed |
| no `Upstream-Commit:` marker anywhere in the branch | reported; run `squash` for that repo first |

`sync: off` on a pair excludes it entirely.

## Removing imported history (`squash`)

`squash` replaces a target branch with a *single root commit* holding **the target's own current
tree** — so all merged lab content is kept — and records the current source commit as the base
for future merges. It force-pushes, it is irreversible, and it therefore requires `--yes`:

```bash
scripts/sync_from_source.py squash --only=calcom --dry-run
scripts/sync_from_source.py squash --only=calcom --yes
```

It is a migration step, not part of a scheduled run: a branch that already has a marker is
skipped unless `--again` is passed. GitHub does not reclaim the disk immediately — the
unreferenced objects stay in the repo's pack until GitHub's own gc runs, so the reported repo
size keeps showing the old figure for a while even though the history is gone from the branch.

## Why the map is explicit, not by name

The copies were made before the `ts-`/`uc-` naming convention existed, and four repos existed
in both orgs under the *same* name with completely unrelated history — syncing those by name
would have overwritten live work. Every pair was established by a shared root commit and is
listed explicitly.

On 2026-09-01 the targets were renamed to their source names, so `target` now equals `source`
for every pair except `workshop-content -> workshop-metadata`. That is cosmetic: the map stays
authoritative and pairs must still never be added by name. For each of the four collisions the
unrelated same-name repo was renamed to `<name>-lab` and archived (nothing deleted, history
intact) before the real copy took the source name; `collisions:` records both names.

Since the imported history was squashed away, the pairs can no longer be *re-derived* from a
shared root commit at all — the map is the only record of what belongs to what, so keep it
under review and never repair it by name matching.

## Running it

```bash
pip install pyyaml

scripts/sync_from_source.py status               # read-only classification of every pair
scripts/sync_from_source.py apply --dry-run      # what a run would change
scripts/sync_from_source.py apply                # snapshot the current source content
scripts/sync_from_source.py apply --create-missing   # also seed repos under new_repos:
scripts/sync_from_source.py squash --yes         # one-time: drop imported upstream history
scripts/sync_from_source.py discover             # find upstream renames / unmapped repos
```

Credentials: reading the source org and pushing to existing targets works with any token
that has contents access to both orgs (`gh auth login` is enough). Creating the repos under
`new_repos:` additionally needs `GITHUB_MIRROR_PAT` — a fine-grained PAT on
`codev-workshops` with **Administration: write** and **Contents: write**, plus
**Workflows: write** if the content includes `.github/workflows/`. `squash` needs a token that
the default-branch ruleset lets force-push (org admin or the Devin app).

Set `SYNC_GITHUB_BASE=https://github.com` if your environment does not rewrite github.com
through a credential proxy.

## Push protection

GitHub push protection rejects commits that contain secrets, which blocked four repos
(`timesheet-app`, `eventflow-storefront`, `uc-appsec-nodegoat`, `eventflow-devin-integration`).
The sync reports these as `FAILED` and moves on — it never bypasses the control on its own.
(Snapshots make this rarer: only secrets present in the *current* content can trip it, not ones
buried in upstream history.)

They were unblocked on 2026-09-01 by granting a per-finding bypass on the *target* repo and
pushing immediately (bypasses expire within minutes):

```bash
# placeholder ids come from the unblock-secret/<id> URLs in the rejected push output
curl -X POST -H "Authorization: Bearer $GITHUB_MIRROR_PAT" \
  ".../repos/codev-workshops/<repo>/secret-scanning/push-protection-bypasses" \
  -d '{"reason":"used_in_tests","placeholder_id":"<id>"}'
```

No org or repo security setting was changed and no history was rewritten, so the secrets stay
identical to the source and are still reported as alerts on the target. The credentials
involved (Slack, Stripe, an Azure AD app secret in `eventflow-storefront`'s `public/ops.js`,
AWS keys in NodeGoat's vendored `node_modules`) live in the *source* history and should be
rotated there.

## Maintenance

`discover` reports source repos that are absent from the map and, for each, whether some
some `codev-workshops` repo already carries the same name — that is how upstream renames and
newly added workshops surface. Since targets no longer share history with their sources, root
commit fingerprinting no longer works and every new pair must be confirmed by a human by
comparing content. Move the resolved ones from `new_repos:` into `pairs:` after they are seeded.

This repo is deliberately *not* in the map: it has no upstream counterpart, so the catalog repo
(`workshop-content`) can stay an exact mirror of its source instead of carrying local tooling
commits that would make it permanently target-ahead.
