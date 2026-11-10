# Installing Sällskap

This takes an association from no server to logins, a password vault, Git hosting and its public website, all on a server it controls. You don't need to know Linux: you copy commands, and the kit says what to do next. Plan for about an hour, plus however long your domain registrar takes to update DNS.

Throughout, `example.org` stands for your association's domain, and `203.0.113.10` stands for your server's address. Use your own wherever these appear.

## What you need

- **A domain** your association controls, and a login to the registrar where you can add DNS records.
- **A server** from any provider. The kit checks it, so you don't need to ask the provider how it works:
  - a full virtual machine (often called a VPS or KVM), not a container;
  - Debian 12 or 13, or Ubuntu 24.04 or 26.04;
  - at least 2 GB of RAM, and 4 GB if you want Git hosting and the vault;
  - at least 15 GB of free disk;
  - a public IPv4 address.
- **An SSH key** on your own computer, added to the server when you order it. Without one, the server stays open to password guessing. If you have no key, run `ssh-keygen -t ed25519` on your computer and add the `.pub` file it makes.
- **Two people,** ideally. One is enough to install, but someone else should know how to get in if you can't.

## 1. Check the server

Log in to the server as root, from your computer:

```sh
ssh root@203.0.113.10
```

Then run the checks. They change nothing:

```sh
curl -fsSL https://raw.githubusercontent.com/kthindustrialsociety/sallskap/main/install.sh | bash -s -- --check
```

Each line says `ok`, `warn` or `FAIL`. A `FAIL` says what to choose instead, such as another operating system or a bigger server. Fix it with your provider, then run the check again.

## 2. Install

```sh
curl -fsSL https://raw.githubusercontent.com/kthindustrialsociety/sallskap/main/install.sh | bash
```

It installs the newest release, and takes a few minutes. It:
- runs the same checks again;
- turns on automatic security updates;
- allows only SSH keys, if it finds yours;
- opens only the ports the kit needs: SSH, 80 and 443;
- installs Docker and the kit;
- schedules a backup every night.

It ends with `Done` and a count of warnings. Read any warnings: each one says what to do. It's safe to run again, which is also how to finish if something fails partway.

If your provider has a "user data" or cloud-init field when you order the server, `cloud-init.yaml` in this repository runs the same install by itself.

## 3. Choose your domain

```sh
kit init example.org
```

This creates the configuration and starts the login service. It then lists the DNS records the server needs, with this server's address. The first time, each one shows as missing.

## 4. Add the DNS records

At your registrar, add an `A` record for each name `kit init` listed, pointing at your server's address. To begin with, that's `app.example.org`. Each module you turn on, and each website you add, lists its own names as well.

Check whether they've arrived, which can take from minutes to a few hours:

```sh
kit status
```

Each name shows `ok` once it points at this server. Until it does, the browser can't reach that name securely, as the server can't get a certificate for it yet.

## 5. Create the first admin

```sh
kit admin you@example.org
```

It prints a link that lets you choose a password once, within 24 hours. Open it, choose a password, then log in at `https://app.example.org`.

You're an **Operator,** the group that runs the installation, so you have to set up two-factor login before you can use the admin page. The page takes you through it with an authenticator app on your phone. **Keep the recovery codes it shows you somewhere safe,** away from your phone.

For each other person, add them on the admin page, then make a link for them:

```sh
kit link someone@example.org
```

There's no email yet, so you pass the link on yourself.

## 6. Turn on the vault and Git hosting

Each is one command. It lists one more DNS record to add:

```sh
kit enable vault     # passwords, for the board, at vault.example.org
kit enable git       # code and issues, at git.example.org
```

Who gets in, and what they can do, is in [modules/vault/README.md](../modules/vault/README.md) and [modules/git/README.md](../modules/git/README.md). In short: Board and Operator get into the vault, and in Git, groups decide who can read and who can write.

## 7. Add your website

```sh
kit site add example.org
```

It lists the DNS records for the site, and prints a **deploy key** once. Save the key straight away as a secret in whatever builds your site, such as GitHub Actions. The server doesn't keep a copy. Publishing is then one line from that build:

```sh
tar -C dist -czf - . | ssh -o IdentitiesOnly=yes -i deploy_key sallskap-deploy@203.0.113.10
```

Other domains that should lead to the same site go on with `--alias`. All of this is in [modules/website/README.md](../modules/website/README.md).

To move an existing site without it going dark: publish it here first, check it with `kit status`, and only then move its DNS records.

## 8. The backup password

Take the first backup by hand, so you see its password:

```sh
kit backup
```

It prints the backup password the first time. **Write it down now, and keep it away from the server.** Put a second copy in the vault. Without this password, nothing in a backup can be read, and nothing can recover it. The vault copy alone isn't enough, because the vault is inside the backup. Why, and who should hold it, is in [docs/restore.md](restore.md).

## 9. A copy off the server

A backup that stays on the server doesn't survive losing the server. From a computer the association trusts, pull a copy:

```sh
ssh root@203.0.113.10 'kit backup export -' > sallskap-backup-$(date +%F).tar
```

The file is encrypted, so it's safe to store. The same line in a scheduled job on that computer makes the copy happen by itself. Keep the copy and the password apart. Other ways, and how to restore, are in [docs/restore.md](restore.md).

## 10. Check it

```sh
kit status
```

It lists every name and whether its DNS is right, when the last backup and the last copy off the server were taken, which release runs, and whether a newer one is out. Run it whenever something seems wrong.

## Afterwards

- **Updates:** `kit update` shows what a new release changes and asks before doing anything. It takes a backup first. See [docs/updating.md](updating.md).
- **A test restore every semester,** onto a throwaway server, with the written-down password alone. See [docs/restore.md](restore.md).
- **When someone leaves the board:** turn off their account on the admin page, and core takes away their access everywhere. The module READMEs say what's left to do by hand, such as changing shared passwords.
