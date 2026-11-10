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

## 1. Download the installer, and read it

Log in to the server as root, from your computer:

```sh
ssh root@203.0.113.10
```

Download the installer from the newest release, with its checksum, and check that they match:

```sh
curl -fsSLO https://github.com/kthindustrialsociety/sallskap/releases/latest/download/install.sh
curl -fsSLO https://github.com/kthindustrialsociety/sallskap/releases/latest/download/SHA256SUMS
sha256sum -c SHA256SUMS
```

It should say `install.sh: OK`. That shows the file arrived whole, exactly as it was released. Both come from the same place, so it doesn't prove more than that.

The installer runs as root, so read it before you run it, or have someone you trust read it:

```sh
less install.sh
```

Many projects install with `curl … | bash`, which runs whatever arrives without anyone seeing it. That works, and is what `cloud-init.yaml` does on providers with a "user data" field, but downloading and reading it first is the careful way.

## 2. Check the server, then install

The checks change nothing:

```sh
bash install.sh --check
```

Each line says `ok`, `warn` or `FAIL`. A `FAIL` says what to choose instead, such as another operating system or a bigger server. Fix it with your provider, then run the check again.

Then install:

```sh
bash install.sh
```

It installs the newest release, and takes a few minutes. Each step ends with an `ok` line:
- the same checks again;
- automatic security updates;
- SSH that only accepts keys, if it finds yours;
- a firewall that only lets in SSH and the web;
- Docker, and the kit;
- a backup every night.

It ends with `Done` and a count of warnings. Read any warnings: each one says what to do. It's safe to run again, which is also how to finish if something fails partway.

**From here on, everything is done with `kit`,** the command the installer just added. Each job is a word after it: `kit init`, `kit admin`, `kit status`, and so on. `kit --help` lists them all, and `kit <command> --help` explains one. The guide never asks you to run Docker or edit files.

## 3. Choose your domain

```sh
kit init example.org
```

This creates the configuration and starts the login service, building it the first time, which takes a minute or two. It then lists the DNS records the server needs, with this server's address. The first time, each one shows as missing.

## 4. Add the DNS records

At your registrar, add an `A` record for each name `kit init` listed, pointing at your server's address. To begin with, that's `app.example.org`. Each module you turn on, and each website you add, lists its own names as well.

**TTL:** leave new records at your registrar's default, usually an hour. If a record already exists and points somewhere else, it can be changed the same way. Computers that looked it up recently keep the old answer for up to its old TTL, so if that matters, lower its TTL to 5 minutes a TTL ahead, change it, then raise it again.

Check whether they've arrived, which can take from minutes to a few hours:

```sh
kit status
```

Each name shows `ok` once it points at this server. A record you've just changed may still show its old address: `kit status` says how long the old answer can last.

**While you wait,** carry on with steps 5 and 6. They print what you need on the server. Only opening the pages needs DNS.

## 5. Create the first admin

```sh
kit admin you@example.org
```

It prints a link that lets you choose a password once, within 24 hours. **Treat it like a password:** whoever opens it first chooses the password for an admin account. Don't paste it into a chat or a ticket.

Once `kit status` shows `app.example.org` as `ok`, open the link, choose a password, then log in at `https://app.example.org`.

You're an **Operator,** the group that runs the installation, so you set up two-factor login before you can use the admin page. The page takes you through it with an authenticator app on your phone. **Keep the recovery codes it shows you somewhere safe,** away from your phone.

For each other person, add them on the admin page, then make a link for them:

```sh
kit link someone@example.org
```

There's no email yet, so you pass the link on yourself, privately.

## 6. Turn on the vault and Git hosting

Each is one command. It lists one more DNS record to add:

```sh
kit enable vault     # passwords, for the board, at vault.example.org
kit enable git       # code and issues, at git.example.org
```

Who gets in, and what they can do, is in [modules/vault/README.md](../modules/vault/README.md) and [modules/git/README.md](../modules/git/README.md). In short: Board and Operator get into the vault, and in Git, groups decide who can read and who can write.

## 7. Add your websites

