# TLS certificates — generated locally, never committed

This directory is mounted read-only into Caddy at `/etc/caddy/certs`. It must contain
`local.pem` and `local-key.pem` before `docker compose up` will serve anything.

```bash
mkcert -install
mkcert -cert-file infra/compose/certs/local.pem \
       -key-file  infra/compose/certs/local-key.pem sih26104.local
```

You also need `sih26104.local` to resolve. Add it to your hosts file:

- Linux/macOS: `/etc/hosts`
- Windows: `C:\Windows\System32\drivers\etc\hosts` (edit as Administrator)

```
127.0.0.1 sih26104.local
```

## Why a real certificate and not plain HTTP

`getUserMedia()` requires a secure context. Browsers grant that to `localhost` as a special case, but
`sih26104.local` is not `localhost` — over `http://` the microphone call fails outright, and the PWA
has nothing to capture. Using `localhost` instead would work and is the wrong trade: the deployed tier
is served from a real hostname behind CloudFront, and a local tier that only works on `localhost`
cannot exercise `ALLOWED_ORIGINS`, the `Origin` check, or the CSP `connect-src` — three of the
controls the negative-contract suite asserts.

`mkcert -install` adds a local CA to your trust store. That is a real change to your machine; remove
it with `mkcert -uninstall` when you are finished with the project.

## Never commit these files

`local-key.pem` is a private key. It is scoped to a hostname that resolves nowhere but your machine,
so leaking it is not a breach — but committing it trains the habit that loses a real one, and
`rules.md` R-34 says no secrets in Git, images, or the client, without a "harmless in this case"
exception. `.gitignore` must cover `infra/compose/certs/*.pem`.
