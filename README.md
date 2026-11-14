# Sällskap

A free and open-source kit that lets a student association run its own websites, member dashboard and board tools on one small server that it controls. One account works across every tool, and each association turns on only the modules it wants.

Sällskap is built by KTH Industrial Society, which is also its first installation. "Sällskap" is a working title.

**Status: early development, not ready to use.** So far this repo holds the specification, the server installer, the `website`, `git` and `vault` modules, the first `kit` commands, and core: Django with accounts, groups, 2FA, an admin page, and the login service for other tools. The other modules aren't built yet.

## Install

[docs/installing.md](docs/installing.md) goes from renting a server to logins, the vault, Git hosting and your website, with backups and a copy off the server. In short, as root on a fresh Debian or Ubuntu server, with the installer from the newest release:

```sh
curl -fsSLO https://github.com/kthindustrialsociety/sallskap/releases/latest/download/install.sh
less install.sh            # it runs as root, so read it first
bash install.sh --check    # changes nothing
bash install.sh
kit init example.org --name "Example Society" --key example
```

## Layout

| Path | Contents |
|---|---|
| `docs/spec.md` | The specification |
| `docs/structure.md` | Associations, teams, people and their roles, and the member CLI: the design, being built. Units are in core |
| `docs/installing.md` | Installing, step by step |
| `docs/restore.md` | What a backup holds, how to take a copy off the server, and how to restore |
| `docs/updating.md` | What `kit update` does |
| `install.sh` | The installer, used by every install path |
| `cloud-init.yaml` | Runs the installer on providers with a "user data" field |
| `compose.yml` | Containers for core, and the worker that tells tools about changes to people's access |
| `kit/` | The `kit` CLI, in progress |
| `systemd/` | The timer that takes a backup every night |
| `core/` | Django: accounts, login, the tree of associations and teams (`core/structure/`), and the admin page. Who sees what is set in `core/accounts/roles.py` |
| `modules/website/` | Public sites and redirects |
| `modules/git/` | Forgejo, with login through core |
| `modules/vault/` | Vaultwarden, with login through core |
| `modules/<name>/` | `chat`, `files` (planned) |
| `tests/` | Tests for the kit, `live_check.py` for a running server, and `load_example.py` for a test server |

## Get people in

There's no email yet, so people get in through one-time links that only set a password. They then log in normally, with 2FA.

```sh
kit admin you@example.org      # create the first admin, and print their link
kit link someone@example.org   # a new link for someone an Operator has added
```

If an Operator is locked out, `kit admin` restores their access and clears any login lockouts. If they've lost both their phone and their recovery codes, `kit admin you@example.org --reset-2fa` also removes their 2FA, so they can set it up again.

## Turn on a module

```sh
kit enable git     # safe to run again, which is also how to finish after a failure
kit disable git    # stops it and keeps its data
kit emergency vault  # get into a tool while core is down: see its README
```

## Backups

A backup is taken every night, encrypted. Everything else is in [docs/restore.md](docs/restore.md).

```sh
kit backup                                     # take one now
ssh root@<server> 'kit backup export -' > backup.tar   # take a copy off the server
kit restore backup.tar                         # put it back, on a fresh server
```

**Keep an offline copy of the backup password.** The other copy is in the vault, which is inside the backup, so if the server is gone the offline copy is the only one left. `kit backup` prints the password when it makes it, the first time it runs.

## Updates

```sh
kit update             # back up, then move to the newest release. It shows what changes and asks first
kit update --rollback  # go back to before the last update
```

What it does and what it checks is in [docs/updating.md](docs/updating.md). What each release changes is in [CHANGELOG.md](CHANGELOG.md).

## Connect a tool

Core is the login service for every other tool, using OpenID Connect. A module does this for itself. To register anything else:

```sh
kit tool add mytool https://tool.example.org/callback   # prints its settings, and the secret once
kit tool add mytool https://tool.example.org/callback --group board --group operator   # only these groups can log in
kit tool list
kit tool remove mytool                                  # ends its access
```

A tool can ask for the scopes `openid`, `email`, `profile` and `groups`, and has to identify people by `sub`. `preferred_username` is the part of the email before the @, with the account's id added if an earlier account has the same one. That covers the usual clash, but it isn't guaranteed unique, and it can change. Built-in groups are sent by their fixed key (`board`, `treasurer`, `events`, `developer`, `operator`, `member`), so renaming one can't change what someone can do in a tool. Groups an Operator added are sent as `custom:<name>`. A tool only hears about changes when someone logs in to it, and a silent `prompt=none` check usually gets `consent_required`.

## Tests

```sh
python3 -m unittest discover -s tests     # the kit. The backup tests need restic installed

docker build --tag sallskap-core:test core     # core, with fake users
docker run --rm --env SALLSKAP_DOMAIN=example.org --env SALLSKAP_DATA_DIR=/tmp \
  --env DJANGO_SECRET_KEY="$(openssl rand -hex 32)" sallskap-core:test python manage.py test
```

`tests/live_check.py` checks a running server from the outside, and creates fake users, so don't run it on an installation with real members.

Core's tests, and test servers, use two made-up installations, defined in `core/structure/examples.py`: **Example Society**, one association with teams and projects, and **Example Federation**, with associations inside it. Every feature has to work for both. `tests/load_example.py` loads one onto a test server, under that server's own association, to check by hand in the admin page:

```sh
python3 tests/load_example.py society test.example.org               # the server's primary domain, to confirm
python3 tests/load_example.py federation test.example.org --replace  # deletes every unit under the association first
```

## License

[AGPL-3.0](LICENSE)
