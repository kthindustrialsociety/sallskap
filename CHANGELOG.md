# Changelog

Newest first. Each release has three parts:

- **Before you update:** what someone updating has to know or do first, written by hand. `kit update` shows it and asks before going ahead. It's never left out: a release with nothing to say says "Nothing."
- **Tool versions:** each tool image that changed, from the Compose files.
- **Changes:** what changed, one line per pull request.

A section is never edited once its release is tagged. A mistake is put right in the next release's section. See [docs/releasing.md](docs/releasing.md).

## 0.7.1 (2026-11-10)

Fixes for what the first real install found, by following the install guide alone, the way another association would.

### Before you update

- After updating, `kit status` warns that nobody has noted the backup password, even if you already have, as nothing recorded it before. Run `kit backup password` once, on a screen (over SSH, `ssh -t`), and the warning goes.
- The install guide now downloads the installer from the newest release, with a checksum, rather than from `main`.

### Tool versions

- None changed.

### Changes

- fix: what the first real install found (#21).
  - `kit status` checks DNS with a public resolver, so a cached answer can't look current. For a record that points elsewhere, it says how long the old answer can last.
  - `kit backup password` shows the password on a screen, and `kit status` warns until someone has seen it. It suggests pulling a copy off the server from another computer.
  - `kit site add` and `kit site key` print the key alone when it goes to a file, so it can be saved without copying it out of a terminal.
  - Quieter output: one line per step, with Compose's and Caddy's own output only when something fails. Core is never asked for from a registry.
  - The installer ends each step with `ok`, and introduces the `kit` command. `kit admin` and `kit link` say to treat the link like a password.
  - The install guide is rewritten, and a `Release` workflow attaches `install.sh` and `SHA256SUMS` to each release.

## 0.7.0 (2026-11-08)

The first release that can update itself. From here on, an installation moves to a newer release with `kit update`, which takes a backup first, and goes back with `kit update --rollback`.

### Before you update

- 0.6.0 has no `kit update`, so there's no update from it. Install 0.7.0 with `install.sh`, which now installs the newest release by default.
- A restore with `--force` now moves the folders it replaces aside, instead of restoring over them, and prints where they went. Delete them once you're sure.

### Tool versions

- Caddy 2.10 → 2.11.9, for its path-matcher fixes (CVE-2026-27587 and others). Caddy's path rules are what keep the closed Vaultwarden and Forgejo routes closed.
- Forgejo 15.0.11 and Vaultwarden 1.37.5, unchanged.

### Changes

- feat: record releases in VERSION and CHANGELOG.md (#16). `kit --version` says which release runs, and backups record it.
- feat: add kit update (#17). It shows what to read first and asks, prepares the new images while everything runs, backs up, switches, then checks the new version. If a check fails, it stops and prints `kit update --rollback`. Core's image is named after its version, so the previous one stays for a rollback. `kit status` says when a newer release is out. See [docs/updating.md](docs/updating.md).
- fix: update caddy to 2.11.9 for its path matcher fixes (#18).

## 0.6.0 (2026-11-06)

The first tagged version, at the end of build step 6. Sällskap is still in early development and not ready for others to use. Anything can change before 1.0.

### Before you update

- Nothing: this is the first release. It has no `kit update`, so install it with `install.sh`.

### Tool versions

- Caddy 2.10
- Forgejo 15.0.11
- Vaultwarden 1.37.5

### Changes

- `install.sh` turns a fresh Debian or Ubuntu server into a secured host and installs the kit. `install.sh --check` changes nothing.
- Core: Django with accounts, groups, 2FA and an admin page, and a login service (OpenID Connect) for other tools.
- The `website` module serves an association's public static sites and redirects its other domains.
- The `git` module: Forgejo, with login through core and teams from groups.
- The `vault` module: Vaultwarden, with login through core, where groups decide who gets in.
- A worker tells the tools when someone's access changes. `kit emergency` reaches the tools when core is down.
- `kit backup`, `kit backup export` and `kit restore` take encrypted nightly backups with restic, and restore a whole installation onto a fresh server.
- Not yet: `kit update`, email, members, events and the setup wizard.
