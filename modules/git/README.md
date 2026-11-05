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

Repos belong to the organisation, which is named after the first part of the domain, so they survive a handover. Ordinary accounts can't create personal repos, forks or other organisations. Addresses come from core, so Forgejo's form for adding one answers with an error.

## When someone leaves

Core tells Forgejo straight away, usually within a few seconds, with no new sign-in needed. If Forgejo is down at the time, it's told as soon as it's back, and the person's page on the admin site says so until then.

| In core | In Forgejo |
|---|---|
| Removed from a group | Off that group's team. Unless they're still a Developer or Operator, they also stop being a collaborator on any repo, including ones they created. Their account and tokens keep working, for public repos |
| Removed from Operator | No longer a Forgejo admin or an owner of the organisation |
| Account turned off, or deleted | Blocked, and off every team. Tokens stop working on the API and Git, and an open web page shows "Account is suspended" whatever it asks for |
| Added to a group, or turned back on | On that group's team straight away. They don't get back as a collaborator, as the team is what gives them access |

Forgejo makes someone a collaborator on a repo they create, as their team only has write access, and they keep admin rights on it until they leave. Git shows someone who's blocked a confusing error, such as `could not determine hash algorithm`, rather than saying they're blocked.

Forgejo's own teams that aren't listed in `module.toml` are left alone, unless someone has left altogether.

## If core is down

Open web pages and tokens keep working, so most people can carry on. Only signing in stops. If an Operator needs in and has no token, run this on the server:

```sh
kit emergency git <their-forgejo-username>
```

It prints a token on their own account, for the API and Git, with no expiry. Delete it under Settings, Applications once core is back, as nothing else can delete it. Forgejo's repos are also plain Git repos under `/srv/git/git/repositories`, which root can copy.

## Using Git

Git goes over HTTPS with a token, as Forgejo has no SSH server and no password login. Create a token in Forgejo under Settings, Applications, then use it as the password:

```sh
git clone https://git.example.org/example/some-repo.git
```

## Known limits

- **Signing out of Forgejo doesn't log you out of core.** Opening Forgejo a few minutes later signs you back in. To stay out, log out of core too.
- **A collaborator added by hand only lasts while they're a Developer or Operator.** Everyone else's collaborations get removed the next time core looks at them, at least once a day, as they can't be told apart from ones Forgejo made when someone created a repo. Give other people access with a team.
- **Webhooks and mirrors outlive whoever set them up.** Someone with admin rights on a repo could have set one up to send its contents elsewhere, and removing them doesn't remove that. Check a sensitive repo's settings when someone leaves on bad terms.
- **Some addresses can't sign in.** An address whose part before the @ is a name Forgejo reserves, such as `admin@example.org`, gets an error page. So does one matching `sallskap-admin`, the account that owns the organisation, or the organisation's own name, or a name Forgejo ends up treating as the same as someone else's. They can't get into anyone else's account, and they can use another address.
- **Email and name changes aren't passed on.** Forgejo takes them from core when someone first signs in, and keeps them.
- **One admin token is kept,** on `sallskap-admin`, in `/etc/sallskap/sync.env`. Only the worker that tells Forgejo about changes has it, and only root can read that file. Forgejo 15 can't delete a token with password login off, so replacing it would leave the old one working.
- **No build runner yet,** so Actions is off.

## Restoring

Everything is in `/srv/git`, `/etc/sallskap/git.env` and `/etc/sallskap/sync.env`. Keep them together: the secret key in `git.env` is what makes Forgejo's stored login secret readable. The `kit` backup comes later.

When the worker starts, it goes over everyone, so someone who left after the backup was taken is cut off again by itself.

## Upgrading

The version is pinned in `compose.yml`, on Forgejo's long-term support line. An upgrade can't be undone, as an older version refuses to start on a newer database, so a full backup comes first. Stop Forgejo while taking it, so the database and repos match.
