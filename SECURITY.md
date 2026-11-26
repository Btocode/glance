# Security policy

Glance sees what you type in every app on your desktop and can type into those apps. A bug that
leaks text, sends it somewhere it should not go, or writes into the wrong place is a security
bug, and we treat it as one.

## Reporting a vulnerability

**Please do not open a public issue.** Report it privately through GitHub:

1. Go to <https://github.com/Btocode/glance/security/advisories/new>
   (the repository's **Security** tab, then **Report a vulnerability**).
2. Describe what happens, the steps to reproduce it, and the Glance version or commit
   (`git -C ~/.local/share/glance-src rev-parse --short HEAD` for an installed copy).

Use synthetic text in your report. Never paste real messages, API keys or tokens; if a secret
of yours was exposed, rotate it first.

You should get a first reply within a week. We will keep you updated while we work on a fix,
credit you in the advisory unless you prefer otherwise, and publish the advisory once a fix is
released.

## Scope

In scope, for example:

- **Privacy leaks.** Typed or selected text reaching argv, disk, logs, `glance status`,
  notifications or crash output; text read from an app on the deny list, a private browser
  window, a password field or a banking/SSO page; text reaching an AI backend without
  redaction, or reaching a backend the user did not choose.
- **Write-back into the wrong place.** An edit typed or pasted into a different window or
  field than the one it was computed for, or interleaved with the user's own typing.
- **Key handling.** An API key stored outside the system keyring (libsecret), or exposed in
  the config, on argv, in logs or to another process.
- **Install and uninstall.** `install.sh`, `uninstall.sh` or `glance doctor --fix` changing
  files Glance does not own, running `sudo` without `--with-apt`, or fetching an unverified
  binary.
- **AI CLI lock-down.** A backend CLI being able to run shell commands, read or write files, or
  keep a session despite the flags Glance passes.

Out of scope:

- What an AI provider does with text you chose to send it under its own terms.
- Problems that need an attacker who already runs code as your user or controls your X
  server (X11 itself does not isolate applications from each other).
- Bugs in third-party tools (harper-ls, the vendor CLIs, GTK, AT-SPI); please report those
  upstream, though we are glad to hear about them if Glance should work around them.

## Supported versions

Glance is pre-1.0. Only the latest commit on `main` gets fixes; re-run the installer to update.
