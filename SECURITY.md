# Security

## Reporting a vulnerability

Please don't open a public issue. Report it privately instead: on this repo's **Security** tab, choose **Report a vulnerability**. Only the maintainers can see it.

It helps to include:
- what's affected, such as a `kit` command, core's login or a module;
- how to reproduce it, on a test installation rather than someone's real one;
- what an attacker could do with it.

Sällskap is built by volunteers, so a reply can take a few days. We'll tell you what we found and when a fix is out, and credit you in the release notes unless you'd rather not be named.

## What's covered

The code in this repo: the installer, `kit`, core, the worker and the modules, including how the kit configures the tools it installs.

A problem in one of those tools themselves, such as Forgejo, Vaultwarden, Caddy or Django, belongs with that project. Tell us too if the kit's setup makes it worse.

## Supported versions

Sällskap is in early development and has no releases yet. Fixes go to `main`.
