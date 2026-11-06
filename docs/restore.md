# Backups and restoring

What is kept, how a copy leaves the server, and how to put an installation back. The design is in §10.4 of [the spec](spec.md).

## What a backup holds

One encrypted [restic](https://restic.net) repository in `/srv/backup/repo`, holding:

| What | Why it's needed |
|---|---|
| `/etc/sallskap` | The config, and every secret: core's keys, each tool's settings, the worker's admin tokens, the backup password itself |
| `/srv/core` | Accounts, groups, 2FA records, and what's waiting to be told to a tool |
| Each module's `data`, from its `module.toml` | Today `/srv/git`, `/srv/vault` and `/srv/website`. A module that's switched off is kept too, because disabling one keeps its data |
| `/srv/caddy` | The certificates, so a restored server serves HTTPS without asking for new ones |
| `/var/lib/sallskap-deploy` | The publishing keys, so CI keeps working after a restore |

A module can add a step of its own before the copy, as `backup.sh` in its folder: see §7.1 of the spec. It runs while the module is still up, for anything that needs a running database, and writes inside the module's own data folder.

**The copy has to be encrypted,** because core's database holds everyone's 2FA secrets as they are, without encryption, which is what allauth does by default.

## The password

Made once, on the first backup. It's kept in `/etc/sallskap/backup.env`, which only root can read, and it goes inside the backups as well, so a restored server carries on with the same one.

It's printed when you run `kit backup` yourself. It is deliberately **not** printed when the output isn't a screen — the nightly run is the first backup on a server nobody has touched, and printing it there would write it into the system journal, where it would outlive the installation and travel in any copy of the logs. If the nightly run got there first, read it on the server:

```sh
sudo grep RESTIC_PASSWORD /etc/sallskap/backup.env
```

**Nothing in a backup can be read without it, and nothing can recover it.** So it lives in two places:

1. In the vault, where the board can find it.
2. On an offline copy, written down, kept somewhere that isn't this server and isn't with the backups.

The offline copy is the one that counts, because the vault is *inside* the backup: in the disaster where the server is gone, the vault is gone with it. **A test restore has to use the offline copy alone,** or it hasn't tested anything.

Whoever holds a backup *and* the password can read every member's details and mint anyone's 2FA codes, so that should only be someone who already has root on the server. Holding the password alone opens nothing, so a second person can safely keep a sealed copy without having any access.

## Taking one

Nightly, by `sallskap-backup.timer`, at 03:00 with up to 15 minutes of jitter. By hand:

```sh
kit backup
```

Logins and the tools stop for it, so that core's copy agrees with each tool's copy: Forgejo's database runs in WAL mode and has to match its repositories, and Vaultwarden's database has to be still while it's read. **The public websites stay up throughout,** as Caddy depends on none of it. `kit backup` prints how long the pause was: measured at 16 to 22 seconds on a 2-CPU server with 12 MB of data, most of it spent waiting for the containers to be healthy again rather than on the copy. Someone who opens `app.` during it gets the web server's plain 502.

Whole folders are copied, not chosen files, which matters more than it looks: each of these databases keeps its recent writes in a `-wal` file beside it, and stopping the container doesn't always fold that back in. Measured on a real server, Forgejo's held 4 MB of writes its database file knew nothing about. A backup of `*.db` alone would lose them without a word.

It also stops the vault's emergency copy if one is running, since that writes to the same data. Start it again with `kit emergency vault` if you still need it.

Old snapshots are forgotten and pruned on each run, keeping 7 daily, 4 weekly and 6 monthly, so the repository doesn't grow for ever.

## Taking a copy off the server

A backup that never leaves the server doesn't survive losing the server. There are two ways, and an association wants at least one of them.

**Pull it, which is the one to prefer.** From any machine with SSH to the server:

```sh
ssh root@<server> 'kit backup export -' > sallskap-backup-$(date +%F).tar
```

That file holds the whole repository, every snapshot in it, and it's already encrypted, so it needs no password of its own. Put the same line in a cron job on a machine the association trusts and the copy happens by itself. Pulling is better than pushing because the credentials then live on the receiving machine: a server that can write to its own off-server copy can also be made to delete it.

`kit backup export <file>` writes it on the server instead, and `kit backup export <folder>` names it by the date.

**Or push it,** for an association with somewhere to put backups but no machine to pull them. Set a restic repository in the config:

```toml
[backup]
target = "sftp:backups@storage.example.org:/sallskap"
```

Every `kit backup` then copies to it as well. The same password is used, so a copy there and an exported file are interchangeable, and a restore can read it directly:

```sh
kit restore --from sftp:backups@storage.example.org:/sallskap
``` Credentials that aren't an SSH key, such as S3 keys, go in `/etc/sallskap/backup.env` as extra lines; restic reads that file as its environment.

`kit status` says when the last backup was taken and when a copy last left the server, and warns when either gets old.

**Keep the copy away from the password.** A machine that holds both is a machine that holds everything.

## Restoring onto a fresh server

What this is for: the server is gone, or a test restore. It needs the exported file and the backup password, and nothing else from the old server.

1. Rent a server that meets §13 of the spec, with the same operating system, and add your SSH key.
2. Run the installer on it, the same line as any other install. It stops and says to set a domain; don't. The restore brings the config with it.
3. Copy the exported file over, and put it back:

   ```sh
   scp sallskap-backup-2026-11-05.tar root@<new server>:/root/
   ssh -t root@<new server> 'kit restore /root/sallskap-backup-2026-11-05.tar'
   ```

   It asks for the password. Type the offline copy. It then says what it's restoring and from when, puts everything back, and starts the whole installation.
4. Point DNS at the new server: `app.`, every module's subdomain, and each public site with its aliases. `kit status` lists what's missing.
5. Check it, below.

`kit restore --from <repository>` reads the backups where they're kept, which is the way back when the push target above is all that's left. `kit restore` on its own, with no file, restores from `/srv/backup` on the server itself — for a mistake rather than a disaster. It refuses to run over a working installation unless you pass `--force`.

It also refuses if the backup was taken with different tool versions from the ones this code pins, because **restoring can only go forwards**: an older Forgejo refuses to start on a database written by a newer one. Install the version the backup was taken with, or pass `--across-versions` if you know this one is newer.

Those are two separate flags on purpose. Rolling back a bad upgrade is the usual reason to restore over a working server, and it is also the usual way to end up running an older tool against a newer database — so `--force` deliberately does not waive the version check as well.

## After a restore

| Check | Why |
|---|---|
| **Remove anyone who has left since the backup was taken** | A restore puts *everyone* back as they were, including people who have left since. They are members again, with working access to every tool, and **nothing will take it away**: core no longer knows they ever left, so the worker sees a member in good standing and keeps their access rather than removing it. No follow-up is opened either, so their shared passwords still need changing. This is the first thing to do after a restore, and the only thing nothing else will catch |
| Open follow-ups on the admin page | Ones that were open when the backup was taken are back, including any that had been dealt with since |
| Everyone can log in, with 2FA | Core's database came back whole |
| Forgejo: a repository, and a push | Its database and its repositories match |
| The vault: an entry decrypts | Each person's own master password still decrypts their own vault |
| The public sites, and their certificates | Caddy's folder came back, so no new certificate was needed |
| `kit status` | DNS, published releases, and that a backup has been taken since the restore |

Three things a restore can't do:

- **It can't know who has left.** Measured on a real restore: someone deleted in core after the backup was taken came back as an active member of their group, and core held no record of the departure. Restoring one tool from an older copy is the opposite case — there core is current, so the worker corrects the tool by itself — but a full restore rolls core back with everything else.

- **It can't bring back a forgotten master password.** Vault entries are encrypted with each person's own, which the server never has. A restored vault is as locked as the original.
- **It can't undo what someone already copied.** Anyone who left before the backup was taken may still hold passwords they saw, and those have to be changed at the site each one belongs to.

## A test restore every semester

Part of the handover checklist in §10.4 of the spec. Restore onto a throwaway server, using the offline copy of the password and nothing else, then delete that server. The copy holds real personal data, so **deleting it afterwards is part of the test.**

It also serves as the rehearsal for an upgrade, since `kit update` takes a backup first and a tool upgrade can't be undone.
