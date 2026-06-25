# Contributing standards

Conventions for issues, branches, commits, and pull requests across the Vigil
repos. The templates in `.github/` enforce most of this — this is the why.

## Repos

- **Vigil** (this repo) — Community edition, AGPLv3, public. Source of truth.
- **Vigil-Pro** / **Vigil-Enterprise** — commercial editions, private. Same
  standards; they plug into core and never fork it. Edition features never land in this repo.

## Issues

- Use the Bug report or Feature request form (blank issues are disabled).
- One issue = one problem or one request.
- Security issues go through a private advisory, never a public issue.
- Tag whether a request is Community vs. Pro/Enterprise — the latter are
  commercial editions handled privately, not in this repo.

## Branches

Short, prefixed, kebab-case off `main`:

```
fix/trivy-scan-timeout
feat/edition-hooks
docs/editions-matrix
chore/bump-2026.3.1
```

## Commits

Plain and professional:

- Subject is one short sentence with the version in parens when it's a release:
  `Fix Trivy scan timeout and seed templates (2026.3.1)`.
- Multi-feature commit: short subject, then **one sentence per feature on its
  own line**. No dashes, no bullet lists, no multi-paragraph prose.
- **Never** add `Co-Authored-By` or "Generated with" trailers. Author every
  commit as yourself.

## Versioning

Versions are `YYYY.MINOR.PATCH` (e.g. `2026.3.0`). On a release, bump **all four
in lockstep** and tag the commit that contains them:

- `server/vigil/settings.py` → `VIGIL_VERSION`, `VIGIL_AGENT_VERSION`
- `agent/vigil_agent/__version__.py` → `__version__`
- `docker-compose.yml` → `VIGIL_AGENT_VERSION` default (and the header comment)

Tag **after** the version-bump commit so `git checkout vX` reports `X`:

```
git tag v2026.3.1 <commit>
git push origin v2026.3.1
```

## Pull requests

- Fill in the PR template; tick the checklist honestly.
- Run the test suite and say so in Testing. Don't claim green without output.
- Self-review the diff before requesting review.
- Squash-merge to keep `main` linear; the squash message follows the commit
  rules above.

