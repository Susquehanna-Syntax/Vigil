# Security Policy

Vigil executes signed remote tasks on monitored hosts, so we take reports
seriously and ask that they be disclosed privately.

## Reporting a vulnerability

**Do not open a public issue, PR, or discussion for a security problem.**

Report it privately through GitHub:

1. Go to the repository's **Security** tab → **Report a vulnerability**
   (<https://github.com/Susquehanna-Syntax/Vigil-Community-Edition/security/advisories/new>).
2. Include the details below.

This opens a private advisory visible only to you and the maintainers.

Please include:

- Affected component (server, agent, a specific endpoint) and version
  (`GET /api/v1/about/`).
- Impact — what an attacker can do.
- Reproduction steps or a proof of concept.
- Any suggested remediation.

## What to expect

- Acknowledgement within a few days.
- An assessment and, for confirmed issues, a fix on the current release line.
- Credit in the release notes if you'd like it; coordinated disclosure once a
  fix is available.

## Supported versions

Security fixes land on the **latest minor release line** only. Run a current
release before reporting.

| Version | Supported |
|---|---|
| Latest `2026.3.x` | ✅ |
| Older | ❌ — upgrade first |

## Scope

In scope: the Vigil server, the agent, and the task-signing / enrollment /
2FA security model (Ed25519 task signing, agent-side allowlists, TTL + nonce
replay protection, the enrollment ceremony).

Out of scope: vulnerabilities in third-party scanners you bring yourself
(Nessus, Greenbone, Trivy), and anything requiring an already-compromised
server signing key or host root. Report those to the respective upstreams.

The commercial **Pro** and **Enterprise** editions are private; report issues
in those through the same private-advisory process on their own repos.
