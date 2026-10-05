# Deploying ftbfs on a server

How to run ftbfs as a service: the web UI reachable from the internet
with Launchpad login, a daily run, snapshots, with builds in LXD worker
containers on other hosts. The reference setup is an LXD VM, but any
Ubuntu machine works the same way. For day-to-day running see
`OPERATIONS.md`; for what is exposed and why, `../SECURITY.md`.

```
 browser --https--> tunnel or TLS proxy --http--> 127.0.0.1:8047
                                                  ftbfs-web (ftbfs serve)
                                                  ftbfs-daily.timer
                                                  /srv/ftbfs: state/ work/ cache/
                                                        |
                                    lxc (TLS, trusted) -+-> buildhost: ftbfs-buildhost-1..n
                                                        +-> another host: ...
```

The first deployment (an Ubuntu 26.04 VM, 2026-10-05) followed these
steps with a Cloudflare Tunnel; the Caddy alternative in step 6 was not
used there.

## 1. The machine

Builds run on the LXD hosts, so the server only needs room for the web
UI, the agents and the state: 2 vCPU and 4 GB of memory are enough.
Size the disk for `work/` and `cache/` (about 4 GB after a month of
runs on the default selection) with ample margin:

```sh
lxc launch ubuntu:26.04 <host>:ftbfs --vm -c limits.cpu=2 \
  -c limits.memory=4GiB -d root,size=40GiB
lxc exec <host>:ftbfs -- bash
```

Inside, as root:

```sh
apt-get install -y dpkg-dev devscripts quilt ubuntu-dev-tools git \
  sqlite3 rsync
snap install astral-uv --classic     # uv
snap install opencode --classic      # the agent backend
snap install lxd                     # only for the lxc client
useradd --system --create-home --home-dir /home/ftbfs \
  --shell /bin/bash ftbfs            # no sudo
install -d -o ftbfs -g ftbfs /srv/ftbfs
```

`/tmp` is a memory-backed tmpfs on cloud images: keep large copies on
disk (`/var/tmp`, `/srv`).

## 2. The code

As `ftbfs` (`su - ftbfs`):

```sh
git clone https://github.com/mclemenceau/ubuntu-agentic-ftbfs /srv/ftbfs
cd /srv/ftbfs && uv sync --frozen
```

To deploy a release rather than `main`, `git checkout v<X.Y.Z>` before
`uv sync --frozen`.

## 3. Configuration and keys

`/srv/ftbfs/config.local.toml`, from `config.local.toml.example`:

```toml
[identity]                 # signs the dev stage's changelog entries
name = "Your Name"
email = "you@example.org"

[backend.opencode]
api_keys = { openrouter = "~/.config/ftbfs/openrouter.key" }

[builders.buildhost]       # one table per LXD host (step 4)
kind = "lxd"
remote = "buildhost"
slots = 2
parallel = 4

[web]
allowed_hosts = ["127.0.0.1", "localhost", "::1", "ftbfs.example.org"]
public_url = "https://ftbfs.example.org"
session_secret_file = "~/.config/ftbfs/session.key"

[web.auth]
provider = "launchpad"
consumer_key = "ftbfs"

[web.roles]
operator = ["your-launchpad-name"]
```

The key files, as `ftbfs`, mode 600:

```sh
install -d -m 700 ~/.config/ftbfs
install -m 600 /dev/null ~/.config/ftbfs/openrouter.key
"$EDITOR" ~/.config/ftbfs/openrouter.key      # the key, one line
install -m 600 /dev/null ~/.config/ftbfs/session.key
head -c 32 /dev/urandom | base64 > ~/.config/ftbfs/session.key
```

Give the server its own session secret: a copy from another instance
would accept that instance's cookies. Prefer a dedicated OpenRouter key
with a credit limit (`OPERATIONS.md`, "Dedicated OpenRouter key"):
`[web] daily_cost_cap` only caps runs started from the web UI, so the
daily timer's runs are bounded by the key's limit alone.

