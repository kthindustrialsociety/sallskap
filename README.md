# Sällskap

A free and open-source kit that lets a student association run its own websites, member dashboard and board tools on one small server that it controls. One account works across every tool, and each association turns on only the modules it wants.

Sällskap is built by KTH Industrial Society, which is also its first installation. "Sällskap" is a working title.

**Status: early development, not ready to use.** So far this repo holds the specification, the server installer, the `website` module, the first `kit` commands, and core: Django with accounts, groups, 2FA, an admin page, and the login service for other tools. The other modules aren't built yet.

## Check a server

This changes nothing on the server:

```sh
bash install.sh --check
```

## Layout

| Path | Contents |
|---|---|
| `docs/spec.md` | The specification |
| `install.sh` | The installer, used by every install path |
| `cloud-init.yaml` | Runs the installer on providers with a "user data" field |
| `compose.yml` | Containers for core |
| `kit/` | The `kit` CLI, in progress |
| `core/` | Django: accounts, login and the admin page. Who sees what is set in `core/accounts/roles.py` |
| `modules/website/` | Public sites and redirects |
| `modules/<name>/` | `vault`, `git`, `chat`, `files` (planned) |
| `tests/` | Tests for the kit, and `live_check.py` for a running server |

## Get people in

There's no email yet, so people get in through one-time links that only set a password. They then log in normally, with 2FA.

```sh
kit admin you@example.org      # create the first admin, and print their link
kit link someone@example.org   # a new link for someone an Operator has added
```

If an Operator is locked out, `kit admin` restores their access and clears any login lockouts. If they've lost both their phone and their recovery codes, `kit admin you@example.org --reset-2fa` also removes their 2FA, so they can set it up again.

## Connect a tool

Core is the login service for every other tool, using OpenID Connect. To register a tool:

```sh
kit tool add forgejo https://git.example.org/user/oauth2/core/callback   # prints its settings, and the secret once
kit tool list
kit tool remove forgejo                                                  # ends its access
```

A tool can ask for the scopes `openid`, `email`, `profile` and `groups`, and has to identify people by `sub`. `preferred_username` is unique when it's sent, but can change: it's the part of the email before the @, with the account's id added if an earlier account has the same one. Built-in groups are sent by their fixed key (`board`, `treasurer`, `events`, `developer`, `operator`, `member`), so renaming one can't change what someone can do in a tool. Groups an Operator added are sent as `custom:<name>`. A tool only hears about changes when someone logs in to it, and a silent `prompt=none` check usually gets `consent_required`.

## Tests

```sh
python3 -m unittest discover -s tests     # the kit

docker build --tag sallskap-core:test core     # core, with fake users
docker run --rm --env SALLSKAP_DOMAIN=example.org --env SALLSKAP_DATA_DIR=/tmp \
  --env DJANGO_SECRET_KEY="$(openssl rand -hex 32)" sallskap-core:test python manage.py test
```

`tests/live_check.py` checks a running server from the outside, and creates fake users, so don't run it on an installation with real members.

## License

[AGPL-3.0](LICENSE)
