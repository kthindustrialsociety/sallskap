# website

Serves an association's public sites and redirects its other domains. Any static site works, whichever tool builds it. The full behaviour is in `docs/spec.md` §7.5.

**Who should own it:** whoever is responsible for the association's public sites.

## Add a site

```sh
kit site add example.org --alias example.se
```

This updates the config and the web server, then shows the DNS records to add and a deploy key. The key is shown once and isn't kept on the server, so store it as a secret in your CI system straight away. If it's lost, `kit site key example.org` replaces it.

## Publish from CI

Build the site, then send the output folder over SSH:

```sh
tar -C dist -czf - . | ssh -i deploy_key sallskap-deploy@your-server
```

The key can only publish to its own site. It can't open a shell or reach anything else on the server.

A publish must contain `index.html` at its top level, and it may only contain plain files and folders. It goes live all at once, and if anything is wrong, the running site stays as it was.

## Undo a publish

```sh
kit site rollback example.org
```

The last five releases are kept. Publishing again moves the site to the new release.

## Remove a site

```sh
kit site remove example.org
```

This removes the site from the config, revokes its key and stops serving it. Its files stay in `/srv/website/example.org/` until you delete them.

## Restore

Back up and restore `/srv/website`. Certificates in `/srv/caddy` are renewed automatically, so they don't need restoring.
