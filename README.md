# Upstream Sync

How `codev-workshops` repos are kept current with their `Cognition-Partner-Workshops`
originals: [`catalog/sync-map.yaml`](catalog/sync-map.yaml) says what is paired with what,
and [`scripts/sync_from_source.py`](scripts/sync_from_source.py) does the work. A bi-weekly
Devin automation runs it against this repo.

(Repo provenance and the workshop catalog itself live in `codev-workshops/workshop-content`;
this repo only holds the sync map and tooling, so that the catalog can be an exact mirror of
its upstream.)

## Rules

The sync is strictly one-directional and default-branch only:

- **Never write back to `Cognition-Partner-Workshops`** — no pushes, branches, PRs or settings
  changes. It is the read-only source of truth.
- Only default branches are read and written. Other branches, tags, releases, issues, PRs and
  repo settings are out of scope.
- The target is never force-pushed, so nothing committed in `codev-workshops` is destroyed by a
  sync; a target that has drifted is reported, and reconciling it is a human decision.

| Target vs. source default branch | `ff-only` (default) | `pr-on-diverge` |
| --- | --- | --- |
| identical | nothing | nothing |
| target strictly behind | fast-forward push | fast-forward push |
| target has its own commits too | skipped, reported | branch + PR in the target |
| target ahead only | skipped, reported | skipped, reported |

`sync: off` on a pair excludes it entirely.

## Why the map is by history, not by name

The copies were made before the `ts-`/`uc-` naming convention existed, and four repos existed
in both orgs under the *same* name with completely unrelated history — syncing those by name
would have overwritten live work. Every pair was established by a shared root commit and is
listed explicitly.

On 2026-09-01 the targets were renamed to their source names, so `target` now equals `source`
for every pair except `workshop-content -> workshop-metadata`. That is cosmetic: the map stays
authoritative and pairs must still never be added by name. For each of the four collisions the
unrelated same-name repo was renamed to `<name>-lab` and archived (nothing deleted, history
intact) before the real copy took the source name; `collisions:` records both names.

## Running it

```bash
pip install pyyaml

scripts/sync_from_source.py status               # read-only classification of every pair
scripts/sync_from_source.py apply --dry-run      # what a run would change
scripts/sync_from_source.py apply                # fast-forward what is safe
scripts/sync_from_source.py apply --create-missing   # also mirror repos under new_repos:
scripts/sync_from_source.py discover             # find upstream renames / unmapped repos
```

Credentials: reading the source org and pushing to existing targets works with any token
that has contents access to both orgs (`gh auth login` is enough). Creating the repos under
`new_repos:` additionally needs `GITHUB_MIRROR_PAT` — a fine-grained PAT on
`codev-workshops` with **Administration: write** and **Contents: write**, plus
**Workflows: write** if the mirrored history touches `.github/workflows/`.

Set `SYNC_GITHUB_BASE=https://github.com` if your environment does not rewrite github.com
through a credential proxy.

## Push protection

GitHub push protection rejects upstream commits that contain secrets, which blocked four repos
(`timesheet-app`, `eventflow-storefront`, `uc-appsec-nodegoat`, `eventflow-devin-integration`).
The sync reports these as `FAILED` and moves on — it never bypasses the control on its own.

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
unpaired `codev-workshops` repo shares its root commit — that is how upstream renames and
newly added workshops surface. Move the resolved ones from `new_repos:` into `pairs:` after
they have been mirrored.

This repo is deliberately *not* in the map: it has no upstream counterpart, so the catalog repo
(`workshop-content`) can stay an exact mirror of its source instead of carrying local tooling
commits that would make it permanently target-ahead.
