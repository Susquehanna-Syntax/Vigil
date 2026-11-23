# vigil, the command line

Hosts, alerts, tasks and deploys from a terminal. Standard library only; Python 3.9 or newer.

```
pipx install "git+https://github.com/Susquehanna-Syntax/Vigil#subdirectory=cli"
vigil login --url https://vigil.example.com
vigil hosts --status offline
vigil alerts
vigil alerts ack 3f2b --for 2h
vigil deploy "Restart nginx" --host web-01 --wait
```

`vigil login` asks for your password and the six-digit code from your authenticator, the same sign-in the browser
does. The session is kept in `~/.config/vigil/session.json` (mode 0600) and ends on the instance's own idle and
maximum sign-in limits. Your password is never stored.

For scripts on a Business install, set `VIGIL_URL` and a scoped API key in `VIGIL_API_KEY`; nothing is stored.

The full reference is in the wiki under API Reference, Command line. `vigil <command> --help` lists every option.

## Tests

- `python -m unittest discover -s tests -t .` from this folder: parsing, output, the session file, what goes on the
  wire.
- `manage.py test vigil.test_cli` from `server/`: the real CLI against a live server, and every endpoint and field it
  reads checked against the OpenAPI schema.