## 4. Build hosts

Each LXD host must trust the server's `lxc` client. On a machine that
can already manage the host, create a token:

```sh
lxc config trust add <host>: --name ftbfs-server   # prints a token
```

and on the server, as `ftbfs`:

```sh
lxc remote add buildhost <host address> --token <token>
uv run ftbfs builders          # reachable, image, workers
```

A trusted client can manage **every** instance on that host, not only
the workers: the server's account is as powerful as an LXD admin there.
Keep the server's other access tight accordingly (`../SECURITY.md`).

A host whose LXD only served local clients needs to listen first, on
its LAN address: `lxc config set core.https_address <address>:8443`.
A host that is sometimes away (a laptop) is fine: its builder is marked
down and builds go to the others. Give it a fixed address (a DHCP
reservation), since the remote is added by address.

Builder names decide the worker containers' names
(`ftbfs-<builder>-<n>`), and the slot locks live in each instance's
`state/builders/`. Two instances using the same builder names on the
same host would share containers without knowing it: never run two
instances with the same `[builders.*]` names. If there is no image on
the hosts yet, `uv run ftbfs builders image` builds it.

## 5. Services

The units ship in `deploy/`. As root:

```sh
cd /srv/ftbfs/deploy
for u in ftbfs-web.service ftbfs-daily.service ftbfs-daily.timer; do
  ln -sf /srv/ftbfs/deploy/$u /etc/systemd/system/$u
done
systemctl daemon-reload
systemctl enable --now ftbfs-web.service ftbfs-daily.timer
```

- `ftbfs-web`: `ftbfs serve` on 127.0.0.1:8047, restarted on failure.
  Runs started from the UI are detached processes in its cgroup, so the
  unit stops only the server (`KillMode=process`): restarting the UI
  leaves a run going.
- `ftbfs-daily.timer`: `ftbfs run --ingest` at 05:00 UTC. systemd never
  starts it twice at once; a run started from the web UI meanwhile is
  refused by ftbfs itself.

Logs go to the journal: `journalctl -u ftbfs-web`, `journalctl -u
ftbfs-daily`. A run's own log is also in `state/runs/`.

Check: `curl -s -o /dev/null -w '%{http_code}\n' -H 'Host:
ftbfs.example.org' http://127.0.0.1:8047/` prints 200, and an anonymous
POST is refused:
`curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'HX-Request: true'
-H 'Host: ftbfs.example.org' -H 'Origin: https://ftbfs.example.org'
http://127.0.0.1:8047/runs/start` prints 401.

## 6. Reaching it from the internet

`ftbfs serve` speaks plain HTTP on loopback; something else brings it
to the network with TLS. Whatever it is must keep the browser's `Host`
header, which the UI checks against `allowed_hosts` and the `Origin` of
every POST.

**Cloudflare Tunnel** (the reference setup): no open port and no
certificate to manage on the server; `cloudflared` only connects out.
In the Cloudflare dashboard (Zero Trust, Networks, Tunnels), create a
tunnel with the public hostname `ftbfs.example.org` pointing at
`http://localhost:8047`, then on the server run the install commands
it shows, ending with `cloudflared service install <token>`. It keeps
the `Host` header by default.

**Caddy** (if the server has a public address): `apt install caddy`,
and `/etc/caddy/Caddyfile`:

```
ftbfs.example.org {
    reverse_proxy 127.0.0.1:8047
}
```

Caddy gets the certificate itself and keeps the `Host` header.

Then, from another machine: open `https://ftbfs.example.org`, log in
with Launchpad, and check that your actions are recorded as
`web:<your login>` on the gate and the timeline.

## 7. Backups

The server's state is the VM itself: let LXD snapshot it on a schedule,
on the host:

