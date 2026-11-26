# Releasing and packaging Glance

The curl one-liner in the README stays the main way to install Glance. Releases and the
packages below are for people who prefer them.

## Versions and releases

- The version lives in one place: `version` in `pyproject.toml`. `glance --version`,
  `glance doctor`, `glance status` and the tray menu read it.
- Every release has a section in `CHANGELOG.md` (Keep a Changelog).
- To release: bump `version`, add the CHANGELOG section, merge to main, then tag and push
  `v<version>`. `.github/workflows/release.yml` runs the default test suite, builds the wheel
  and the source archive, and creates the GitHub Release. The release holds both files, a
  `SHA256SUMS`, and the CHANGELOG section as notes. A tag with a `-` (`v0.5.0-rc1`) becomes a
  pre-release.
- The grammar engine `glance_core` is released on its own `core-v*` tags
  (`core-wheels.yml`). Its pinned wheels are listed in `scripts/core-wheels.sha256`.

## How installs follow releases

- `install.sh --ref latest` is the default, also for the curl one-liner. It installs the newest
  `v*` tag (no pre-releases, never `core-v*`). Before the first release exists, it installs
  the main branch. `--ref main` follows main; `--ref v0.4.0` pins a release. `GLANCE_REF`
  does the same as `--ref`.
- A clone that is on a tag is a release install. The daily update check compares it with the
  newest GitHub Release ("Glance 0.4.0 → 0.5.0"). A clone on the main branch compares
  commits, as before.
- `glance update` installs the latest release, and `glance update --main` installs main. The
  tray's "Update now" keeps an install on the channel it already follows.
- A package install answers `glance update` with its package manager's command instead. The
  package writes a `glance/_packaged` file to make that happen.

## Package installs: `glance setup`

A package installs Glance's commands for the whole system, plus its support tree:
`install.sh`, `uninstall.sh`, `scripts/`, `packaging/` and `extension/`. Some parts must stay
per user, so each user runs `glance setup` once:

- Linux: the systemd user unit (it runs the package's `glanced`), the GNOME Shell extension
  on Wayland, harper-ls in `~/.local/share/glance/bin`, and `glance doctor --fix`.
- macOS: the LaunchAgent, harper-ls, and `glance doctor`.

`glance setup` runs the installer's own `--setup-only` mode. That mode writes the same install
manifest as a curl install. `glance setup --undo` runs `uninstall.sh`, which reverses exactly
that. Users should run `glance setup --undo` before they remove the package.

## AUR: `glance-writing-assistant`

Files: `aur/PKGBUILD`, `aur/.SRCINFO`, `aur/glance-writing-assistant.install`.

The package installs the release's source archive with `python-installer`, plus the pinned
`glance_core` wheel. Its dependencies are Arch packages. It conflicts with
`python-glanceclient`, which also ships `/usr/bin/glance`.

To publish:

1. Copy the three files into the AUR git repository
   (`ssh://aur@aur.archlinux.org/glance-writing-assistant.git`).
2. Run `updpkgsums`. It replaces `SKIP` with the archive's SHA-256, which you can also find
   in the release's `SHA256SUMS`.
3. Run `makepkg --printsrcinfo > .SRCINFO`.
4. Test with `makepkg -si`, then commit and push.

## Homebrew: the `Btocode/homebrew-glance` tap

File: `homebrew/glance.rb`. Copy it to `Formula/glance.rb` in
`github.com/Btocode/homebrew-glance`, then put the source archive's SHA-256 from
`SHA256SUMS` in place of `REPLACE_WITH_SHA256_FROM_SHA256SUMS`.

The formula builds a virtualenv in `libexec` and installs from PyPI at install time, which is
acceptable for a tap. It also installs the pinned `glance_core` wheel for the Mac's CPU and
puts the support tree in `share/glance`. Its caveats tell the user to run `glance setup` and
to allow Accessibility.

Users install with `brew tap Btocode/glance && brew install glance && glance setup`.

## Debian and Ubuntu: a `.deb` built with nfpm

Files: `debian/nfpm.yaml`, `debian/build-deb.sh`, `debian/postinst`, `debian/prerm`.

`build-deb.sh` builds `dist/glance_<version>_amd64.deb`. It needs network access, `uv` and
`nfpm`. Run it on Ubuntu 22.04 (Python 3.10). The package holds:

- `/opt/glance/wheels`: Glance, every dependency and the pinned `glance_core`, so nothing is
  downloaded when the package installs.
- The support tree in `/opt/glance`.

`postinst` builds `/opt/glance/.venv` with the system Python
(`--system-site-packages`, for GTK and AT-SPI) and links the commands into `/usr/bin`.
`prerm` removes the venv and the links. Per-user files stay until each user runs
`glance setup --undo`. Attach the `.deb` to the GitHub Release by hand, or add a job for it.
