# Sällskap — a self-hosted, modular platform for student associations

*Specification v0.4 · 2026-11-13 · KTH Industrial Society (KTHIS) · working title*

> *Sällskap*: Swedish for "society", a group of people who choose to do something together.

---

## 1. Summary

Sällskap is a free and open-source kit that lets a student association run its own websites, member dashboard and board tools on one small server that it controls. Everything runs from one config file. Every person has one account that works across all tools. Each association picks the modules it wants.

KTHIS builds it and is its first installation. Any other association should be able to reproduce KTHIS's setup in about 30 minutes without Linux knowledge.

## 2. Problem

- **Board turnover.** Boards change every year. Passwords, accounts, documents and know-how leave with the people who held them.
- **A patchwork of free plans.** Associations string together Slack, Google Workspace, Supabase, Cloudflare and form tools. Each one has limits (Slack hides history after 90 days, databases pause when inactive), a separate login and a separate owner.
- **No ownership of data.** The member register, finances and history sit with third parties, under their terms and prices.
- **Everyone reinvents it.** Every association solves membership, events and fees on its own, usually badly.

## 3. Goals and non-goals

### Goals
1. **One account for everything.** A member logs in once; groups decide what they can reach.
2. **Modular.** An association turns on only what it needs: website, dashboard features, password vault, Git, chat, files.
3. **Lean.** One server, one Compose file, one config file, one backup method.
4. **Easy to reproduce.** A fresh server plus one pasted snippet gives a working installation.
5. **Survives handovers.** Access is tied to roles, not people. Restore and handover are documented and one command each.
6. **FOSS only.** Every component is free and open source. Nothing proprietary is required to run it.
7. **Portable.** All data lives in plain folders and standard databases, and can move to any Linux server.

### Non-goals
- **Hosting email.** Mail stays with a hosted provider, because deliverability is too hard to guarantee from a self-hosted server.
- **High availability or multi-server clustering.** One server with good backups is the target.
- **A multi-tenant SaaS.** Each association runs its own installation. A shared hosting service for associations may come later (see §14), but it runs the same kit.
- **Replacing KTH or THS systems.**

## 4. Users and roles

| Role | Typical access |
|---|---|
| Visitor | Public website, public event list, join form |
| Member | Member portal: profile, fees, event sign-ups, chat |
| Board | Admin for members, events and governance; password vault |
| Treasurer | Fees and treasury |
| Events | Events and sign-ups |
| Developer | Git, build runner |
| Operator (IT, 1–2 people) | Server, backups, updates, module config |

Roles come from the structure in [structure.md](structure.md): an association, the associations and teams inside it, and each person's memberships and positions in them. A board role in an association inside another gives board access to that association and everything under it, not the whole installation. Operator is the one role given by hand. Someone signed up for an event or following an association needs no account. Every module maps access to roles (§8).

## 5. Principles

1. **KTHIS is installation #1.** KTHIS installs the same way anyone else does. If KTHIS needs a manual step, that's a bug in the kit.
2. **Config, not code.** Everything specific to one association lives in its config file, never in the code.
3. **SQLite by default.** Postgres only for modules that require it.
4. **The hard parts stay hosted.** Email and DNS are left to the providers that do them well.
5. **One way to do each thing.** One entry point (Caddy), one backup tool (restic), one login (OpenID Connect via core).
6. **Nothing is added without an owner.** Each module an association enables has a named person responsible for it.
7. **Presets are guaranteed; other combinations are best-effort.**

## 6. Architecture

```
 Visitors · members · board
          │
          ▼
 Registrar DNS
   <domain>, *.<domain> → server          MX → hosted mail provider
   each site domain + alias → server
          │
          ▼ HTTPS
┌─ One Linux server · Docker Compose ───────────────────────────────┐
│ CORE (always on)                                                  │
│  Caddy           HTTPS certificates + routing by subdomain        │
│  Django          accounts · roles · login for all modules (OIDC)  │
│                  + dashboard features (members, events, fees …)   │
│  restic          nightly encrypted backup of /srv                 │
│  kit CLI         enable · disable · update · backup · restore     │
│                                                                   │
│ MODULES (optional)                                                │
│  website   one or more domains   public sites + redirects         │
│  vault     vault.<domain>    Vaultwarden                          │
│  git       git.<domain>      Forgejo + build runner               │
│  chat      chat.<domain>     Matrix (Continuwuity) or Zulip       │
│  files     cloud.<domain>    Nextcloud + Collabora  (needs postgres)
│  postgres  internal          enabled automatically by dependency  │
│                                                                   │
│ /srv/<module>/   all data, one folder per module                  │
└──────────────┬────────────────────────────────┬───────────────────┘
               │ SMTP (587)                     │ restic (nightly)
               ▼                                ▼
   Hosted mail provider                Off-server backup storage
   (mailboxes + app mail)              + periodic offline copy
```

