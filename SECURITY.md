# Security Policy

## Supported Versions

Appistry is a single-track project. Only the latest commit on `main` is supported.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report findings privately through GitHub's private vulnerability reporting:

1. Go to <https://github.com/tohuw/appistry/security/advisories/new>
2. Or open the repository's **Security** tab and choose **Report a vulnerability**

Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- Any suggested mitigations, if known

You can expect an acknowledgement within 2 business days and a fix or
disposition within 14 days. Please give us a reasonable window to ship a fix
before disclosing publicly.

## Scope

Appistry runs entirely on the local machine and binds only to `127.0.0.1`. Of
particular interest are:

- Anything that lets a web page, another local process, or a registered app's
  metadata escape the loopback boundary or gain code execution
- Path traversal or command injection through registry-controlled values
  (app `id`, `name`, `cwd`, `command`, `icon`, `github_url`)
- Leakage of per-launch secrets, or of OAuth credentials transiting the stable
  hook proxy
- Weaknesses in the app-bundle removal / project-cleanup path that could
  destroy user data
