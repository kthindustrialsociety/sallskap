# Updating

`kit update` moves an installation to a newer release. It takes a backup first, and that backup is the only way back: an older Forgejo refuses to start on a database a newer one has written, so tools can't be downgraded in place.

```sh
kit update            # the newest release. It shows what changes and asks first
kit update 0.7.1      # a particular release
kit update --rollback # back to before the last update, from the backup it took
```

## What it does

1. **Checks, and changes nothing yet:**
   - it runs as root;
   - every container is healthy now, so an old problem isn't blamed on the update;
   - there's room for the new images and a backup;
   - no backup is running.
2. **Picks the release:**
   - the newest `v*` tag, or the one you name;
   - an older release, the same one, or a branch is refused;
   - so is a tag whose `VERSION` doesn't match its name, or whose changelog section is incomplete.
3. **Shows what to read first:**
   - each release's "Before you update" from [CHANGELOG.md](../CHANGELOG.md);
   - every tool version that changes;
   - for a new Forgejo, a link to its release notes and a reminder to check your own CSS against them.

   Then it asks. `--yes` answers for you.
4. **Prepares the new version while everything still runs:** it pulls the new tool images and builds core's new image, `sallskap-core:<version>`. If this fails, nothing has changed.
5. **Takes a backup:**
   - Forgejo finishes its queued work first;
   - logins and the tools stop, and stay stopped until the new version is up, so nothing is written that a rollback would lose;
   - the public sites stay up throughout;
   - if the backup fails, everything starts again on the old version.
6. **Switches the code,** and hands over to the new release's own `kit`. It starts everything, and core updates its database as it starts.
7. **Checks the new version:**
   - every container is healthy;
   - Forgejo's own `forgejo doctor check`, with every check but two that always report errors here: `gc-lfs`, as LFS is unused, and `authorized-keys`, as its SSH server is off. Forgejo exits 0 even when a check reports an error, so kit reads the output. Its warnings are shown, but don't fail the update;
   - the worker reaches each tool the way it expects, including the layout of Vaultwarden's admin page that it reads;
   - core's login page answers through Caddy.
8. **If a check fails,** it stops and leaves everything as it is for you to look at. It says what failed and prints the one command that goes back: `kit update --rollback`.
9. **If every check passes,** it removes older images of the kit's own tools, keeping the ones from before this update, and clears Docker's build cache. Images that aren't the kit's are left alone.

Logins are usually down for a minute or two, from the backup until the checks.

## Rolling back

`kit update --rollback`:
- checks out the code from before the update;
- has that code restore the backup taken just before it, with `kit restore --force --snapshot <id>`;
- starts everything on the old images, which are still on the server.

Anything written since that backup is lost. After an update that passed its checks, that may include real work, so it asks first.

The folders the restore replaces are moved aside, not deleted, and their new names are printed. Delete them once you're sure.

## A test server

`kit update --unreleased <branch or commit>` moves to something that isn't a release, with a warning. It's for test servers only: it has no changelog section, and nothing checks its `VERSION`.
