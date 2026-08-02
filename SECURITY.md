# Security Policy

## Supported Versions

Roost is a single-track project. Only the latest commit on `main` is supported.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report findings privately through GitHub's private vulnerability reporting:

1. Go to <https://github.com/tohuw/roost/security/advisories/new>
2. Or open the repository's **Security** tab and choose **Report a vulnerability**

If that form is unavailable to you — private reporting has to be enabled on the
repository, and if it is off the link 404s — say so in a normal issue **without the
details**, asking for a private channel. An issue reading "I have a security finding
about the help server, how do I send it privately" discloses nothing and is a better
outcome than a report nobody could file.

Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- Any suggested mitigations, if known

You can expect an acknowledgement within 2 business days and a fix or
disposition within 14 days. Please give us a reasonable window to ship a fix
before disclosing publicly.

## Scope

Roost runs entirely on the local machine and binds only to `127.0.0.1`. Of
particular interest are:

- Anything that lets a web page or another local process escape the loopback
  boundary, or that lets Roost be used as a conduit into a raven. A fixed
  loopback port is reachable by any page the user has open, so the help server
  refuses a foreign `Host` and **any** `Origin`, and forwards nothing.
- **Credential mixing between ravens.** Each raven owns its own token. Anything
  that could send one raven's credential to another raven's port, cache a token
  across ravens, or make Roost mint a credential on a raven's behalf.
- **Descriptor handling.** A descriptor is a file written by another process and
  is treated as untrusted input. Anything that lets a descriptor field reach a
  menu, a log, a terminal, or a filesystem path unvalidated — including control
  characters, ANSI escapes, bidirectional overrides, path traversal through a
  raven name or `token_path`, or a value that redirects a request off the port
  the descriptor declared.
- **Unbounded work from a raven.** A raven is a separate process that can hang or
  misbehave. Anything that lets it block the menu-build path, return an
  unbounded response, or turn a malformed reply into a crash rather than a
  disabled section with a reason.
- Anything that reads or writes Roost's own state directory (see
  `roost/paths.py`) with
  permissions that let another local user see or alter it.
