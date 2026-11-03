# Sällskap

A free and open-source kit that lets a student association run its own websites, member dashboard and board tools on one small server that it controls. One account works across every tool, and each association turns on only the modules it wants.

Sällskap is built by KTH Industrial Society, which is also its first installation. "Sällskap" is a working title.

**Status: early development, not ready to use.** So far this repo holds the specification, the server installer, the `website` module, the first `kit` commands, and core: Django with accounts, groups, 2FA and an admin page. Login for other tools and the other modules aren't built yet.

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

## Tests

```sh
python3 -m unittest discover -s tests                                     # the kit
docker compose run --rm --no-deps core python manage.py test              # core, with fake users
```

`tests/live_check.py` checks a running server from the outside, and creates fake users, so don't run it on an installation with real members.

## License

[AGPL-3.0](LICENSE)
