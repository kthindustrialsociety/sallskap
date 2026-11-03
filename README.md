# Sällskap

A free and open-source kit that lets a student association run its own websites, member dashboard and board tools on one small server that it controls. One account works across every tool, and each association turns on only the modules it wants.

Sällskap is built by KTH Industrial Society, which is also its first installation. "Sällskap" is a working title.

**Status: early development, not ready to use.** So far this repo holds the specification, the server installer, the `website` module and the first `kit` commands. These have been tested on a real Debian 13 server. Core (Django, accounts and login) and the other modules aren't built yet.

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
| `core/` | Django: accounts, login and dashboard (planned) |
| `modules/website/` | Public sites and redirects |
| `modules/<name>/` | `vault`, `git`, `chat`, `files` (planned) |
| `tests/` | Run with `python3 -m unittest discover -s tests` |

## License

[AGPL-3.0](LICENSE)
