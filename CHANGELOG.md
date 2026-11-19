# Changelog

Newest first. Each release has three parts:

- **Before you update:** what someone updating has to know or do first, written by hand. `kit update` shows it and asks before going ahead. It's never left out: a release with nothing to say says "Nothing."
- **Tool versions:** each tool image that changed, from the Compose files.
- **Changes:** what changed, one line per pull request.

A section is never edited once its release is tagged. A mistake is put right in the next release's section. See [docs/releasing.md](docs/releasing.md).

## 0.8.0 (2026-11-19)

Your association's structure comes to core: its associations and teams, its people and their memberships and positions, who can do what, and each person's access to the tools, worked out from those. The groups go, so read the first two points below before updating.

### Before you update

- **Add your association's name and key to the config,** under `[association]`, before you update. The update stops without them:

  ```toml
  [association]
  name   = "Example Society"
  key    = "example"
  domain = "example.org"
  ```

  The key is short, lowercase letters, digits and single hyphens, such as your association's usual abbreviation. Tools will know your association by it, so **it can never change.** If you forgot, the update says so: add them, then run `kit update --finish`.
- **Write down who is in each group before you update.** Access now comes from memberships and positions, and Operator is the only group. The update removes the Board, Treasurer, Events, Developer and Member groups, and any group made by hand, along with who was in them. Until they're recorded, they lose what the groups gave them: the vault, Forgejo's board team, and the admin page. Operators keep everything, if the group is still called Operator: if you renamed it, name it Operator again first. What has to be recorded:
  - a membership of your association for each member;
  - the board's positions on your association, chair included;
  - a team for what Events and Developer were for, with its lead and participants.

  Record your association's chair after updating with `kit chair <their email>`. The rest is recorded by the chair and the people they appoint, through the member pages and the command line, which come in a later release: the admin page no longer changes any of it (below).
- **The admin page shows your association's structure and no longer changes it:** associations and teams, people and their addresses, memberships, positions, reasons, grants and seat rules. Operators run the server and have no say over members, so these changes are made by people whose positions allow them, and the member pages and command line for that come in a later release. Operators still add and change accounts, make Operators, remove someone's two-factor authentication, mark follow-ups done, and merge two people. `kit chair` records your association's chair.
- **Django's own admin history is deleted by the update,** as it kept the names and addresses of what was changed, with no way to delete them. The new audit log records changes from the update on, naming nobody. There's nothing to do.
- **The admin page is for Operators only now.** Board members no longer reach it, and the vault's follow-ups go to Operators. The board's own pages come in a later release.
- **If Git is on, refresh Forgejo's sign-in after updating,** so it maps your association's chair and vice chair the way core does: run `kit tool remove forgejo`, then `kit enable git`. Signing in to Forgejo doesn't work between the two. Forgejo's old developers team is left as it was, and nobody is added to it any more: give its people a team in core, then delete it in Forgejo.
- **If you push backups to a target with `[backup] target`,** the first backup after updating forgets the old ones there, as the server does, keeping 7 daily, 4 weekly and 6 monthly. Anything older than about six months goes. The server forgot those long ago, so if you want to keep one, copy the target itself somewhere else before updating. A pulled copy, from `kit backup export`, is yours to keep: keep the newest, and none older than six months, as each holds every backup.
- **Core now deletes people it has no reason to keep,** every night just after midnight: someone with no account, no membership or position that's current or starts later, and no reason that hasn't expired, 30 days after the last of those ended. Their ended positions stay, under their name and nothing else, as your association's record of who held what. Updating from 0.7.2 deletes nobody, as the update makes a person for each account, and each still has it. From then on, deleting an account deletes its person too, the night after, once nothing holds them. Deleted people stay in your backups until those age out: see [docs/restore.md](docs/restore.md). An operator can see who would go with `docker compose exec core python manage.py delete_people --dry-run`, run in `/opt/sallskap`.
- **A tool you limited with `kit tool add --group`** to developer, events or a group made by hand lets nobody in now. Remove it with `kit tool remove`, then add it again with the keys you want, such as `board` or `website:lead`. The vault needs nothing: it already uses `board` and `operator`.

### Tool versions

- None changed.

### Changes

- feat: units, the tree of associations and teams (structure.md §1).
  - Core creates your association as the root unit, from the config's name and key, and keeps its name in step with the config. The admin page shows the units inside it, under Units.
  - A unit's key is fixed once set and never reused. A project is archived when it's over, which archives everything inside it.
  - Nothing is visible to members yet.
  - `kit init` now asks for the association's name and key.
- feat: people, and the ties between them and units (structure.md §2 and §3).
  - Every account becomes a person, with its name and its address, marked verified. Accounts themselves don't change, so neither does anything the tools see.
  - The admin page shows people, memberships, positions and reasons, under People. Ties are ended rather than deleted, so the record of who held what is kept, until core deletes someone with nothing left (below).
  - Only members hold board roles or lead teams, and membership of an association counts for every association above it.
  - When an account's address is changed, the old one stays on its person, unverified, so it can be given to someone else, such as a role's shared address.
- feat: authority, and grants (structure.md §4).
  - One check decides what each role can do, and in which units, from current positions and grants. Every change to the tree asks it (actions, below).
  - The admin page shows grants, under Grants. A grant gives one permission, such as fees and treasury, to a role in a unit, so it passes to whoever holds that role next. Appointing people, changing units and giving grants can't be granted.
  - **Two-factor authentication is required for anyone with a board role in any association,** from the day the position is recorded, as well as for Operators as before. Someone given a board role is asked to set it up before any tool lets them in.
