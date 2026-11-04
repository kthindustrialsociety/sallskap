# git

Code hosting and issues with [Forgejo](https://forgejo.org), at `git.<domain>`. Everyone logs in through core, so one account and one 2FA setup cover it. Someone who knows the association's repos should own this module.

## Turn it on

Add a DNS record for `git.<domain>`, then:

```sh
kit enable git
```

It's safe to run again, which is also how to finish if something fails partway. `kit disable git` stops Forgejo and keeps its data.

## Who gets what

Opening Forgejo signs someone in through core, with nothing to click if they're already logged in there, which creates their Forgejo account at first. Anyone with an account in core can do this. They get no access until a group gives it to them:

| Group | In Forgejo |
|---|---|
| Operator | Forgejo admin, and an owner of the organisation |
| Developer | Can write to all the organisation's repos and create new ones |
| Board | Can read all the organisation's repos |
| Anyone else | Public repos only |

Repos belong to the organisation, which is named after the first part of the domain, so they survive a handover. Ordinary accounts can't create personal repos or other organisations.

## Using Git

Git goes over HTTPS with a token, as Forgejo has no SSH server and no password login. Create a token in Forgejo under Settings, Applications, then use it as the password:

```sh
git clone https://git.example.org/example/some-repo.git
```

## Known limits

- **Signing out of Forgejo doesn't log you out of core.** Opening Forgejo a few minutes later signs you back in. To stay out, log out of core too.
- **Changes arrive at the next sign-in.** Forgejo only hears about someone's groups when they sign in, so a person removed from a group keeps their team, and their tokens keep working, until then. Turning off someone's account in core doesn't reach Forgejo yet either.
- **Some addresses can't sign in.** An address whose part before the @ is a name Forgejo reserves, such as `admin@example.org`, gets an error page. So does one matching `sallskap-admin`, the account that owns the organisation. They can't get into anyone else's account, and they can use another address.
- **Email and name changes aren't passed on.** Forgejo takes them from core when someone first signs in, and keeps them.
- **One token stays behind.** Creating the organisation needs an admin token, and Forgejo 15 can't delete one with password login off. Only a hash of it is stored, and nothing keeps its value.
- **No build runner yet,** so Actions is off.

## Restoring

Everything is in `/srv/git` and `/etc/sallskap/git.env`. Keep them together: the secret key in that file is what makes Forgejo's stored login secret readable. The `kit` backup comes later.

## Upgrading

The version is pinned in `compose.yml`, on Forgejo's long-term support line. An upgrade can't be undone, as an older version refuses to start on a newer database, so a full backup comes first. Stop Forgejo while taking it, so the database and repos match.