```sh
lxc config set <host>:ftbfs snapshots.schedule="30 3 * * *" \
  snapshots.expiry=14d snapshots.pattern=auto-%d
lxc info <host>:ftbfs          # Snapshots: the list
```

A snapshot of the running VM is crash-consistent, which SQLite in WAL
mode recovers from by design, and on zfs or btrfs it is instant and
only grows with the changes. Pick the time away from the daily run. The
snapshots live in the host's storage pool: they cover a broken VM or a
bad update, not the loss of the host's disk. For that, copy a snapshot
elsewhere now and then (`lxc export <host>:ftbfs --instance-only` or
`lxc copy` to another host).

**Restore.** Roll the whole VM back, code and config included:

```sh
lxc restore <host>:ftbfs auto-<n>
```

or, to look at an old state without touching the server, start a copy,
with no tunnel on it (a copy of `cloudflared` would serve the public
name too) and gone before its daily timer fires:

```sh
lxc copy <host>:ftbfs/auto-<n> <host>:ftbfs-restore
lxc start <host>:ftbfs-restore
lxc exec <host>:ftbfs-restore -- systemctl disable --now cloudflared \
  ftbfs-daily.timer
...
lxc delete -f <host>:ftbfs-restore
```

On the first deployment, a copy started from a snapshot showed the same
`ftbfs status` and planned the same work as the server.

On a machine that is not an LXD instance, back up `state/ftbfs.db` with
`sqlite3 state/ftbfs.db ".backup <file>"` (safe during a run), plus
`state/snapshots/`, `state/opencode/` and `work/`; `cache/` refills
itself. Restoring into another directory needs `ftbfs relocate`
(step 8).

## 8. Moving an existing instance

To move an instance that has been running elsewhere, and keep its
verdicts, verified fixes and cost history:

1. On the old machine: no run in progress (`ftbfs status`; `ftbfs
   control pause` and wait if needed), then stop its web UI and
   whatever schedules its runs. From here on it must not run: it uses
   the same builder names (step 4).
2. Copy the database consistently, and the files, keeping symlinks:
   ```sh
   sqlite3 OLD/state/ftbfs.db ".backup /var/tmp/ftbfs.db"
   lxc file push /var/tmp/ftbfs.db <host>:ftbfs/srv/ftbfs/state/ftbfs.db
   tar -C OLD -cf - work cache state/snapshots state/opencode \
     | lxc exec <host>:ftbfs -- tar -C /srv/ftbfs --no-same-owner -xf -
   lxc exec <host>:ftbfs -- chown -R ftbfs:ftbfs /srv/ftbfs
   ```
3. On the server, as `ftbfs`, before starting the services:
   `uv run ftbfs relocate --dry-run OLD`, then `uv run ftbfs relocate
   OLD`. Results store absolute paths that are part of the cache keys;
   relocate rewrites them without re-running anything (`OPERATIONS.md`,
   "Moving an instance").
4. `ftbfs status` and the dry-run preview on the server must match the
   old machine's.

## 9. Updating and rolling back

Between runs (`ftbfs status`: none running; the daily one starts at
05:00 UTC), take a snapshot first (`lxc snapshot <host>:ftbfs
pre-v<X.Y.Z>`), then as `ftbfs`:

```sh
cd /srv/ftbfs
git fetch --tags && git checkout v<X.Y.Z>    # or: git pull --ff-only
uv sync --frozen
```

and as root, `systemctl daemon-reload` (the units in `deploy/` may have
changed) and `systemctl restart ftbfs-web`.

The daily service starts a fresh process each time, so it needs no
restart. Read the release notes first: a change to `pipeline.toml`, a
prompt, `rules.toml` or a stage version re-runs that stage and what
follows it, which can cost tokens.

Roll back with `git checkout <previous tag>`, `uv sync --frozen` and a
restart. The database schema only migrates forward: if the newer
release migrated it, restore the snapshot taken before the update
instead (step 7).