### Core components

| Component | Choice | Why |
|---|---|---|
| Web server | Caddy | Automatic HTTPS, very short config, imports one config snippet per module |
| App and login | Django (Python), SQLite | Mature (LTS releases), role-based admin built in, large community |
| Login protocol | OpenID Connect, with Django as the login service (django-allauth's provider) | Supported by every planned module; one user database |
| Two-factor and email verification | django-allauth | Set up once in core, protects every module |
| Backups | restic | Encrypted, deduplicated, writes to any SFTP or S3-compatible target |
| Packaging | Docker Compose, versioned images | Identical on every server |

### Rejected alternatives (decision log)

| Decision | Rejected | Reason |
|---|---|---|
| Own server | Cloudflare Workers/D1, Supabase, Firebase | Per-request CPU caps, user caps, pausing projects, multiple accounts, weak ownership of data |
| Django | PocketBase | PocketBase is pre-1.0 with essentially one maintainer, and its admin is superuser-only, so the board can't get role-limited access |
| Django as login service | Authentik, Kanidm | Adds a service, and people would be managed in two places. Revisit if a module needs features Django can't provide. |
| django-allauth's provider | django-oauth-toolkit | Shares core's login, 2FA and login limits instead of sitting beside them. django-oauth-toolkit is the fallback if a module needs something allauth's provider lacks. |
| No Amazon SES, no Cloudflare | — | Fewer providers; FOSS preference |
| Matrix or Zulip | Mattermost | Mattermost's free edition caps viewable history at 10,000 messages |
| Hosted email | Self-hosted mail | Outgoing mail ports blocked by default on cloud servers; spam filtering and blocklists |

## 7. Module system

### 7.1 Module contract

Each module is a folder under `modules/<name>/` containing:

| File | Required | Content |
|---|---|---|
| `module.toml` | yes | Name, description, default subdomain, RAM estimate, dependencies, data paths, access group, session length if it needs its own, setup questions |
| `compose.yml` | yes | The module's containers, with memory limits |
| `site.caddy` | if web-facing | Caddy routing lines, imported automatically, with `__HOST__` for its hostname |
| `connector.py` | if it can | How core tells the tool about changes to someone's access straight away, without waiting for their next login: see §8 |
| `backup.sh` | optional | Pre-backup step, e.g. a database dump |
| `README.md` | yes | What it does, who should own it, how to restore it |

Example `module.toml`:

```toml
name        = "git"
description = "Code hosting, issues and builds (Forgejo)"
subdomain   = "git"
ram_mb      = 1700            # Forgejo ~200 MB + runner capped at 1.5 GB
requires    = []
data        = ["/srv/git"]
access      = ["developer"]   # group mapped via OIDC
oidc        = true

[[setup]]
key    = "runner"
prompt = "Enable the build runner?"
type   = "bool"
```

### 7.2 Rules

1. Every module with a login **must** support OpenID Connect against core.
2. Modules never read or write each other's data; sharing goes through core.
3. Dependencies are declared in `module.toml` and switched on automatically.
4. Every container has a memory limit.
5. Backup and restore are identical for every module: restic over the folders it declares under `data`, plus an optional `backup.sh` run before the copy. A module that is switched off is still backed up, because disabling one keeps its data.
6. Disabling a module keeps its data. Only an explicit `kit remove` deletes it.
7. Only Caddy publishes ports. Ports published by Docker bypass the server's firewall, so any other published port is a bug, and the tests fail on it.

### 7.3 Association config

```toml
[association]
name   = "Example Society"
domain = "example.org"         # primary domain: app., vault., git. and so on live here
logo   = "logo.svg"
colors = { primary = "#…", accent = "#…" }

[mail]
smtp_host = "…"
from      = "no-reply@example.org"

[backup]
target = "sftp:…"            # optional: also push each backup here. See §10.4

[modules]
website  = true
vault    = true
git      = false
chat     = false
files    = false

# One block per public site. Most associations need exactly one.
[[website.sites]]
domain  = "example.org"
aliases = ["example.se", "old-name.se"]

[[website.sites]]
domain  = "example-robotics.org"
aliases = ["exrobotics.org"]
```

The config and secrets live on the server and are included in backups. They are never committed to the public repo.

### 7.4 Presets (tested on every release)

| Preset | Modules | Suggested server |
|---|---|---|
| Minimal | core + website | 2 CPUs / 4 GB |
| Board | Minimal + vault + git | 2–4 CPUs / 4–8 GB |
| Full | Board + chat + files | 4–8 CPUs / 16 GB |

Each preset is tested with one site and with several.

### 7.5 Domains and websites

An installation has one **primary domain** and any number of **site domains**.

**Primary domain**
- Set in `association.domain`. Every service lives under it (`app.<domain>`, `vault.<domain>`, `git.<domain>`), and the login issuer is `https://app.<domain>`.
- Treat it as permanent. Modules store the issuer, so changing it is a migration: `kit domain move <new>` updates every module and keeps the old service hostnames redirecting. Each OIDC module's README states how it handles the move, and the move is covered by tests.
- Serving a site on the primary domain is optional. Without one, the bare domain redirects to `app.<domain>`.

**Sites**
- Each `[[website.sites]]` block is one static site, with its own domain, certificate and folder at `/srv/website/<domain>/`.
- `www.<domain>` redirects to the bare domain automatically for two-part names such as `example.org`. Set `www` for names the kit can't judge, such as `example.co.uk`.
- Fields: `domain` (required), `aliases`, `spa` (send unknown paths to `index.html`, on by default) and `www`. A site domain can be a hostname such as `links.example.org`.
- `kit` refuses a site hostname that collides with a service hostname.

**Aliases**
- An alias answers with a permanent redirect (301) to its site's domain and keeps the path and query string.
- Aliases are web-only. Mail on an alias domain is never touched, so a domain can redirect its website and still receive mail.

**Publishing**
- Sites are built outside the server (static output only) and published from CI, in keeping with §12: no shell on the server for developers.
- `kit site add` creates an SSH deploy key that can publish to that one site and do nothing else. It's shown once and isn't kept on the server. The CI system is the association's choice; publishing only needs `tar` and `ssh`.
- Uploads may only contain plain files and folders, must have `index.html` at the top level, and are size-limited.
- Each publish is a new release folder, and the site switches to it atomically. A failed publish leaves the running site untouched. The last five releases are kept, and `kit site rollback <domain>` restores the previous one.
- Compression, HTTP/3, security headers, and long-lived caching for hashed assets are on by default for every site.

**Separation**
- Session cookies are host-only and never set on a parent domain. Sites share no login state with the services; logging in always goes through `app.<domain>`.
- The `widgets` endpoints accept cross-origin requests only from the installation's own site domains.

**DNS**
- `kit status` checks that every domain and alias points at the server and shows the exact record to add when one doesn't.
- SPF, DKIM and DMARC are checked for the domain in `mail.from`.

## 8. Identity and access

- **One account per person,** stored in Django. Every module has a "Log in with <association>" button (OpenID Connect). People who only sign up for events or subscribe have no account ([structure.md](structure.md) §2).
- **Groups carry access:** the login response includes the user's groups, and each module maps them to its own permissions, e.g. Forgejo teams or admin rights. A tool that can't do this, such as Vaultwarden, can be limited to certain groups in core instead. Built-in groups are sent by a fixed key, so renaming one doesn't change anyone's access.
- **Groups are worked out from the structure:** apart from Operator, nobody is put in a group by hand. Each person's current memberships and positions become keys such as `robotics:member` or `website:lead`, beside the built-in ones. See [structure.md](structure.md) §5.
- **Membership drives accounts:** when a membership lapses or someone leaves the board, changing it in Django updates their access within seconds, in every tool whose module has a connector, and at their next login in the rest. Whether a tool's API can do this counts when choosing one.
- **How core reaches tools:** a change to someone's access is saved along with a note that the tools need telling. A worker in its own container, with no ports, picks that up within seconds, retries if a tool is down, and the admin page says if it's stuck. It's the only process with the tools' admin tokens, so core's web process, which faces the internet, never has them. Each module's `connector.py` does the talking, using only the standard library. It also goes over everyone once a day and when it starts, which covers a tool restored from a backup.
- **When someone leaves:** what can be done for them is done at once, and anything only a person can do becomes a follow-up on the admin page that stays open until someone marks it done, such as changing shared passwords.
- **If core is down:** each module's README says how to get in. Nothing opens a way around 2FA at a public address: an emergency way in needs SSH on the server.
- **Sessions:** short lifetimes for sensitive modules (vault: 8–24 h).
- **Two-factor authentication:** required for the board, treasurer and operator groups; optional for members.
- **Passwords:** hashed with Argon2 (Django supports it). Login attempts are rate-limited.
- **Known exceptions:**
  - Git pushes use personal access tokens created after logging in.
  - Vaultwarden keeps a separate master password by design.

## 9. Dashboard features (Django apps)

All feature apps are always installed and switched on or off in the admin with **feature flags**. A disabled feature is hidden from menus, the member portal and the widgets, and its data is kept.

| Feature | Contents | Requires |
|---|---|---|
| `core` (always on) | Accounts, login, people, associations and teams, positions, subscriptions, association settings, audit log, API | — |
| `members` | Join form, member register, membership years, export | — |
| `events` | Events, capacity, sign-ups with only an email, attendee lists | — |
| `mailings` | Newsletters and event announcements to subscribers, and member notices | — |
| `fees` | Membership fee per year, paid/unpaid, reminders | members |
| `governance` | Board composition per year, meetings, minutes, statutes, annual meeting | — |
| `treasury` | Transactions, receipts (file upload), budget, recurring costs, invoices, requisitions | — |
| `pages` | Simple public pages edited in the admin, for associations without a website | — |
| `widgets` | Embeddable snippets and small public data endpoints for enabled features | — |

**Invoices and requisitions** (in `treasury`):
- **Invoices** bill a customer or partner. **Requisitions** claim money from a funder under a grant decision, and record the decision, project and funder's reference.
- The treasurer creates both in the dashboard and downloads a finished PDF with the association's logo, organisation number and payment details.
- Either can be issued in the name of the association or one of the associations inside it, with that association's name and logo.
- Numbers are assigned automatically and run in sequence, and an issued document can't be edited. A mistake is corrected with a new document, so the records stay reliable for bookkeeping.
- Each one has a status (draft, sent, paid) and can be linked to the transaction that paid it.

**Interfaces:** every action is one function in core with one permission check, reached in three ways ([structure.md](structure.md) §7 and §8):
- **Command line and scripts:** a JSON API, and a CLI that anyone can run on their own computer, limited to what their positions allow.
- **Board, leads and members:** server-rendered pages (Django templates, optionally htmx); no JavaScript build step.
- **Operators:** Django admin, as a back office. Its permissions can't be limited to one association or team, so the board of an association inside another uses the pages or the CLI.
- **Public:** `pages` or the association's own site with widgets.
- **Language:** Swedish and English.

## 10. Installation and operations

### 10.1 Install (target: 30 minutes, no Linux knowledge required)

1. Rent a server that meets §13 and choose a supported operating system. Adding an SSH key at this step is recommended.
2. Run the one-line installer, the same line at every provider, in whichever way the provider allows:
   - a "user data" or cloud-init field when ordering, if there is one (`cloud-init.yaml` calls the same installer)
   - otherwise the provider's browser console or SSH
3. The installer runs the checks in §10.2, secures the server, starts core and prints a one-time setup link.
4. Add the DNS records the kit displays at the registrar.
5. Open the setup link at `app.<domain>/setup`. It only works with the link's one-time token, so nobody else can claim a new installation. The setup wizard asks for:
   - name, logo and colours
   - the first admin account
   - mail settings and backup target
   - board roles, membership fee and year
   - modules to enable (shown with RAM estimates and a suggested server size)
   - public sites and their domains (optional; more can be added later)
6. Done. The installation reports its status.

### 10.2 Checks, not assumptions

An association should never have to ask its provider how the server works. The kit finds out for itself and explains what to do in plain language.

| Check | When | If it fails |
|---|---|---|
| Operating system, processor type, RAM, disk | Before installing | Stop, and say what to choose instead |
| Public IPv4 address | Before installing | Stop, and explain how to add one |
| Ports 80 and 443 free on the server | Before installing | Stop, and name what is using them |
| Outgoing mail on port 587 | Before installing, and when mail settings change | Warn; the wizard offers a relay or another port |
| Ports 80 and 443 reachable from outside | When the first certificate is issued | Explain that the provider's firewall may be blocking them |
| An SSH key is present | Before locking down SSH | Keep password login, and warn until a key is added |

`install.sh --check` runs the first group alone and changes nothing.

### 10.3 The `kit` CLI

| Command | Action |
|---|---|
| `kit init <domain>` | Create the first config (the setup wizard does this in a browser) |
| `kit admin <email>` | Create the first admin, or restore someone's Operator access and clear any login lockouts. `--reset-2fa` also removes their 2FA |
| `kit link <email>` | Create a one-time link that lets someone choose a password. It doesn't log them in, so it can't get around 2FA |
| `kit tool add <name> <callback> [--group <group>] [--session-hours <1-24>]` / `kit tool list` / `kit tool remove <name>` | Register a tool with the login service and show its settings and secret once, optionally limited to certain groups; list tools; remove a tool and end its access |
| `kit emergency vault [--off]` / `kit emergency git <username>` | Get into a tool while core is down: a private copy of the vault reached over SSH; a token on someone's own Forgejo account |
| `kit enable <module>` | Ask the setup questions, show any DNS record needed, start the module |
| `kit disable <module>` | Stop the module and keep its data |
| `kit remove <module>` | Delete its data after confirmation |
| `kit site add` / `kit site remove <domain>` | Add a site and its deploy key and show the DNS records; remove a site, keeping its files |
| `kit site key <domain>` | Replace a site's deploy key |
| `kit site rollback <domain>` | Switch a site back to its previous release |
| `kit domain move <new>` | Move the installation to a new primary domain (§7.5) |
| `kit update [<version>]` / `kit update --rollback` | Show what to read first and ask → prepare the new images while everything runs → back up → switch and restart, which runs the database migrations → check the new version, and stop with the way back if a check fails. `--rollback` goes back to the code and backup from before the last update. See [updating.md](updating.md) |
| `kit backup` | Take one now. Logins and the tools pause for it; the public sites don't |
| `kit backup export [file\|-]` | Write every backup as one encrypted file, for a copy off the server. `-`, the default, streams it to standard output, so one line over SSH takes a copy |
| `kit restore [file\|-] [--from REPO] [--snapshot ID]` | Full restore onto a fresh server, from an exported file, from an off-server target, or from the backups on this server. Needs the backup password |
| `kit status` | Enabled modules, RAM use, last backup, certificate expiry, available updates |

The same status is shown on a page in the admin.

`kit` is for operators, as root on the server. Managing the association itself, such as members, teams and events, is done with the member CLI from anyone's own computer ([structure.md](structure.md) §8).

### 10.4 Backups

Three layers, in [docs/restore.md](restore.md). Nothing in them depends on the hosting provider, so an association on any provider gets the same safety net and the same restore.

1. **A nightly encrypted backup on the server,** restic over the config and every folder a module declares, kept for 7 days, 4 weeks and 6 months. Logins and the tools pause for it, so that core's copy agrees with each tool's copy; the public sites stay up. This covers a deleted repository or a wrecked database, and it's what the other two layers carry.
2. **A copy off the server.** `kit backup export` writes every backup as one encrypted file, streaming to standard output by default, so one line over SSH takes a copy by hand, and the same line in a cron job on a machine the association trusts takes one automatically. Pulling is preferred: a server that can write to its own off-server copy can also be made to delete it. For an association with storage but no machine, `[backup] target` pushes to any restic repository (SFTP or S3-compatible) on each run.
3. **An offline copy of the password,** written down, kept away from both the server and the backups. The other copy is in the vault, which is inside the backup, so only the offline one survives losing the server. Whoever holds both a backup and the password can read everything, so that should be someone who already has root; the password alone opens nothing.

`kit status` warns when the last backup, or the last copy off the server, gets old.

A **test restore every semester** is part of the handover checklist, onto a throwaway server, using the offline copy of the password alone. That copy holds real personal data, so deleting the server afterwards is part of the test.

### 10.5 Monitoring
- An external uptime check (not on the same server) emails the operator.
- `kit status` warns about failed backups, low disk space and expiring certificates.

## 11. Email

- A hosted provider holds the mailboxes and handles app mail (verification, password reset, notices) over SMTP.
- DNS records are set up correctly and checked by `kit status`: SPF, DKIM, DMARC.
- Apart from `mailings`, the app sends as little mail as possible.
- `mailings` sends only to people who confirmed a subscription, or to members on member business, always with a one-click unsubscribe ([structure.md](structure.md) §6). Whether the provider's limits allow that volume is open.
- Many providers block outgoing port 25, so the kit always sends through the configured provider, usually on port 587 (§10.2).

## 12. Security and privacy (GDPR)

- The member register is personal data, and the association is the data controller. The kit ships templates for:
  - a privacy policy
  - a data processing agreement (for the hosting provider)
  - retention rules (e.g. delete lapsed members after N years)
- Members can export and delete their data from the member portal. Someone without an account, such as an event sign-up or a subscriber, does the same from the link in their emails.
- Data on a non-member is kept only while there's a reason with an expiry, such as an upcoming event. A person with nothing left is deleted ([structure.md](structure.md) §3).
- Access: 1–2 operators have root on the server, which is also how they get into tools if core is down. Developers and incubator teams deploy only through Git, never with a shell on the server.
- Databases are never exposed to the internet; everything goes through Caddy.
- Backups are encrypted. Keys are kept in the password vault and one offline copy.
- An audit log records admin actions on members and finances.
- `SECURITY.md` in the repo explains how to report vulnerabilities.

## 13. Server requirements and cost

**Requirements**
- A full virtual machine (KVM or similar), not a container.
- Debian 12 or 13, or Ubuntu 24.04 or 26.04, on x86-64 or ARM64.
- A public IPv4 address, and inbound TCP 80 and 443 plus UDP 443.
- SSH or a browser console for running the installer.
- RAM and CPU for the chosen preset (§7.4).

**Typical monthly cost, November 2026**

| Item | Minimal | Full |
|---|---|---|
| Server | about 50–100 kr | about 200–300 kr |
| Off-server backup storage | about 45 kr | about 45 kr |
| Hosted mail, if not already in place | about 75 kr | about 75 kr |

Moving to another server or provider is `kit restore` onto a fresh installation (§10.3).

## 14. Open source and distribution

- **License:** AGPL-3.0.
- **Code:** the public repo lives on GitHub for visibility, owned by a KTHIS organisation account, not a personal one. It is mirrored to KTHIS's own Forgejo.
- **Releases:** semantic versions, at 0.x until another association can install and update the kit on its own, a changelog that says first what someone updating has to do, and versioned container images. See [releasing.md](releasing.md) and [CHANGELOG.md](../CHANGELOG.md).
- **Docs:**
  - installation guide with screenshots
  - operator handbook
  - handover checklist
  - GDPR templates
- **Public demo** that resets nightly.
- **Adoption path:**
  1. KTHIS in production.
  2. 1–2 other KTH associations via THS.
  3. Public launch (self-hosting communities, other universities).
  4. Possibly KTHIS-hosted installations for associations that cannot run a server.

## 15. Roadmap

| Phase | Scope | Done when |
|---|---|---|
| **0 — Foundation** | Core (Caddy, Django accounts and roles, restic, kit CLI v0), `website` module, `members` + `events`, setup wizard v0 | All KTHIS sites and app.kthindustrial.com run on the kit; board manages members and events in the admin |
| **1 — Board tools** | `fees`, `governance`, `vault` + `git` modules, login to both via core, one-line install on any provider | KTHIS board uses one login for dashboard, vault and Git; restore tested |
| **2 — Workspace replacement** | `files` (Nextcloud + Collabora, postgres), `chat` (Matrix or Zulip), `treasury`, mail moved to a hosted EU provider | KTHIS stops paying for or depending on Google Workspace and Slack |
| **3 — Release** | Presets tested automatically on every release, docs, demo, GDPR templates, second association installed | Another association installs it on its own in under an hour |

## 16. Open questions

1. **Name.** "Sällskap" is a working title. The commands are named with it: `kit` and the member CLI, which should become one command.
2. **Chat:** Zulip (better UX; phone notifications need Zulip's relay service) or Matrix (fully FOSS, lighter, federated)?
3. **Vaultwarden login through core:** answered at step 4 of the build: yes, without a fork. See `modules/vault/README.md` for its limits.
4. **Payments:** membership fees via Swish, reconciled manually or through an integration? Out of scope for v1 unless decided otherwise.
5. **Setup link on cloud-init installs:** nobody sees the installer's output when it runs from a "user data" field, so the one-time link needs another route. One option is a token the person generates and includes in the pasted snippet.