- feat: seat rules, for posts that come with another (structure.md §4).
  - A seat rule gives a role in one unit to whoever holds certain roles in another, such as a seat on a federation's board for the chair of each association inside it, or a department led by an association's vice chair. The admin page shows them, under Seat rules. A rule is only accepted from someone who could appoint in that unit.
  - Core gives each seat itself, as a position, from the day the position it comes from starts, and ends it when that position or the rule ends. A seat gives what its role gives, grants and two-factor authentication included, and stays in the record when it ends. It can't be set, changed or ended by hand.
- feat: keys to the tools, worked out from memberships and positions (structure.md §5).
  - Tools are told keys: `<unit>:<role>` for each position, seats included, and `<association>:member` for each association someone is a member of, counting upwards. `member`, `board` (a board role on your association), `treasurer` (its treasurer) and `operator` are the same in every installation, for modules to use. Only current ties in units that aren't archived count, and keys from roles that need two-factor authentication wait until it's set up.
  - A change to a membership, a position, a seat or the tree reaches the tools within seconds, so archiving a project ends its people's access at once. Ties that start or end on a day with no change, such as a board taking office on 1 July, are picked up just after midnight.
  - The worker takes the newest change each time, so one made while it goes over everyone, as it does after a restart, goes next rather than after the people it had already picked up.
  - Operator is the only group. The admin page is for Operators only, and each account's page there shows its keys.
  - `kit tool add --group` takes any key, such as `robotics:chair` or `website:lead`.
  - Forgejo: each association gets an organisation, yours being the one named after your domain. A team gets a Forgejo team its people write to, and `<team>.lead`, from which its leads administer the team's repos and create new ones. Each organisation has a `members` team, which reads the repos a lead opens to it, and its association's chair and vice chair own it. Core makes these when someone first needs them, and leaves anything made by hand alone. See modules/git/README.md.
  - The unit key `custom` is no longer reserved.
  - `tests/live_check.py` gives its fake people memberships and positions instead of groups.
- fix: `tests/live_check.py` turns off its fake people's own Forgejo 2FA before removing them. Removing them left the 2FA behind, so on a test server Forgejo's own check warned of one more orphaned 2FA record after every run. A test server with those warnings can clear them once with `docker exec sallskap-forgejo-1 forgejo doctor check --run check-db-consistency --fix`.
- feat: actions, and the audit log (structure.md §4 and §7).
  - Every change to associations, teams, memberships, positions, reasons, grants and seat rules is an action in core, which asks once whether the person can do it, in that unit, today. Moving, archiving, bringing back and deleting a unit, and setting up a service for another association, are decided one level up. An appointment that starts later is checked when it's recorded, so the outgoing board records the next one.
  - The audit log records each action, with who did it, where, through which way in, and what changed, and also Operators' changes to accounts and `kit admin` and `kit chair` on the server. It names nobody, so it never keeps anyone's data after they're deleted, and it's only ever added to. Operators read it in the admin page, under Audit log.
  - The admin page shows the tree, people and their ties, and changes none of them. Operators merge two people there, which the log records.
  - `kit chair <email>` records your association's chair, and makes them a member of it if they aren't one. It's how a new installation records its first chair.
- fix: a backup target set with `[backup] target` now forgets old backups after each copy, as the server does, keeping 7 daily, 4 weekly and 6 monthly. It kept every backup for ever, and with them everyone since deleted.
- feat: deleting people with nothing left (structure.md §3).
  - Every night just after midnight, core deletes everyone with no account, no membership or position that's current or starts later, and no reason that hasn't expired, 30 days after the last of those ended, or after they were added if they never had one. Each night works out afresh who that is, so a night missed costs nothing, and someone a restore brings back goes again the next night.
  - Their ended positions stay, seats included, under their name and nothing else, and can't be changed. Grants and seat rules they gave stay as they were, and show their giver as since deleted. Their addresses, memberships and reasons go. The tools are told nothing: someone without an account has nothing in them.
  - The audit log gets one row for each night that deleted anyone, counting what went and naming nobody.
  - `manage.py delete_people --dry-run` says who would go today; without `--dry-run`, it deletes them now.
- chore: core runs django-allauth 65.22.1 and gunicorn 27.0.1. allauth's fixes to the login service come with it: a code can be used once even when two tries come together, and using it twice revokes what it gave. A code given to an account turned off since still gets no tokens: allauth stopped checking that in 65.22, so core does.


## 0.7.2 (2026-11-10)

The update to 0.7.1 failed its last check on a fresh install. This fixes the check, and lets a failed update be finished without a rollback.

### Before you update

- If your update to 0.7.1 failed with "Forgejo's own check failed: … 1 configuration files with errors", nothing is wrong with your installation, and there's nothing to do first. Run `kit update`. It updates to 0.7.2, and this time the check passes.

### Tool versions

- None changed.

### Changes

- fix: let forgejo's check pass on a fresh install, and finish a failed update (#23).
  - Forgejo only makes its repositories folder with the first repo, and its own check fails without it. `kit` now makes it, as it already did the log folder.
  - When Forgejo's check exits with an error, `kit update` shows the lines that name the problem, not only "1 configuration files with errors".
  - `kit update --finish` runs the checks again after a failed update, once its cause is fixed. Before, the only way out it offered was a rollback. A failed update and `kit status` both say so.

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
