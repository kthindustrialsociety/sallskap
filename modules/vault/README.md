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

## Known limits

- **A session lasts 12 hours, whatever someone does.** It can't be extended, and after 11 hours 55 minutes the next page load signs them out, so they log in again.
- **Removing someone reaches them at their next page load, not at once.** Each page load and unlock asks core, and core refuses anyone who has left the groups or been turned off. Until then, an open page keeps what it has already decrypted. Someone using their token by hand keeps API access until the session ends, as Vaultwarden only asks core when refreshing.
- **Removing someone doesn't remove what they've seen.** They may have copied or cached passwords, so change the shared ones too, and remove them from the organisation, as that isn't connected to core.
- **Logging out of the vault doesn't log you out of core.** To stay out on a shared computer, log out of core too. For 12 hours after first opening the vault, logging out stays that way, and it asks for an address to sign in again. After that, the first log out signs you straight back in, as far as its lock screen.
- **If core is down, the vault is out of reach.** The next page load signs people out, and they can't sign in until core returns. They lose nothing, and getting back in works as usual. Keep an offline copy of what you'd need to fix the server.
- **Email changes in core aren't passed on.** Accounts follow core's id, so the vault stays theirs, and Vaultwarden keeps showing the old address.
- **A new core account with an old address can't get in.** It's a different person as far as Vaultwarden is concerned, so it refuses them rather than handing over the old vault. Someone who loses their core account and gets a new one needs the old Vaultwarden account removed, and nothing does that yet.
- **No command-line client.** The Bitwarden CLI logs in with a personal API key, and those would get around core and 2FA, so nobody can create one.
- **Extensions and apps aren't tested.** Only the web page has been, and only it can start signing in by itself.
- **Turned off, as they need mail or would get around core:** emergency access, password hints, public share links and invitations to people who haven't signed in. It also doesn't fetch icons for saved sites.

## Restoring

Everything is in `/srv/vault` and `/etc/sallskap/vault.env`. Keep them together, and stop Vaultwarden while copying its database. The vaults are encrypted with each person's master password, so a restore doesn't bring back anything someone has lost theirs for. The `kit` backup comes later.

## Upgrading

The version is pinned in `compose.yml`. Upgrade promptly, as it's a password vault and fixes arrive as ordinary releases, and take a backup first. An upgrade should be checked in a browser, as its web page comes with it and `tests/live_check.py` doesn't use one. `modules/vault/site.caddy` relies on two things that a new version could change: the identifier in its redirect, which the live check tests, and the routes it closes.