The server can host any number of static websites, each on its own domain. A site is a folder of files with an `index.html`, built by whatever tool you like, or written by hand.

### Add a site

```sh
kit site add example.org
```

Run it once for each site. For a site on another domain, such as a society's own, run it again with that domain. A bare domain like `example.org` also gets `www.example.org`, sent to the same site. Other domains that should lead to it go on with `--alias`, such as `--alias example.se`.

It lists the site's DNS records. Don't change them yet: publish the site first, below.

It also prints the site's **deploy key**, once.

### What the deploy key is

A deploy key is a key that can publish this one site to the server, and nothing else. It can't log in, open a shell or reach any other site. The server doesn't keep a copy, so nobody can get it back. If it's lost, make a new one, which cancels the old one:

```sh
kit site key example.org
```

**Save it straight into a file,** rather than copying it out of the terminal, where a missed line or an added space spoils it. From your own computer:

```sh
ssh root@203.0.113.10 'kit site key example.org' > example.org-deploy-key && chmod 600 example.org-deploy-key && ssh-keygen -y -f example.org-deploy-key
```

The last part checks the file. It prints a line starting `ssh-ed25519` if the key is whole.

### Publish the site

**By hand,** for a site that rarely changes: from the computer with the key file, send the folder that holds `index.html`:

```sh
tar -C <folder> -czf - . | ssh -o IdentitiesOnly=yes -i example.org-deploy-key sallskap-deploy@203.0.113.10
```

It ends with `Published` and the release's name.

**Automatically,** for a site built from a repository: save the key as a secret in the build, and have it run the same line after building. In GitHub Actions, that's a repository secret under Settings → Secrets and variables → Actions. A build on your own Git hosting comes later. [modules/website/README.md](../modules/website/README.md) has the details.

### Move its DNS

Once it's published, `kit status` lists the site with its release, such as `example.org: 20261110T090908-271474412`. That's the check: the server has the site. Its address can't be opened in a browser yet, as the server only gets the site's certificate once DNS points here.

Then add or change the site's DNS records, as in step 4. For a site that already runs somewhere else, lower its TTL a TTL ahead, so the move is quick. The old server keeps answering until then, so nobody sees a gap.

Undo a publish with `kit site rollback example.org`.

## 8. The backup password

Every backup is encrypted with one password. The first backup made it, probably the nightly one, so show it:

```sh
kit backup password
```

**Write it down on paper, and keep that away from the server.** Nothing in a backup can be read without it, and nothing can recover it.

Then save a second copy in the vault, where the board can find it:
1. Open `https://vault.example.org`. The first time, it asks you to choose a master password. Nobody can recover that one either.
2. Create an organisation, such as "Board", rather than using your personal vault, which nobody else can open once you hand over.
3. Add an entry to it holding the backup password.

The paper copy is the one that counts: the vault is inside the backups, so if the server is lost, the vault copy goes with it. Who should hold it is in [docs/restore.md](restore.md).

`kit status` warns until someone has seen the password with `kit backup password`.

## 9. A copy off the server

A backup that stays on the server doesn't survive losing the server. From a computer the association trusts, not the server, pull a copy:

```sh
ssh root@203.0.113.10 'kit backup export -' > sallskap-backup-$(date +%F).tar
```

The file is encrypted, so it's safe to store. The same line in a scheduled job on that computer makes the copy happen by itself. Keep the copy and the paper with the password apart. Other ways, and how to restore, are in [docs/restore.md](restore.md).

## 10. Check it

```sh
kit status
```

It lists every name and whether its DNS is right, each site's published release, when the last backup and the last copy off the server were taken, whether the backup password has been noted, which release runs, and whether a newer one is out. Nothing should say `warn` or `DNS`. Run it whenever something seems wrong.

## Afterwards

- **Updates:** `kit update` shows what a new release changes and asks before doing anything. It takes a backup first. See [docs/updating.md](updating.md).
- **A test restore every semester,** onto a throwaway server, with the paper password alone. See [docs/restore.md](restore.md).
- **When someone leaves the board:** turn off their account on the admin page, and core takes away their access everywhere. The module READMEs say what's left to do by hand, such as changing shared passwords.
