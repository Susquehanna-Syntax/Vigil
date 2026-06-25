## Summary

<!-- One or two sentences: what this changes and why. -->

## Type

- [ ] Bug fix
- [ ] Feature
- [ ] Refactor / cleanup
- [ ] Docs

## Proposed commit message

<!--
Plain style: short subject with the version in parens, one sentence per
feature on its own line, no bullet lists, no AI / Co-Authored-By trailer.
e.g. "Fix Trivy scan timeout and add edition seams (2026.3.1)"
-->

```
```

## Checklist

- [ ] Tests pass: `cd server && USE_SQLITE=true VIGIL_SIGNING_KEY_SEED=<seed> .venv/bin/python manage.py test`
- [ ] Migrations added if any model changed (`makemigrations`)
- [ ] If this is a release, version bumped in lockstep: `settings.py` (`VIGIL_VERSION`, `VIGIL_AGENT_VERSION`), `agent/vigil_agent/__version__.py`, `docker-compose.yml`
- [ ] No edition (Pro / Enterprise) code added to this Community repo
- [ ] Extension contract intact (`docs/pro-extension-points.md`): `KNOWN_EVENTS` stable, no edition imports in core
- [ ] No AI / Co-Authored-By attribution in commits
- [ ] No secrets, keys, or signing seeds committed

## Testing

<!-- What you ran and what you observed. -->

## Screenshots

<!-- UI changes only. -->
