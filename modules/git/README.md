# git

Code hosting and issues with [Forgejo](https://forgejo.org), at `git.<domain>`. Everyone logs in through core, so one account and one 2FA setup cover it. Someone who knows the association's repos should own this module.

## Turn it on

Add a DNS record for `git.<domain>`, then:

```sh
kit enable git
```

It's safe to run again, which is also how to finish if something fails partway. `kit disable git` stops Forgejo and keeps its data.

## Who gets what

Opening Forgejo signs someone in through core, with nothing to click if they're already logged in there, which creates their Forgejo account at first. Anyone with an account in core can do this. What they reach comes from their memberships and positions, as keys ([docs/structure.md](../../docs/structure.md) §5):

| In core | In Forgejo |
|---|---|
| Operator | Forgejo admin, and an owner of your association's organisation |
| The chair or vice chair of an association | An owner of its organisation |
| A board role on your association (`board`) | Reads every repo in your association's organisation |
| A member of an association, directly or through one inside it | On its organisation's `members` team, which reads the repos opened to members |
| A team's lead | On the team, and on `<team>.lead`, which administers the team's repos and creates new ones |
| A team's participant | On the team, which writes to the team's repos |
| Anyone else | Public repos only |

- **Organisations:** your association's is the one named after the first part of the domain. Every association inside it gets its own, named by its key, the first time someone needs it. Repos belong to an organisation, so they survive a handover. Ordinary accounts can't create personal repos, forks or organisations.
- **Teams:** a team's Forgejo teams are in the organisation of the association that owns it. That's so for teams inside teams, a team other associations take part in, and a team run for another association. Core makes them the first time someone needs them, and never deletes one.
- **A new repo is only its creator's at first.** Forgejo makes whoever creates it, a team's lead, its admin. Give it to your team, and to `<team>.lead` so the next lead can manage it, under the repo's Settings, Collaborators. To let your association's members read it, give it to `members` too. Until then, only you, the board and the organisation's owners can see it.
- **Archiving a team** takes its people off its Forgejo teams within seconds, and its repos stay where they are. Moving a team to another association moves its people to that association's organisation. Its repos stay put, and an owner can transfer them in Forgejo.
- **What core keeps in step:** your association's organisation, with its Owners and board teams, and the organisations and teams core made, whose description starts "Kept in step with core". Someone added to or removed from one of those by hand is put back as core says. Other teams are left alone, unless someone has left altogether. If a team or organisation made by hand already has the name core needs, core leaves it alone, and the person's page on the admin site says why until it's renamed.
- **A unit whose key Forgejo already uses** in the organisation (owners, members or board) gets a team called `<key>.team`.
- **Someone signing in for the first time** gets the teams beyond Owners and board a few seconds after, when core tells Forgejo about them.

## When someone leaves

Core tells Forgejo straight away, usually within a few seconds, with no new sign-in needed. If Forgejo is down at the time, it's told as soon as it's back, and the person's page on the admin site says so until then.

| In core | In Forgejo |
|---|---|
| A position or membership ends, or its team is archived | Off the teams it gave. They stay a collaborator on repos they created only while they hold a team in that organisation. Their account and tokens keep working, for public repos |
| Removed from Operator | No longer a Forgejo admin or an owner of your association's organisation |
| Account turned off, or deleted | Blocked, and off every team in core's organisations. Tokens stop working on the API and Git, and an open web page shows "Account is suspended" whatever it asks for |
| A new position or membership, or the account turned back on | On its teams straight away. They don't get back as a collaborator, as the team is what gives them access. If the role requires 2FA, this waits until they've set it up |

Forgejo makes someone a collaborator on a repo they create, and they keep admin rights on it while they hold a team in its organisation. Git shows someone who's blocked a confusing error, such as `could not determine hash algorithm`, rather than saying they're blocked.

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
- **A collaborator added by hand only lasts while they hold a team in that organisation, or are an Operator.** Everyone else's collaborations get removed the next time core looks at them, at least once a day, as they can't be told apart from ones Forgejo made when someone created a repo. Give other people access with a team.
- **Webhooks and mirrors outlive whoever set them up.** Someone with admin rights on a repo could have set one up to send its contents elsewhere, and removing them doesn't remove that. Check a sensitive repo's settings when someone leaves on bad terms.
- **Some addresses can't sign in.** An address whose part before the @ is a name Forgejo reserves, such as `admin@example.org`, gets an error page. So does one matching `sallskap-admin`, the account that owns the organisations, or an organisation's name, or a name Forgejo ends up treating as the same as someone else's. They can't get into anyone else's account, and they can use another address.
- **An association can't get an organisation named after someone.** Organisations and people share names in Forgejo, so if someone's username is an association's key, or the key is a name Forgejo keeps for itself, such as `explore` or `api`, the organisation isn't made. The admin page says so on the page of each person who needs it. An Operator can free the name by renaming that account in Forgejo's site administration, as an association's key never changes.
- **Email and name changes aren't passed on.** Forgejo takes them from core when someone first signs in, and keeps them.
- **One admin token is kept,** on `sallskap-admin`, in `/etc/sallskap/sync.env`. Only the worker that tells Forgejo about changes has it, and only root can read that file. Forgejo 15 can't delete a token with password login off, so replacing it would leave the old one working.
- **No build runner yet,** so Actions is off.

## Restoring

`kit backup` and `kit restore` cover this module along with everything else: see [docs/restore.md](../../docs/restore.md). It backs up `/srv/git` together with `/etc/sallskap`, which is what matters here — the secret key in `git.env` is what makes Forgejo's stored login secret readable, and `sync.env` holds the admin token the worker uses. Forgejo is stopped while the copy is taken, as its database runs in WAL mode and has to match the repositories.

If you put **only this module's data** back, while core is current, the worker sorts it out: it goes over everyone when it starts, including accounts deleted in core, so an account its backup brings back is cut off again by itself.

A **full restore is different**, because core is rolled back too. Everyone comes back as they were when the backup was taken, including people who have left since, and core no longer knows they left. Remove them by hand: see [docs/restore.md](../../docs/restore.md).

Restoring can only go forwards: an older Forgejo refuses to start on a database written by a newer one, so `kit restore` stops if the versions don't match.

## Upgrading

The version is pinned in `compose.yml`, on Forgejo's long-term support line. An upgrade can't be undone, as an older version refuses to start on a newer database, so a full backup comes first. Stop Forgejo while taking it, so the database and repos match.
