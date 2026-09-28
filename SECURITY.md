# Security policy

## Reporting a vulnerability

Please report vulnerabilities **privately**, through GitHub's private vulnerability
reporting on this repository:

1. Open the **Security** tab of
   [Michael-Drake/rentctl](https://github.com/Michael-Drake/rentctl/security).
2. Choose **Report a vulnerability**
   ([direct link](https://github.com/Michael-Drake/rentctl/security/advisories/new)).

That is the only reporting channel — there is no security email address. Please do not
open a public issue for a suspected vulnerability.

A useful report says what you ran, on which OS and rentctl version (`pip show rentctl`),
what happened, and what you expected. Fixes ship in a new release; there are no
backports to older versions.

## What counts

`rentctl` starts and kills processes on your behalf, so the reports that matter most are
ones where it:

- runs a command you did not approve, or runs a changed command without stopping for
  re-approval;
- lets a repo's `rentctl.toml` reach outside its own directory;
- signals a process it does not own — including through PID reuse;
- reports a port or a board as verified-clean when the check did not actually run.

## What is out of scope

Running a command from a config file in a repo is arbitrary code execution by design —
the [README](README.md#security-what-you-are-trusting) says so, and says what rentctl
does and does not guarantee about it. In particular, approval pins the *command*, not the
code it runs: a change to `package.json` scripts or application code changing behaviour
without a re-approval is documented behaviour, not a vulnerability.
