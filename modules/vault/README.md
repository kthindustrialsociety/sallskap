# vault

A password vault with [Vaultwarden](https://github.com/dani-garcia/vaultwarden), at `vault.<domain>`. People get in through core, so one account and one 2FA setup cover it, and each person also has a master password. Someone the board trusts with its passwords should own this module.

## Turn it on

Add a DNS record for `vault.<domain>`, then:

```sh
kit enable vault
```

It's safe to run again, which is also how to finish if something fails partway. `kit disable vault` stops Vaultwarden and keeps its data.

## Who gets in

Only Board and Operator, which are set in `module.toml`. Both require 2FA in core, so every login to the vault has passed it. Core shows anyone else a "No access" page.

To change the groups or the session length, edit `module.toml`, then:

```sh
kit tool remove vaultwarden
kit enable vault
```

That signs everyone out of the vault at their next page load, and changes nothing else.

## Logging in

Opening `vault.<domain>` while logged in to core asks for nothing but the master password. Someone who isn't logged in to core does that first.

The first time, they choose a master password. The page calls this joining an organisation, but it makes a personal vault. **Vaultwarden can't recover a lost master password,** as it's what decrypts the vault, and nobody else holds it. So anything the association can't lose belongs in a shared organisation with at least two owners, and has an offline copy.

## Sharing passwords

Shared passwords live in an organisation. Groups in core decide who can sign in, but not who's in an organisation, as Vaultwarden has no way of taking that from core. With no mail, an owner adds each person by hand:

1. They sign in once and choose a master password.
2. An owner adds their address under the organisation's members, and they're accepted right away.
3. The owner confirms them, which gives them the organisation's key.

## When someone leaves

Turning off someone's account in core, deleting it, or removing them from Board and Operator all do the same thing. Core tells Vaultwarden straight away, usually within a few seconds, and it turns off their account here. If it's down at the time, it's told as soon as it's back.

That ends every token at once, including one used by hand, and an open page logs itself out. It also refuses their master password, so the emergency copy below won't let them in either. Adding them back turns the account on again, and they sign in as usual.

**Their personal entries are locked too,** as nothing here can tell them apart. So before someone hands over, they should copy out anything of their own. If that gets missed, add them back to a group for long enough to do it.

**What only a person can do.** Someone who has left may have copied or kept the passwords they saw. Vaultwarden's server can't read entries, and they're passwords to other sites anyway, so these can't be changed for you. If they were in any organisations, Board and Operators get a follow-up on the admin page for each one, until someone marks it done:

1. An owner of the organisation removes them from its members.
2. The owner changes every password they could see, at the site each one belongs to, then updates the entry.
3. Someone marks the follow-up as done, which records who and when.

If they were an organisation's only owner, the follow-up says so, as nobody can manage it after that. Add them back to a group for long enough to make someone else an owner.

## If core is down

The public address can't be signed in to until core returns, and the next page load signs people out. They lose nothing. An Operator can still get in, with SSH on the server and their master password:

```sh
kit emergency vault
```

This starts a private copy of Vaultwarden on the same data, which takes master passwords. It publishes no ports, so nothing changes at the public address, which still refuses them. It prints the tunnel to run on your own computer, and then you open `https://localhost:8222`. Your browser warns about its certificate, which was made just then for this, and that's expected, as the tunnel is what protects it. Changes you make there are real. An account that's turned off stays off.

Stop it when you're done:

```sh
kit emergency vault --off
```

Only an Operator with SSH can do this, and it doesn't ask for 2FA, as SSH stands in for that. A Board member without SSH has to ask them.

**If the whole server is gone, none of this helps.** Keep an offline copy of what you'd need to fix it, such as the provider's login and the SSH key, and of the backup keys. Export the organisation's vault for this now and then, with a strong password of its own, and store it somewhere that isn't this server.

## Known limits

- **A session lasts 12 hours, whatever someone does.** It can't be extended, and after 11 hours 55 minutes the next page load signs them out, so they log in again.
- **The admin page is on, and closed to the internet by Caddy.** Only the worker that tells Vaultwarden about changes has its token, and Vaultwarden itself only has a hash of it. An upgrade should be checked to make sure the page's address hasn't moved, which `tests/live_check.py` does.
- **The worker reads a web page to find people.** Vaultwarden's data doesn't say who's linked to which core account, only its admin page does, and addresses don't do, as it keeps an old one. If an upgrade changes that page, nobody gets cut off until this is fixed, and the admin page says so.
- **Logging out of the vault doesn't log you out of core.** To stay out on a shared computer, log out of core too. For 12 hours after first opening the vault, logging out stays that way, and it asks for an address to sign in again. After that, the first log out signs you straight back in, as far as its lock screen.
- **Email changes in core aren't passed on.** Accounts follow core's id, so the vault stays theirs, and Vaultwarden keeps showing the old address.
- **A new core account with an old address can't get in.** It's a different person as far as Vaultwarden is concerned, so it refuses them rather than handing over the old vault. Someone who loses their core account and gets a new one needs the old Vaultwarden account removed, and nothing does that yet. The old account is turned off when the old core account goes.
- **No command-line client.** The Bitwarden CLI logs in with a personal API key, and those would get around core and 2FA, so nobody can create one.
- **Extensions and apps aren't tested.** Only the web page has been, and only it can start signing in by itself.
- **Turned off, as they need mail or would get around core:** emergency access, password hints, public share links and invitations to people who haven't signed in. It also doesn't fetch icons for saved sites.

## Restoring

Everything is in `/srv/vault`, `/etc/sallskap/vault.env` and `/etc/sallskap/sync.env`. Keep them together, and stop Vaultwarden while copying its database. The vaults are encrypted with each person's master password, so a restore doesn't bring back anything someone has lost theirs for. The `kit` backup comes later.

When the worker starts, it goes over everyone, including accounts deleted in core, so someone who left after the backup was taken is turned off again by itself. The passwords they could see still need changing, and no follow-up says so, as it's already been dealt with once as far as core knows.

## Upgrading

The version is pinned in `compose.yml`. Upgrade promptly, as it's a password vault and fixes arrive as ordinary releases, and take a backup first. An upgrade should be checked in a browser, as its web page comes with it and `tests/live_check.py` doesn't use one. `modules/vault/site.caddy` relies on two things that a new version could change: the identifier in its redirect, which the live check tests, and the routes it closes. `connector.py` relies on the layout of its admin page, which the live check tests too.
