# Known issues & host prerequisites

Running notes from testing `bootstrap/install.sh` on a real box, kept separate from
[`ARCHITECTURE.md`](architecture/ARCHITECTURE.md) because this is host-specific troubleshooting,
not design. First target: RHEL/Rocky/AlmaLinux 9. Add to this as new hosts turn up new gotchas.

## Still on you — host-level decisions a script shouldn't make for you

### `postgres-backup.yaml`'s ObjectStore endpoint — confirm it after SeaweedFS syncs

Added 2026-08-31 alongside SeaweedFS/Postgres backup wiring. `manifests/postgres-backup.yaml`'s
`ObjectStore.spec.configuration.endpointURL` ships with a best-guess Service DNS name for
SeaweedFS's S3 gateway (`storage-seaweedfs-s3.storage-seaweedfs.svc.cluster.local:8333`) — the
Helm chart's exact generated Service name depends on its `_helpers.tpl` fullname template, which
wasn't worth vendoring the whole chart locally just to confirm ahead of a real sync. Same class of
"can't be known until it actually runs on your cluster" as MetalLB's pool below, not a bug.

**Fix:** once `apps/optional/storage-seaweedfs` has synced, confirm the real name —

```bash
kubectl -n storage-seaweedfs get svc
```

— and update `endpointURL` in both `manifests/postgres-backup.yaml` (the `ObjectStore`) and the
`S3_ENDPOINT` env var on the bucket-creation `Job` in that same file if it differs from the
placeholder. Until this is confirmed, `postgres-backup` and `postgres-cluster`'s WAL archiving will
sit Degraded/erroring — same "commit a TODO'd default, Argo CD shows it degraded until addressed"
pattern as `metallb-pool.yaml`/`cluster-issuer.yaml` below, and expected on a fresh install.

### MetalLB's IP pool is network-specific — update it on any new host/network

`src/core/argocd/manifests/metallb-pool.yaml` is committed with a real, concrete IP range
(currently `192.168.4.240-192.168.4.250`, homelab-dev's eero LAN), not a placeholder. Unlike
`REPO_URL`/`REVISION`, there's no way for `install.sh` to derive "which slice of this network is
safe to hand out" on its own — that always needs a human check (see the ping-loop approach below).

This means the committed value is only correct for the network it was set up on. Run this repo
against different hardware or a different network — new server, router swap, a cloud VM — and it
silently carries over the old range. MetalLB's `L2Advertisement` mode ARPs on the local subnet, so
a mismatched pool doesn't just risk a conflict, it doesn't work at all: ingress-nginx's external IP
just sits on `<pending>` forever, with nothing pointing at "wrong subnet" as the cause.

`install.sh` now checks this automatically — it compares the pool's subnet against the host's
detected subnet and warns (doesn't block) on a mismatch. If you see that warning, or you're setting
this up fresh on new hardware, find a safe range with a quick reachability check rather than
guessing at your router's DHCP boundary:

```bash
for i in 240 241 242 243 244 245; do ping -c1 -W1 192.168.X.$i >/dev/null && echo "192.168.X.$i is IN USE" || echo "192.168.X.$i looks free"; done
```

(swap `192.168.X` for your actual subnet — check any device's own IP to find it, no router login
needed). Update `metallb-pool.yaml` with whatever range comes back clean.

### firewalld

**What happened:** k3s installed and the `k3s` service ran, but the node sat `NotReady`
indefinitely — no crash, no clear error, just stuck. `firewalld` (on by default on RHEL 9) was
blocking traffic k3s/Flannel need. `sudo systemctl stop firewalld` unblocked it immediately.

**Status right now:** only *stopped*, not disabled — it'll come back on the next reboot and block
things again. Pick one:

- **Disable it.** Reasonable for a homelab box sitting behind your own router, not directly
  internet-facing:

  ```bash
  sudo systemctl disable firewalld
  ```

- **Keep it, add rules instead.** Better if this box is more exposed (a cloud VM, a shared
  network) — open exactly what k3s needs rather than turning the firewall off. Per
  [k3s's own port requirements](https://docs.k3s.io/installation/requirements), a single-node
  server needs:

  | Port | Protocol | For |
  |---|---|---|
  | 6443 | TCP | K3s supervisor / Kubernetes API server |
  | 10250 | TCP | Kubelet metrics and API |
  | 8472 | UDP | Flannel VXLAN — k3s's own docs say this one specifically should never be exposed to the open internet |

  ```bash
  sudo firewall-cmd --permanent --add-port=6443/tcp
  sudo firewall-cmd --permanent --add-port=10250/tcp
  sudo firewall-cmd --permanent --add-port=8472/udp
  sudo firewall-cmd --reload
  ```

  **This list will grow.** It only covers k3s itself — once ingress-nginx and MetalLB are synced
  you'll want 80/443 open too, and other core pieces may need their own ports. Treat this as a
  starting point, not the final list, and update it here as new pieces come online.

### SELinux

**Status:** `Enforcing` on this box, and — once the stale pgdg repo below stopped interfering with
`k3s-selinux`'s install — it turned out *not* to be the actual blocker. Worth a standing check
anyway, since it's a common RHEL-family gotcha even when it isn't the cause:

```bash
getenforce                      # confirm current mode
rpm -q k3s-selinux               # should be installed
sudo journalctl -u k3s -n 200 --no-pager | grep -i denied   # any AVC denials logged?
```

If something does fail with a denial later: `sudo setenforce 0` temporarily confirms SELinux is
the cause, but isn't a real fix — the actual fix is whatever policy/context is missing, not leaving
the box permissive.

### Stale/conflicting yum repos

**What happened:** an earlier manual attempt at installing PostgreSQL 13 directly on the host (via
the PGDG yum repo) left a dead `pgdg13` repo registered in `/etc/yum.repos.d/` even after that
attempt was abandoned. `dnf`/`yum` refreshes metadata for *every* enabled repo on any transaction —
so `install.sh` triggering k3s's installer to pull in `k3s-selinux` failed on a repo with nothing
to do with k3s or Postgres, because that repo had gone `410 Gone` (PG13 hit end-of-life in November
2025). Fixed by removing it:

```bash
sudo rm /etc/yum.repos.d/pgdg*.repo
sudo dnf clean all
```

No longer needed at all now that Postgres runs via CloudNativePG inside the cluster
(`src/core/argocd/apps/postgres-operator.yaml`), not as a host package.

**General lesson:** if `install.sh` fails on a seemingly unrelated repo/package error, check
`dnf repolist all` for anything stale or broken before assuming the script itself is wrong.

### Private repo credentials for Argo CD

**What happened:** the deploy key used to `git clone` this repo onto the host only authenticates
the host's own git client — it does nothing for Argo CD's `repo-server` pod, which has no
credentials of its own by default. First sync of `root` failed with `error creating SSH agent:
SSH agent requested but SSH_AUTH_SOCK not-specified`.

**Status:** fixed at the script level. `bootstrap/install.sh` now takes `--repo-ssh-key <path>` and
registers that key as a repository credential Secret in the `argocd` namespace automatically.
Because that Secret lives in the `argocd` namespace, `bootstrap/teardown.sh` deletes it along with
everything else — pass `--repo-ssh-key` again on every fresh `install.sh` run against a private
repo, same as any other flag. No need to hand-create the Secret anymore.

### `teardown.sh` hanging forever on `kubectl delete application root`

**What happened:** `root` (only `root` — see `src/core/argocd/root-app.yaml`) carries the
cascade-delete finalizer `resources-finalizer.argocd.argoproj.io`, so Argo CD tears down
everything it manages before the object actually finalizes. In testing, that cascade stalled —
most likely tangled up with the crash-looping `postgres-operator` pod from the CRD-size issue
above, which couldn't finish processing one of its own custom resources' finalizers. The
`kubectl delete application root` line had no timeout, so it blocked the whole script silently,
forever, with zero output — no error, no progress, nothing to Ctrl-C into except an unresponsive
terminal.

**Status:** fixed at the script level (see changelog below) — bounded timeouts on every delete in
this stage, and if `root`'s cascade specifically doesn't finish in 90s, the script now forces it
through by stripping the finalizer directly, on the reasoning that `k3s-uninstall.sh` right after
wipes the whole node regardless, so there's nothing to lose by not waiting out a stuck cascade.

If you're ever stuck on an older copy of this script: find the hung process (`ps aux | grep
kubectl`), `kill` it and the parent `teardown.sh`/`sudo bash` processes, then run
`kubectl -n argocd patch application root --type=merge -p '{"metadata":{"finalizers":null}}'`
by hand before continuing with `kubectl delete namespace argocd` and `k3s-uninstall.sh` yourself.

### `__REPO_URL__`/`__REVISION__` placeholders left un-substituted in `apps/*.yaml`

**What happened:** `postgres-cluster`, `cert-manager-issuers`, `metallb-config`, and
`keycloak-instance` — the four `Application` manifests that source `manifests/*.yaml` from this
same repo, rather than an external chart — still had the literal strings `__REPO_URL__` and
`__REVISION__` as their `repoURL`/`targetRevision`. `bootstrap/install.sh`'s `sed` substitution
only ever runs against `root-app.yaml`, the one file it applies directly; everything under
`apps/` is read straight from git by Argo CD itself, with no templating step of its own. All four
sat stuck at `Unknown` sync status indefinitely — no crash, no clear top-level error, just silently
never resolving, since `kubectl get applications` doesn't surface the underlying comparison error
without an explicit `-o jsonpath='{.status.conditions}'` lookup.

**Status:** fixed by hardcoding real values directly in each manifest (see
`src/core/argocd/README.md`'s "Self-referencing apps" section for the full reasoning and the
tradeoff this creates — these four won't automatically follow `root` if you ever bootstrap from a
branch other than `dev`).

### `sealed-secrets`'s chart repo 404ing

**What happened:** `https://bitnami-labs.github.io/sealed-secrets/index.yaml` started returning
`404 Not Found`. The project's GitHub org renamed from `bitnami-labs` to `bitnami` at some point
after this manifest was first written, and the old Pages URL didn't redirect.

**Status:** fixed — `apps/sealed-secrets.yaml` now points at `https://bitnami.github.io/sealed-secrets`,
version bumped to the current `2.19.3` (appVersion `0.39.1`) since the old `2.16.1` pin predated the
move. Re-verified on 2026-08-30 that this is still the free, independently-maintained project, not
Bitnami's paywalled general catalog.

### Manual `sudo kubectl`/`sudo k3s-...` commands need the full path

**Reminder, not a script bug:** the `PATH` fix in `bootstrap/lib/common.sh` only helps commands run
*inside* the bootstrap scripts. Typing `sudo kubectl ...` or `sudo k3s-uninstall.sh` directly in your
own shell is a fresh `sudo` invocation, and `sudo` resolves commands via its own `secure_path`, not
your shell's `$PATH` — which is why this same `/usr/local/bin` gap bites again for any manual
command. Either type the full path (`sudo /usr/local/bin/kubectl ...`), or fix it once at the host
level via `sudo visudo` — find the `Defaults secure_path = ...` line and add `/usr/local/bin` to it.

### node-exporter's default port (9100) can collide with a pre-existing host exporter

**What happened:** `monitoring.yaml`'s `kube-prometheus-stack` bundles `prometheus-node-exporter`,
which runs with `hostNetwork: true` — it binds directly to the host's port 9100, not just a
container port. On a box already running its own native `node_exporter` (feeding a separate,
pre-existing Prometheus setup), that's an immediate, permanent `CrashLoopBackOff`:
`listen tcp 0.0.0.0:9100: bind: address already in use`.

**Status:** worked around, not a script fix — this is a legitimate per-host conflict, not a bug.
`monitoring.yaml` now overrides the chart's `prometheus-node-exporter.service.port`/`targetPort` to
`9101` instead of touching whatever's already on `9100`. Confirmed against the chart's own
`ServiceMonitor` template that Prometheus discovers this port by name (`metrics`), not the number,
so scraping keeps working automatically with no other change needed. If you hit the same collision
on a different host, check what's already bound first (`ss -ltnp | grep 9100`) before assuming which
side should move.

**Reminder if you change a synced Application's Helm values:** Argo CD only sees what's pushed to
git — running `kubectl annotate ... refresh=hard` (or any resync/restart) *before* the commit
actually reaches the branch it's tracking just re-applies the same old config. If a fix doesn't seem
to take effect, check the live resource directly (e.g. `kubectl get svc ... -o jsonpath='{.spec.ports}'`)
to confirm what's actually deployed before assuming the fix itself is wrong.

### Keycloak's pod stuck unhealthy after a fresh install — two separate bugs, not one

**What happened:** `platform-0` sat unhealthy for the first ~40 minutes of a fresh install
(2026-08-30), showing two *different* failures in sequence — worth recording as two bugs, not one,
since fixing the first one just uncovered the second:

1. `Warning FailedMount ... secret "keycloak-tls" not found`. `spec.http.tlsSecret: keycloak-tls`
   is Keycloak's own internal HTTPS listener cert, required for the pod to start at all — this is
   completely independent of `spec.ingress.enabled`. An earlier comment in this repo assumed the
   hostname TODO "wasn't blocking anything right now" because ingress was disabled; that was true
   for external routing, not for this field. Nothing in the scaffold created that Secret.
2. Once that was fixed, the pod moved to `CreateContainerConfigError` /
   `Error: secret "platform-postgres-app" not found` — see the next entry; this is really the same
   root cause as the namespace problem below, just surfacing as a container start failure here.

**Status:** both fixed.

- `keycloak-tls`: `manifests/keycloak-instance.yaml` now includes a `cert-manager.io/v1 Certificate`
  for `keycloak-tls`, issued off the `platform-ca` `ClusterIssuer` (see `manifests/cluster-issuer.yaml`).
- The Postgres credential: see "Postgres is a shared core service, but its auto-generated credential
  can't leave its namespace" below.

### Postgres is a shared core service, but its auto-generated credential can't leave its namespace

**What happened:** `postgres-cluster.yaml`'s `bootstrap.initdb` (deliberately, see that file's
comments) has no `secret:` field, so CNPG auto-generates the `keycloak` role's password itself, into
a Secret named `platform-postgres-app` — **in the `postgres` namespace**, because that's where the
`Cluster` resource lives. Kubernetes Secrets can't cross namespaces, and Keycloak's CR lives in the
`keycloak` namespace, so its `db.usernameSecret`/`passwordSecret` references could never resolve.
This wasn't a fluke of timing — it was never going to work as written, and it'll happen again for
any *future* service that wants to use this same shared Postgres cluster from its own namespace.

Checked whether CNPG has a supported way to annotate that auto-generated secret for a mirroring tool
to pick up (e.g. via `inheritedMetadata`) before reaching for a bigger fix — confirmed via a
CloudNativePG GitHub discussion that it doesn't: "no direct mechanism for annotating CNPG's
auto-generated app/superuser secrets."
([cloudnative-pg/cloudnative-pg#3653](https://github.com/cloudnative-pg/cloudnative-pg/discussions/3653))

**Status:** fixed generally, not just for Keycloak — this will recur for every future service that
wants this Postgres cluster, so it's solved once at the platform level rather than patched per-app:

- `apps/reflector.yaml` deploys [kubernetes-reflector](https://github.com/emberstack/kubernetes-reflector)
  (wave 0, core tier) — a small controller that mirrors an annotated Secret into other namespaces and
  keeps it in sync.
- `bootstrap/install.sh` (step 2d) generates one real credential — `platform-postgres-keycloak-credentials`,
  a plain `kubernetes.io/basic-auth` Secret in the `postgres` namespace, **never committed to git** —
  idempotently (only the first time; re-running the script doesn't rotate an existing credential out
  from under a running cluster), and annotates it for Reflector's auto-mirror mode
  (`reflection-auto-namespaces: keycloak`).
- `manifests/postgres-cluster.yaml`'s `Cluster` now has a `managed.roles` entry for `keycloak` pointing
  at that Secret — CNPG reconciles the role's actual database password to match it on an ongoing
  basis, which works whether the cluster is brand new or (like the one this was fixed against)
  already past its initial bootstrap.
- `manifests/keycloak-instance.yaml`'s `db.usernameSecret`/`passwordSecret` now point at
  `platform-postgres-keycloak-credentials` instead of the CNPG-auto-generated one.

**Loose end, harmless:** CNPG's own auto-generated `platform-postgres-app` Secret in the `postgres`
namespace is now unused — nothing references it anymore, and its password value is stale (CNPG
doesn't delete it after bootstrap). Left in place rather than cleaned up; not worth the extra
complexity for a Secret nothing reads.

**To apply this on an already-running cluster:** push, then either re-run `bootstrap/install.sh`
(every earlier step is idempotent and no-ops on what's already there) or run step 2d's commands by
hand, then force-refresh `reflector`, `postgres-cluster`, and `keycloak-instance`:
`kubectl -n argocd annotate application reflector postgres-cluster keycloak-instance argocd.argoproj.io/refresh=hard --overwrite`.

**One more step this actually needed, worth remembering for next time:** after all of the above
synced clean (`keycloak-instance` Synced/Healthy, correct secret name confirmed in both the live
`Keycloak` CR's `spec.db` and the `StatefulSet`'s pod template env vars), `platform-0` itself *still*
didn't restart — same pod object, same 88-minute-old failure, still erroring on the old secret name.
The Keycloak operator's `StatefulSet` uses `updateStrategy: OnDelete` — the operator deliberately
controls pod rollout timing itself rather than letting Kubernetes replace pods automatically the
moment the template changes, so an already-running pod keeps its stale spec until something actually
deletes it. Fix was `kubectl -n keycloak delete pod platform-0` — safe here since it had never
started successfully, so there was no session/data state on it to lose. **Any future change to
`keycloak-instance.yaml`'s `Keycloak` CR will hit this same gap** — check `spec.updateStrategy.type`
on the `platform` `StatefulSet` if a CR change syncs clean but the pod doesn't visibly react, and
delete the pod by hand if it's `OnDelete`.

### CNPG's operator only discovers the Barman Cloud plugin at its own startup

**What happened (2026-09-01):** `postgres-backup-plugin`, `postgres-backup`, and `storage-seaweedfs`
all synced Healthy — the `barman-cloud` pod in `cnpg-system` was `Running`, the S3 bucket existed
(confirmed via its own Job's logs), the `ObjectStore` existed — and the first `ScheduledBackup` run
still failed immediately: `requested plugin is not available: barman-cloud.cloudnative-pg.io`.
Every piece this repo is responsible for was correctly in place; the CNPG operator itself just
didn't know the plugin existed yet. Matching reports on the plugin's own repo
([#196](https://github.com/cloudnative-pg/plugin-barman-cloud/issues/196),
[#660](https://github.com/cloudnative-pg/plugin-barman-cloud/issues/660)) point at the actual
mechanism: the operator enumerates available CNPG-I plugins once, at its own process startup — it
doesn't keep watching for new plugin Services to appear afterward. Our `postgres-operator` pod had
already been running for hours (from an earlier, unrelated install step) by the time
`postgres-backup-plugin` first synced, so it never saw the new `barman-cloud` Service come up. (The
*other* cause reported in that thread — Cluster/ObjectStore/Secret split across different
namespaces — isn't what happened here; this repo keeps all three in `postgres` on purpose, see
`manifests/postgres-backup.yaml`.)

**Status:** not a bug in anything this repo controls — a one-time gap on whichever install first
brings the plugin up, same shape as the Keycloak `OnDelete` gotcha above (a controller that doesn't
react to a change automatically the first time). Originally left as a manual step on the theory
that `install.sh` hands off to Argo CD and returns immediately, well before a truly fresh cluster's
plugin would even exist — but that reasoning didn't hold up: Argo CD's wave ordering *guarantees*
`postgres-operator` (wave 0) is already healthy before `postgres-backup-plugin` (wave 1) is even
created, which makes "operator starts before the plugin exists" the reliable, predictable case on
every fresh bring-up, not a coin flip. `install.sh` now waits for the plugin (bounded, 5 minutes)
and restarts the operator automatically as its final step — see its own step 5 comments — skipping
the wait entirely on a cluster where `cnpg-system` already existed (nothing new to discover) or with
`--skip-k3s` (an existing external cluster, left to you). Manual fix below still applies to the case
automation can't cover — adding this capability to an already-running cluster whose operator has
been up for a while, exactly what happened during testing on 2026-09-01 before this was automated:

```bash
kubectl -n cnpg-system rollout restart deployment postgres-operator-cloudnative-pg
kubectl -n cnpg-system rollout status deployment postgres-operator-cloudnative-pg
```

Then either wait for the next scheduled run or trigger one by hand to confirm:

```bash
kubectl apply -f - <<'EOF'
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: platform-postgres-manual-test
  namespace: postgres
spec:
  cluster:
    name: platform-postgres
  method: plugin
  pluginConfiguration:
    name: barman-cloud.cloudnative-pg.io
EOF
kubectl -n postgres get backups -w
```

Safe to restart the operator any time — it briefly pauses reconciliation, it doesn't touch already-
running Postgres pods. **Only needed once per cluster**, right after `postgres-backup-plugin` first
goes healthy; the operator remembers the plugin across its own future restarts/upgrades from then on.

### The first (`immediate: true`) backup can fail on a fresh install — SeaweedFS's own startup race

**What happened (2026-09-01), on a fully fresh install with the operator-restart fix above already
in place:** `postgres-backup`'s `ScheduledBackup` still produced one `failed` `Backup` before its
very next attempt (2 seconds later, by coincidence of timing) succeeded, and every WAL archive since
has been clean. The `barman-cloud` sidecar's own logs (`kubectl -n postgres logs platform-postgres-1
-c plugin-barman-cloud`, NOT the central `barman-cloud` Deployment in `cnpg-system` — that one only
logs the plugin's pod-patching lifecycle hooks, not actual backup/archive activity) showed the real
cause: SeaweedFS's S3 gateway returned a genuine `InternalError` on `CreateBucket` for about a
minute — not "bucket doesn't exist," an actual internal error — meaning SeaweedFS's own
master/volume/filer components hadn't finished electing/stabilizing yet, even though its pods
already reported `Running`. One second after that internal error stopped, WAL archiving started
working and hasn't failed since.

**Status:** benign and self-healing, not a defect — this is "the destination isn't fully warmed up
in its first minute of existence," a fundamentally different race than the bucket-doesn't-exist-yet
one `manifests/postgres-backup.yaml`'s bucket-creation Job already retries around. `ScheduledBackup`
has no automatic retry for a `Backup` that already reached `failed` — only the *next* trigger (the
real daily 02:00 schedule, or a manual one-off) gets a fresh attempt — which is exactly why this one
resolved itself within seconds here rather than needing any intervention. Deliberately NOT
engineered away (e.g. by dropping `immediate: true` or adding an artificial startup delay): the
point of `immediate: true` is not waiting until 02:00 for your first backup, one cosmetic `failed`
entry costs nothing functionally, and CNPG's own `ContinuousArchiving`/`LastBackupSucceeded`
conditions (`kubectl -n postgres get cluster platform-postgres -o jsonpath='{.status.conditions}'`)
tell you the real, current state regardless of what any one historical `Backup` object says. If a
fresh install's `ScheduledBackup` fails immediately, check whether it's this — SeaweedFS pods
`Running` for well under a couple of minutes — before assuming something is actually broken.

### Checking whether backups/WAL archiving are actually working: don't trust `Cluster.status.firstRecoverabilityPoint`

**What happened (2026-09-01):** with backups and WAL archiving both genuinely healthy (confirmed via
the `Cluster`'s own `ContinuousArchiving`/`LastBackupSucceeded` conditions, both `True`), both
`kubectl -n postgres get cluster platform-postgres -o jsonpath='{.status.firstRecoverabilityPoint}'`
and the newer per-method `{.status.firstRecoverabilityPointByMethod}` came back completely empty —
looking like backups weren't really landing anywhere, despite every other signal saying they were.

**Status:** not a bug, and not specific to this repo — a known reporting gap in how the CNPG core
operator surfaces plugin-based backup state, matching
[cloudnative-pg/cloudnative-pg#8276](https://github.com/cloudnative-pg/cloudnative-pg/issues/8276)
(filed against `kubectl cnpg status`, but the same gap shows up querying the raw `Cluster` status
directly). With the plugin architecture, this data isn't copied up into `Cluster.status` — it lives
on the `ObjectStore` resource itself instead:

```bash
kubectl -n postgres get objectstore platform-postgres-backup-store -o jsonpath='{.status.serverRecoveryWindow}'
```

That's the field that's actually authoritative for a plugin-backed cluster —
`firstRecoverabilityPoint`, `lastSuccessfulBackupTime`, and `lastFailedBackupTime` all live there,
confirmed accurate against real backup activity during testing. Check this (or the `Cluster`'s
`ContinuousArchiving`/`LastBackupSucceeded` conditions) instead of `Cluster.status.
firstRecoverabilityPoint` on this repo's Postgres setup, now and until CNPG's core operator starts
populating the top-level field for plugin-based backups too.

### Reaching Keycloak (or anything with a pinned `hostname`) by raw IP breaks after the first page

**What happened (2026-08-31):** port-forwarded to `platform-service` and connected fine via
`https://<homelab-IP>:8443` — the login page loaded — but everything broke immediately after
(submitting the login form, loading the admin console). `keycloak-instance.yaml`'s `Keycloak` CR
pins `spec.hostname.hostname: keycloak.platform.local`, and Keycloak's hostname provider enforces
that value strictly for every URL it generates once you're past the first static page — form
actions, redirects, the admin console's own asset URLs all point at `keycloak.platform.local`
regardless of what address the browser actually used to connect. A browser that can't resolve that
name has no way to follow them.

**Status:** worked around, not a bug — this is Keycloak (and `hostname`-pinned services generally)
behaving as configured, not a defect. Fix: make the configured hostname actually resolve, rather
than trying to bypass it. On whichever machine's browser you're using, add a line to its hosts file
— `C:\Windows\System32\drivers\etc\hosts` on Windows (Notepad "Run as administrator" to save it),
`/etc/hosts` on Linux/macOS — pointing the configured hostname at wherever you're actually reaching
the service:

```text
192.168.4.129 keycloak.platform.local
```

Then browse to `https://keycloak.platform.local:8443`, not the raw IP. This is a temporary,
per-machine, testing-only mapping — it doesn't affect anything else at that IP (hosts entries only
add name→IP resolution, they don't restrict or remove existing IP-based access), and it only
applies on the one machine whose hosts file you edited. Once `platform-gateway` and a real
`Ingress` exist, `keycloak.platform.local` is meant to resolve to ingress-nginx's IP
(`192.168.4.240`, from `metallb-pool.yaml`) instead of the homelab box's own address — update or
remove this entry at that point rather than leaving it pointed at the wrong place.

### `catalog-service`'s auth was a placeholder — closed at the application layer, still open at the network layer

Added 2026-09-01, Phase 2 kickoff; updated 2026-09-01 (same day) when role enforcement landed;
**updated again 2026-09-02 (platform-gateway-auth branch) — the application-layer half of this gap
is now closed, read on for what's still open.**

`src/core/catalog-service/app/deps.py`'s `get_current_principal` still reads three plain headers
(`X-Workspace`, `X-User`, `X-Role`) with no verification of its own — but as of this branch, that's
no longer a placeholder to worry about. `platform-gateway` now sits in front of catalog-service for
real, verifies every caller's Keycloak JWT (signature via JWKS, expiry, issuer —
`src/core/gateway/app/auth.py`), and is the only thing that ever sets those three headers on a
request that reaches catalog-service — `X-User`/`X-Role` are derived straight from the verified
token, never anything a caller declared; `X-Workspace` stays a client-supplied hint but gateway
checks it against the token's `groups` claim before trusting it (403 if there's no match). Any
request routed through gateway (which is everything `platform-cli` sends — see
`platform_sdk/client.py`) can no longer forge identity or role. See `deps.py`'s own docstring for
the full picture from catalog-service's side.

**Network-layer gap, closed 2026-09-03 (`catalog-service-netpol` branch)** — corrected here
2026-09-11 after this entry was found to have never been updated when that branch actually shipped
(the manifest itself, and its own comments, have said "closed" since 2026-09-03; this doc just never
caught up). `src/core/argocd/manifests/catalog-service.yaml` now includes a `NetworkPolicy`
(`catalog-service-restrict-ingress`): ingress into `catalog-service`'s pods only from pods in the
`gateway` namespace (matched via the `kubernetes.io/metadata.name` label every namespace gets
automatically since k8s 1.21 — nothing needed to explicitly label `gateway`), restricted to port
8000, the one port the Service exposes. Ingress-only on purpose — `catalog-service`'s own outbound
traffic to Postgres/DNS is untouched.

**Confirmed live, 2026-09-11:** `sudo kubectl -n catalog-service get networkpolicy
catalog-service-restrict-ingress` shows it applied on the real cluster (created `2026-09-03T06:12:28Z`,
matching git exactly). A disposable pod in the `keycloak` namespace (deliberately not `gateway`)
running `curl -v http://catalog-service.catalog-service.svc.cluster.local:8000/healthz` got a clean
`Connection refused` in ~1ms — DNS resolved fine, so this is the policy rejecting the connection, not
an unrelated failure. Gateway's own traffic to catalog-service (proven working throughout this same
session, across every module/auth test) confirms the allowed path still works. The one risk the
manifest's own comment flagged up front — that kubelet-sourced readiness/liveness probe traffic
might not match a namespaceSelector-only rule, since it doesn't originate from a pod in any namespace
— turned out to be a non-issue in practice: no ipBlock fallback was ever needed.

**Status:** both the application-layer gap (platform-gateway-auth, 2026-09-02) and the network-layer
gap (catalog-service-netpol, 2026-09-03) are closed and confirmed live. `catalog-service` can now
safely stay off-limits to anything but gateway — still don't put it behind an `Ingress` or
`LoadBalancer` Service, since that would reintroduce off-cluster reachability this NetworkPolicy
never tried to address (it only restricts in-cluster, pod-to-pod traffic).

### `platform-cli-login`'s device-grant fields — one Keycloak-version detail confirmed only at bootstrap-script-run time

Added 2026-09-02, platform-gateway-auth branch. `bootstrap/keycloak-bootstrap-login-client.sh`
creates the public client `platform login`'s device flow authenticates against. Keycloak's
`ClientRepresentation` has a top-level `oauth2DeviceAuthorizationGrantEnabled` boolean for enabling
RFC 8628, but that field 400'd with an "Unrecognized field" error on at least one real Keycloak
version in the wild ([keycloak/keycloak#19688](https://github.com/keycloak/keycloak/issues/19688),
reported against v21.0.2) — this cluster's Operator is pinned to 26.7.2, which may or may not still
hit it; nothing found while researching this said definitively either way for that specific version,
so the script doesn't gamble on it. The underlying, always-supported mechanism is the older
attributes-map key `attributes["oauth2.device.authorization.grant.enabled"]`.

**What the script actually does:** sends both the top-level field and the attributes key together on
the first attempt (covers whichever Keycloak build wants the top-level field). If that specific
request 400s naming `oauth2DeviceAuthorizationGrantEnabled` as an unrecognized property, it retries
with just the attributes key and prints which path it took. Any other failure reason is surfaced as
an error rather than silently retried further — same "verify before a live-mutating script depends
on it, be upfront if you can't fully" discipline as `keycloak-bootstrap-cli-client.sh`'s own header
comment describes for its `kcadm.sh`-adjacent Admin API calls.

**Status:** not something to fix — it's inherent uncertainty about a specific Keycloak version's API
surface, handled defensively rather than assumed away. If you run this script and see the "top-level
field rejected, falling back" warning, that's expected on some builds, not a sign of a bigger
problem — the client ends up correctly configured either way. Worth noting here which path this
cluster's 26.7.2 Operator actually took, the first time this script runs for real, so a future reader
doesn't have to rediscover it.

**Confirmed 2026-09-02, first live run against `homelab-dev`'s real Keycloak 26.7.2 Operator:** it
*does* still reject the top-level field —
`{"error":"Invalid json representation for ClientRepresentation. Unrecognized field
\"oauth2DeviceAuthorizationGrantEnabled\" at line 9 column 48."}` — same as the v21.0.2 report this
entry was written against. The script's fallback handled it exactly as designed: retried with the
attributes-only body, created `platform-cli-login` successfully, and printed which path it took. So
on this cluster, expect to see the fallback warning every time this script creates the client fresh
(idempotent re-runs after that just report the client already exists, no warning). One unrelated,
also-expected bit of output from the same run: two `curl: (7) Connection refused` lines appeared
right after the port-forward started — that's the script's own readiness-poll loop (up to 20 tries,
0.5s apart, stderr not suppressed) finding its feet before `kubectl port-forward` finished
establishing, not a failure; the run went on to succeed. Noting it here since the two look alarming
in the raw log without this context.

### `keycloak-tls`'s Certificate needed the in-cluster Service DNS name added as a SAN

Added 2026-09-02, platform-gateway-auth branch. `platform-gateway` connects directly to Keycloak's
real in-cluster Service DNS name (`platform-service.keycloak.svc.cluster.local:8443`) to fetch the
JWKS — deliberately NOT the port-forward + `curl --resolve`-equivalent trick
`platform_sdk/_keycloak_connection.py`'s CLI-side tooling uses (that mechanism monkeypatches
`socket.getaddrinfo` process-wide, fine for a one-shot CLI invocation, unsafe in a long-running
concurrently-serving Deployment). Verified before relying on it, not assumed: Keycloak's
`hostname-strict` behavior (`spec.hostname.hostname: keycloak.platform.local`, see the "Reaching
Keycloak by raw IP" entry above) only governs URLs Keycloak itself *generates* — redirects, form
actions, the `iss` claim in every token — it does NOT reject an incoming request based on its Host
header or TLS SNI. A pure JWKS/JSON API call made directly against the in-cluster Service DNS name
works fine at the application layer.

The actual (and only) blocker was narrower: `keycloak-tls`'s `Certificate`
(`manifests/keycloak-instance.yaml`) only listed `keycloak.platform.local` in `dnsNames` — a direct
connection to `platform-service.keycloak.svc.cluster.local` failed *TLS hostname verification*, not
the application-layer request itself.

**Status:** fixed by adding `platform-service.keycloak.svc.cluster.local` and
`platform-service.keycloak.svc` to that Certificate's `dnsNames` (both the fully-qualified and short
in-cluster DNS forms, since which one a client presents as SNI/Host can depend on how the target is
written in its own config). `platform-ca-secret` also needed Reflector annotations added
(`manifests/cluster-issuer.yaml`'s `secretTemplate`) so its CA mirrors into gateway's namespace —
gateway mounts only the `ca.crt` key from that mirrored Secret (never `tls.key`, the CA's private
key — see `manifests/gateway.yaml`'s own comment for why that distinction matters).

### `platform login`'s first live run hit a real race in `_PortForward`'s readiness check

Found 2026-09-02, platform-gateway-auth branch, during the live end-to-end pass (not a hypothetical
— `platform login` failed on its very first real invocation against `homelab-dev`). Symptom: a
`ConnectError: [Errno 104] Connection reset by peer` raised from inside httpx's TLS handshake
(`start_tls`), not a connect-refused — meaning something accepted the TCP connection on
`127.0.0.1:18444` and then reset it mid-handshake. Ruled out a stale/colliding port-forward first
(`ss -ltnp | grep 18444` came back empty — nothing else was listening), which pointed at the real
cause: `platform_sdk/_keycloak_connection.py`'s `_PortForward._wait_ready()` only confirmed a bare
TCP `connect()` succeeded before handing control back to the caller. `kubectl port-forward` opens its
local listener and starts accepting connections slightly *before* its reverse tunnel to the target
pod is actually usable — so a caller connecting in that narrow window gets its TCP connection
accepted, then reset once the tunnel underneath still isn't ready, which is exactly indistinguishable
from "something is genuinely broken" from the caller's side. This is shared code
(`KeycloakAdminClient` and `KeycloakLoginFlow` both use it, per `_keycloak_connection.py`'s own
docstring) — `workspace invite`'s earlier live confirmation (2026-09-01) apparently just didn't hit
the race that day; it's a timing window, not a guarantee.

**Status:** fixed — `_wait_ready()` now requires a full TLS handshake to complete against
`127.0.0.1:<local_port>` (cert verification deliberately OFF, since this is a liveness probe for the
tunnel, not an identity check; the real request right after `_wait_ready` returns goes through
`_ResolvePatch` and gets properly verified against `platform-ca`), the same guarantee
`keycloak-bootstrap-cli-client.sh`'s and `keycloak-bootstrap-login-client.sh`'s own `curl -k`
readiness loops already get, just without shelling out to curl. Covered by three new unit tests in
`tests/test_keycloak_connection.py` — a working TLS listener, a listener that accepts TCP but never
completes a handshake (the exact regression shape), and nothing listening at all — none of which need
kubectl or a live cluster, since `_wait_ready()` is pure socket logic underneath the parts of
`_PortForward` that do (`start()`/`_extract_ca_cert()`, still confirmed-live-only, see
`test_keycloak_admin.py`'s docstring for why). Re-running `platform login` after this fix landed got
past this specific failure — see the next entry for what showed up right after.

### `platform login`'s printed verification URL was only ever reachable from the machine running it

Found 2026-09-02, platform-gateway-auth branch, same live end-to-end pass, immediately after the
`_wait_ready` race above was fixed. `platform login` ran to completion and printed a verification
URL — `https://keycloak.platform.local:18444/realms/platform/device?user_code=...` — but opening it
in a browser on a *different* machine (the Windows box this repo's working copy lives on) than the
one running `platform login` (`homelab-dev`) failed to resolve at all.

Two things compound here, both worth understanding on their own:

1. **The port is `platform-cli-login`'s own ephemeral local port-forward port (`18444`), not
   Keycloak's real one.** Keycloak's `hostname-strict` behavior (see the "Reaching Keycloak by raw
   IP" entry above) only pins the *hostname* it uses when generating URLs like
   `verification_uri`/`verification_uri_complete` — it reflects back whatever *port* the originating
   request actually came in on. Since `KeycloakLoginFlow` sends the device-authorization request
   through its own local port-forward (`https://keycloak.platform.local:<local_port>`), Keycloak
   echoes that same local port back into the URL it hands back — not whatever port Keycloak is really
   deployed on. Confirmed, not assumed: this wasn't previously known about this Keycloak build's
   hostname provider before this run.
2. **`_PortForward` bound loopback-only, by design, before this fix.** That was correct for
   `KeycloakAdminClient` (`workspace invite`'s CLI process is the only thing that ever talks to its
   forward — no browser involved) but wrong for `KeycloakLoginFlow`: device-flow login fundamentally
   requires a human's browser to open the exact URL Keycloak generated, and per point 1 above, that
   URL always points at wherever this specific forward is listening. A loopback-only forward makes
   that URL unopenable from anywhere but the one process that created it — including the very common
   case (this one) where `platform login` runs on a headless/remote box and the human's browser is on
   a different machine entirely.

**Status:** fixed — `_PortForward` now takes a `bind_address` (default `"127.0.0.1"`, unchanged
behavior for `KeycloakAdminClient`), and `KeycloakLoginFlow` passes `bind_address="0.0.0.0"` so its
forward is reachable from the wider LAN, not just `localhost`. Covered by two new
`tests/test_keycloak_connection.py` cases confirming the constructed `kubectl port-forward` command
carries `--address 127.0.0.1` by default and `--address 0.0.0.0` when explicitly requested — both by
capturing the argv via a monkeypatched `subprocess.Popen`, no kubectl needed.

**Still needed to actually open the URL from a different machine, and NOT something this fix
alone solves — the same fix as the "Reaching Keycloak by raw IP" entry above, same reasoning:** a
hosts-file entry on whichever machine's browser you're using, pointing `keycloak.platform.local` at
`homelab-dev`'s real LAN IP (not `127.0.0.1`, not the raw IP by itself, and not any of this cluster's
in-cluster Service DNS names — those only resolve inside the cluster). The port stays whatever
`platform-cli`'s `keycloak_login_local_port` setting is (`18444` by default) — that part doesn't
change, since it's still an arbitrary local port being forwarded, just no longer loopback-restricted.

**Bounded exposure, worth being upfront about rather than silently deciding:** binding to `0.0.0.0`
means Keycloak's HTTPS listener is reachable by anything on the LAN for as long as one `platform
login` invocation runs — typically well under a minute, and only proxying to Keycloak's own login UI
(nothing more privileged than what any browser could already reach once this cluster has a real
Ingress in front of Keycloak, which is the eventual intended state anyway per the "Reaching Keycloak
by raw IP" entry's closing note). Consistent with this repo's existing homelab/trusted-LAN threat
model (MetalLB's pool, no firewall by default) — flagged here rather than assumed acceptable without
saying so.

### `platform login`'s tokens failed gateway's issuer check — same port-reflection behavior, a third symptom

Found 2026-09-02, platform-gateway-auth branch, same live end-to-end pass — the third distinct
failure mode traced back to Keycloak's hostname provider only pinning the *hostname* it uses in
generated values, not the port (see the two entries above for the first two symptoms of the same root
cause: the bootstrap script's device-grant fields entry is unrelated, but "Reaching Keycloak by raw
IP" and the `platform login` verification-URL entry immediately above are the same mechanism). Once
the verification URL was reachable and login completed successfully, `platform me` failed with
`gateway error (401): Token failed verification: Invalid issuer`.

Confirmed, not assumed: decoding the saved token's `iss` claim
(`python3.12 -c "import json, base64; ..."` against `~/.config/platform/credentials.json`) showed
`https://keycloak.platform.local:18444/realms/platform` — the CLI's own ephemeral local port-forward
port, baked into the token by the same request-port-reflection behavior already confirmed above. This
could never match `platform-gateway`'s `expected_issuer` (`gateway/app/config.py`, built from the
fixed `GATEWAY_KEYCLOAK_PUBLIC_URL` — `https://keycloak.platform.local`, no port) — not a one-time
fluke, every token obtained through any port-forward-based path would carry whatever ephemeral port
that specific connection happened to use, so this was guaranteed to keep failing on every future
`platform login` too, not just this one.

**Status:** fixed by pinning the port Keycloak stamps into every generated URL/claim, decoupling it
from whatever port a given request actually arrives on. This CRD version
(`k8s.keycloak.org/v2alpha1`) has no typed `hostname.port`/`hostnamePort` field — confirmed via
`kubectl explain keycloak.spec.hostname --recursive` against the live cluster before assuming one
existed, same discipline as this file's other entries. The Keycloak Operator's documented escape
hatch for passing raw server options (https://www.keycloak.org/server/all-config) straight through as
`KC_*` env vars is `additionalOptions` — confirmed present the same way
(`kubectl explain keycloak.spec.additionalOptions --recursive`) before relying on it.
`manifests/keycloak-instance.yaml`'s `Keycloak` CR now sets
`additionalOptions: [{name: hostname-port, value: "18444"}]` (→ `KC_HOSTNAME_PORT=18444`), and
`manifests/gateway.yaml`'s `GATEWAY_KEYCLOAK_PUBLIC_URL` was updated to
`https://keycloak.platform.local:18444` to match. `18444` was chosen deliberately, not arbitrarily: it
matches `platform_sdk/config.py`'s `keycloak_login_local_port`, so `platform login`'s own dedicated
port-forward doubles as the browser-facing endpoint with no separate manual forward needed — and it
deliberately avoids `8443` (Keycloak's real internal port, and the port already used for manual
admin-console access during this same live-testing session) specifically to avoid the class of
stale/colliding-port-forward bug already hit once this session with
`keycloak-bootstrap-login-client.sh`'s own leftover forward (see that entry, further up this file).

**Interim, not permanent — same closing note as "Reaching Keycloak by raw IP" above:** this whole pin
goes away once this cluster has a real Ingress in front of Keycloak. At that point Keycloak serves on
the Ingress's standard port and omits it from generated URLs entirely (same as any normal HTTPS site),
`additionalOptions`'s `hostname-port` entry should come out of `keycloak-instance.yaml`, and
`GATEWAY_KEYCLOAK_PUBLIC_URL` goes back to no port. Leaving both in place after that point would be
silently wrong, not just unnecessary — worth remembering to actually remove them, not just leave them
as harmless-looking leftovers, when that Ingress work lands.

**Confirmed live** — after `homelab-dev` pulled both manifest changes and Argo CD resynced
`keycloak-instance`/`gateway`, a fresh `platform login` succeeded with no "Invalid issuer" error. The
very next call (`platform me`) immediately hit a different, previously-hidden failure — see the next
entry.

### `platform me`'s first real call hit a fourth issue: PyJWT rejected a real token's `aud` claim

Found 2026-09-02, platform-gateway-auth branch, same live pass, immediately after the issuer fix
above was confirmed working — `platform me` failed with `gateway error (401): Token failed
verification: Invalid audience`. Fourth distinct live-testing finding in this one session, and unlike
the first three, NOT related to Keycloak's hostname/port behavior at all — this one is a gap between
what the test suite's synthetic JWTs looked like and what a real Keycloak-issued token actually
contains.

`gateway/app/auth.py`'s `verify_token()` calls `jwt.decode(...)` without an `audience=` argument.
PyJWT's default `options` has `verify_aud: True` — if the token being verified carries an `aud` claim
but the caller never says what audience to expect, PyJWT raises `InvalidAudienceError` rather than
silently skipping the check, on the reasoning that an unchecked-but-present `aud` claim is probably a
caller mistake. Every real Keycloak-issued token carries an `aud` claim (`"account"` by Keycloak's own
default, unless a client's scopes/mappers say otherwise) — but `tests/conftest.py`'s `sign_token`
fixture never included one, so every existing test signed a token shape real Keycloak never actually
produces, and this path went completely untested until a real login exercised it.

**Status:** fixed — `verify_token()` now passes `"verify_aud": False` in `jwt.decode()`'s `options`,
with a comment explaining why explicitly (not simply "pass the right audience instead"): gateway isn't
itself a registered Keycloak client with a principled expected audience of its own —
`platform-cli-login` mints tokens for whatever Keycloak's default happens to be, and hardcoding a
check against that default (`"account"`) would tie this code to an internal Keycloak implementation
detail that could silently change if this realm's client scopes/mappers are ever reconfigured, without
strengthening the actual trust boundary at all. That boundary is signature + issuer + expiry + the
`groups` claim's workspace-membership check in `derive_headers()` — none of which depend on `aud`.
`tests/conftest.py`'s `sign_token` now includes a realistic `"aud": "account"` claim by default (so
every existing test exercises this path going forward, not just a new one), plus a new
`test_verify_token_accepts_a_token_carrying_an_aud_claim` regression test in `test_auth.py` pinning
the fix explicitly. 31/31 gateway tests pass, ruff clean.

**Confirmed live, 2026-09-02** — once the fix actually reached `dev` (a real gap in its own right: the
commit initially only existed on `feature/platform-gateway-auth`, never merged forward, so neither CI
nor Argo CD — both gated on `dev` — ever saw it; see the "four self-referencing apps" entry above for
the same class of gotcha applying to CI/image builds, not just Argo CD Applications) and
`build-and-push-gateway` rebuilt `ghcr.io/dougallpercival/gateway:dev`, `platform me` returned real
data end to end: `workspace: personal`, `user: dougall`, `role: editor` — matching the group
membership `platform workspace invite --role editor` set up earlier. This is the first fully clean
`platform login` → `platform me` round-trip through the real cluster.

**The plan's full live verification is now complete.** The last remaining item — a viewer-role user
attempting a write gets a real 403 — was confirmed the same day: a second Keycloak user (`testviewer`)
was created by hand, added to `/workspaces/personal/viewer` via `platform workspace invite testviewer
--role viewer`, logged in via `platform login`, and `platform dataset create ...` correctly rejected
with a 403 — role came entirely from real Keycloak group membership, nothing the client sent. That's
the actual security property this branch exists to prove, and it holds. Every item in the plan's
Verification section is now checked off against the real cluster, not just the test suite: the
bootstrap script's live device-grant fallback, the Certificate SAN/Reflector fix, gateway
Synced/Healthy with a working `/healthz`, `platform login`'s full device flow, `platform me`/`platform
workspace list` through gateway, and this final role-enforcement check.

**The general lesson, worth stating plainly rather than leaving implicit:** four distinct live-testing
failures in one session, and three of the four (this one included) trace back to the test suite's
synthetic tokens being more lenient than a real Keycloak-issued one — missing `aud` here, and the
first three all being downstream of `hostname-port` behavior no unit test could exercise without a
real cluster. Worth remembering next time a new claim or Keycloak-specific behavior is added anywhere
in this codebase: match what real Keycloak actually produces in test fixtures, not just what's
convenient to construct, or the gap just moves to the next thing that touches it.

### GHCR packages default to private on first publish — one manual step after `ci.yml`'s first push

Added 2026-09-01, alongside `.github/workflows/ci.yml` and `manifests/catalog-service.yaml`.
Confirmed against GitHub's own docs before relying on it, not assumed: when a package is published
to `ghcr.io` for the first time — via a workflow's ambient `GITHUB_TOKEN`, which is how `ci.yml`
does it — its visibility defaults to **private**, regardless of whether the source repository
itself is public. There's no setting that makes it inherit the repo's visibility automatically;
the `org.opencontainers.image.source` label `ci.yml` sets links the package to this repo (so GHCR
*can* inherit access permissions from it) but that's a different thing from visibility, which still
needs one manual flip.

**Fix, once, after `ci.yml`'s first successful push to `dev`:** on GitHub, go to the
`catalog-service` package (`https://github.com/users/DougallPercival/packages/container/package/catalog-service`,
or find it via your profile's Packages tab) → Package settings → "Danger Zone" → Change visibility
→ Public → confirm by typing the package name. **This is one-way** — GitHub does not let you make a
public package private again, so don't do this speculatively for a package you're not sure should
be public.

Until this is done, every pull against `ghcr.io/dougallpercival/catalog-service` from outside this
GitHub account's own Actions runs (including `manifests/catalog-service.yaml`'s Deployment pulling
it into your cluster) will fail with an authentication/not-found error — expected, not a sign
anything else is broken.

### First sync of the `catalog-service` Application can show a transient secret-not-found error

Added 2026-09-01. Same underlying race as "Postgres is a shared core service, but its
auto-generated credential can't leave its namespace" above, different pair of namespaces: this
Application's `CreateNamespace=true` creates the `catalog-service` namespace as part of syncing
itself, and Reflector (mirroring `platform-postgres-catalog-credentials` in from `postgres`) can
only start mirroring into a namespace once it exists. So the migration Job and/or Deployment can
briefly sit in `CreateContainerConfigError` ("secret ... not found") right after the very first
sync, before Reflector catches up.

**Status:** self-heals within seconds via Argo CD's `selfHeal` + Kubernetes' own pod backoff/retry
— same resolution as Keycloak's version of this same race. Don't chase it if you see it once on a
fresh install; only worth investigating if it's still failing more than a minute or two later.

### `catalog-service`/`platform-sdk`/`platform-cli` need Python 3.12+ — most hosts' default `python3` is older

Hit for real on `homelab-dev` (Rocky Linux 9.4, 2026-09-01, installing `platform-sdk`/`platform-cli`
there for the first time): the default `python3` was 3.9.19, and `pip install -e ...` failed with
`requires a different Python` against `requires-python = ">=3.12"` in all three packages'
`pyproject.toml`. Not a bug in any of them — this repo's Python code has required 3.12+ since
`catalog-service` first existed; it just hadn't been installed directly on a host's own Python
before (CI runs `actions/setup-python@v5` with `3.12` explicitly, and the in-cluster Deployment
runs it from a container image with its own Python — this was the first time anything needed the
*host's* `python3` to already be 3.12+).

**Fix, host-dependent (this isn't something a script here can install for you — see the "Getting a
cluster up" prerequisites this repo has never enumerated before now):**

- RHEL-family 9.x (Rocky/Alma/RHEL, like `homelab-dev`): `sudo dnf install python3.12` — ships
  directly from AppStream as a non-modular package, no EPEL needed, installs alongside (doesn't
  replace) the system `python3`. Then invoke it explicitly: `python3.12 -m venv ...`.
- Ubuntu 22.04 ships 3.10, same problem: `sudo apt install python3.12` if your release carries it,
  otherwise the deadsnakes PPA.
- macOS: `brew install python@3.12`.

Each of `catalog-service/README.md`, `platform-sdk/README.md`, and `platform-cli/README.md` now
states this requirement up front rather than only surfacing it as a pip error.

### `bootstrap/*.sh` losing their executable bit after every pull — this repo is edited from Windows

Hit for real on `homelab-dev` (2026-09-01): `keycloak-bootstrap-cli-client.sh` needed `chmod +x`
again after simply pulling an updated version of the *same* script — no different than the first
time it was ever run.

Root cause: this repo's working copy lives on a Windows filesystem (`D:\Projects\OpenDataPlatform`),
and NTFS has no concept of a Unix executable bit at all. Whatever tool commits changes from that
side (Claude's own file-delivery pipeline included — it writes raw file bytes, nothing more) has no
executable bit to set, so every bash script under `bootstrap/` gets checked into git with mode
`100644` (not executable) unless something has *explicitly* told git otherwise via
`git update-index --chmod=+x`. On a Linux box, `git pull`/`checkout` applies whatever mode git
actually has on record for a file every time that file's *content* changes — so a local `chmod +x`
you did after the first `git pull` doesn't survive the next one that touches the same file; it's not
sticky the way you'd expect a permission to be.

**One-time fix (do this once, from anywhere with `git` and push access — Windows Git Bash/WSL/
PowerShell are all fine, this is a plain git plumbing command, not something OS-specific):**

```bash
git update-index --chmod=+x bootstrap/install.sh bootstrap/join-node.sh \
  bootstrap/keycloak-bootstrap-cli-client.sh bootstrap/keycloak-bootstrap-login-client.sh \
  bootstrap/keycloak-bootstrap-ui-shell-client.sh bootstrap/seal-gateway-github-token.sh \
  bootstrap/seal-gateway-module-proxy-secret.sh bootstrap/export-sealed-secrets-key.sh \
  bootstrap/snapshot-setup.sh bootstrap/teardown.sh bootstrap/verify.sh
git commit -m "Mark bootstrap scripts executable in git (Windows-side commits don't set the x-bit)"
git push
```

This changes what mode git *itself* has recorded for these files, permanently — every future
`git pull`/clone on any Linux box checks them out already executable, no more manual `chmod +x`
after every pull. (`bootstrap/lib/common.sh` deliberately isn't in that list — it's `source`d, never
executed directly, so it was never supposed to be executable.)

**List corrected 2026-09-11** — it had gone stale, missing every bootstrap script added since the
original 2026-09-01 writeup (`keycloak-bootstrap-login-client.sh`, `keycloak-bootstrap-ui-shell-
client.sh`, both `seal-gateway-*.sh` scripts, and now `export-sealed-secrets-key.sh`). Worth
re-running the command above even if you ran an earlier version of it before — `git update-index` is
idempotent for files already marked executable, so re-running it with the full current list is
always safe and just catches up whatever was missing.

Going forward: any *new* script added under `bootstrap/` (or anywhere else meant to be run directly,
e.g. `./script.sh` rather than `bash script.sh`) needs this same one-time `git update-index
--chmod=+x` treatment the first time it's committed — there's no way to automate this from Claude's
side of the file-delivery pipeline (writing file bytes to your working copy has no executable bit to
set), so it has to happen wherever the actual `git commit`/`push` happens.

### `modules-root` doesn't reliably auto-prune a module's `Application` object on uninstall

Found 2026-09-10, `feature/gateway-module-lifecycle-dispatch` branch, during the first-ever live
end-to-end exercise of `platform module uninstall` — both the underlying CLI code path itself, and,
for the first time, gateway's new `POST /modules/{id}/uninstall` dispatch mechanism triggering it via
the new `module-lifecycle.yml` workflow. Not caused by anything this branch built: the dispatch
mechanism's job ends at "delete `modules-enabled/<id>.yaml` and push" — confirmed working perfectly
(the commit landed, with the correct SSH `repoURL`, from a real non-detached branch). What's actually
broken is one layer downstream, in `apps/core/modules-root.yaml`'s own app-of-apps prune, which
predates this branch (platform-module-lifecycle, 2026-09-03) and has apparently never been exercised
live before now.

**What happened:** after `hello-module.yaml` was deleted from `modules-enabled/` and pushed,
`modules-root`'s Application correctly recalculated `hello-module` as `OutOfSync` (needing pruning) —
confirmed even after an explicit hard refresh (`kubectl annotate application modules-root
argocd.argoproj.io/refresh=hard`) against the exact commit that deleted the file. But its automated
sync (`syncPolicy.automated: {prune: true, selfHeal: true}`) never actually removed the child
`Application`. Its own `status.operationState.syncResult.resources` showed the real reason — not an
error, a deliberate skip:

```json
{"group": "argoproj.io", "kind": "Application", "name": "hello-module", "status": "PruneSkipped", "message": "ignored (requires pruning)"}
```

Ruled out before giving up on a root cause: `argocd-cm`'s `resource.exclusions` (only excludes
`Endpoints`/`EndpointSlice`/`Lease`/authn-authz noise, nothing touching `argoproj.io`), and the
`default` `AppProject`'s `namespaceResourceBlacklist`/`clusterResourceWhitelist` (no restriction on
`Application` either). Searched Argo CD's own docs and several GitHub issues without finding the exact
internal condition that produces `PruneSkipped`/`"ignored (requires pruning)"` specifically for a
nested `Application` resource — this looks like a known-flaky corner of Argo CD's app-of-apps pruning
(several unrelated community reports describe the same "correctly diffs it, then skips the actual
delete" shape), not something specific to this repo's config, but that's not a confirmed root cause,
just the best read available without digging into Argo CD's own source.

**Workaround, confirmed working:** a direct delete goes through the exact same
`resources-finalizer.argocd.argoproj.io` cascade Argo CD's own prune would have used —

```bash
sudo kubectl -n argocd delete application <module-id>
```

— correctly removed `hello-module`'s `Deployment`/`Service`. Its `PersistentVolumeClaim` intentionally
survives this (and would survive a working auto-prune too) — see `src/charts/hello-module/templates/
pvc.yaml`'s own `Delete=false` convention, referenced in `modules-root.yaml`'s comment.

**Status:** open, real follow-up work, not a "someday" caveat — every `platform module uninstall`
hits this identically whether it's run directly by an operator or triggered through gateway's
dispatch, so it isn't specific to either door. Doesn't block this branch (the dispatch mechanism's own
job is fully proven). Next step when picked back up: reproduce with a plain local `platform module
uninstall` (no gateway involved at all) to rule out anything about the workflow's own git
identity/environment being a contributing factor, then actually read Argo CD's sync-task-generation
code (or file an upstream issue if it turns out to be a genuine Argo CD bug) rather than continuing to
guess from documentation alone.

**Confirmed live again, 2026-09-10, `feature/ui-shell-addons-mutation` branch:** hit identically a
second time, now through the Add-ons page's real Remove button rather than a raw `curl`. Clicking
Remove → confirm did trigger a real, successful `module-lifecycle.yml` run (git commit landed,
`modules-enabled/hello-module.yaml` removed) — same as before, this gap is entirely downstream of the
dispatch mechanism. The page's own polling correctly never saw the catalog flip to "not installed"
(since `hello-module`'s `Application` was still sitting there Synced/Healthy against its own chart
source, orphaned but not gone), and after the 2-minute window its "Still processing — refresh in a
bit, or try again below" fallback rendered exactly as designed — so at least this is no longer a silent
"where did it go" moment for whoever's driving the UI, it's an honest "this is taking a while." The
`kubectl -n argocd delete application <module-id>` workaround, run manually, resolved it immediately
and the row flipped to "not installed" on the next poll — confirming the workaround still holds through
this second real exercise.

**Built, 2026-09-10, `feature/force-cleanup` branch:** the "Force cleanup" action sketched below is
real now — `POST /modules/{module_id}/force-cleanup` (`src/core/gateway/README.md`'s own section,
`app/modules.py`/`app/argocd.py`'s `delete_module_application()`), surfaced in the Add-ons page only
once a Remove has actually timed out, exactly as originally planned (`src/core/ui-shell/README.md`'s
Force cleanup section) — a deliberate click on an already-stuck row, never automatic, for the same
git-push-race reason called out below.

One correction against the design note this entry originally recorded: the delete goes through a
**broadened Kubernetes RBAC grant on the existing gateway ServiceAccount** (added the `delete` verb
to its `Role` in `argocd/manifests/gateway.yaml`), not Argo CD's own REST API. Raised explicitly with
the repo owner before building, since the original note's literal wording ("Argo CD's own REST API")
and its stated reasoning ("reusing/broadening the same credential `argocd.py` already holds") were in
tension — `argocd.py` has only ever held a Kubernetes ServiceAccount token, never an Argo CD-native
API credential, so "reuse the same credential" and "call Argo CD's REST API" couldn't both be true.
Confirmed: broaden the Kubernetes RBAC. This is the more literal reading of "one fewer credential
shape" — no new SealedSecret, no new bootstrap script, no new credential type in the system at all —
and produces an identical outcome, since a plain Kubernetes `DELETE` against the `Application` object
triggers the exact same `resources-finalizer.argocd.argoproj.io` cascade Argo CD's own REST API
delete or a manual `kubectl delete` would.

The git-push race called out when this was still a deferred idea was real and is why the button still
only ever appears after a timeout, never fires on its own: deleting the `Application` object
*immediately* on a Remove click would race the workflow's own git push (the workflow takes ~30s to
land the commit; if `modules-root` reconciles while the manifest file is still present in git, it
would just recreate the `Application` gateway just deleted).

**Confirmed live, 2026-09-11, against `homelab-dev`:** verified in two parts.

First, the raw endpoint, directly — with `hello-module` installed and healthy (not stuck), a
`curl -X POST .../modules/hello-module/force-cleanup` with an editor-role token returned `200`
with `{"status": "deleted", ...}`, and `kubectl -n argocd get application hello-module` immediately
came back `NotFound` — proving the broadened RBAC grant and the delete call itself work end to end
against the real cluster, independent of ever actually reproducing a stuck uninstall.
`kubectl -n hello-module get deployment,service,pvc` confirmed the `Deployment`/`Service` were torn
down by the finalizer cascade while the `PersistentVolumeClaim` correctly survived, same as the
original manual-`kubectl delete` workaround always produced. Reinstalled `hello-module` immediately
after to restore it for further testing.

Second, the actual UI path: reinstalled `hello-module`, clicked **Remove** on the Add-ons page, and
this exact prune gap reproduced a third time — the row sat on "Removal queued…" past the 2-minute
poll timeout, at which point the row's note and button correctly swapped to the new copy ("Still
removing after a while…") and a red **Force cleanup** button, exactly as designed. Clicking it
resolved the row to "not installed" on its own, with no manual `kubectl delete` step — the gap this
entry documents is now something anyone with editor access can close from the browser, not something
that requires cluster access and a terminal.

Only remaining rough edge: the button's appearance still depends on this same prune gap actually
reproducing within the 2-minute polling window (as it did, for the third time, during this same
verification) — if `modules-root` prunes cleanly, `uninstall` just resolves on its own and there's
nothing to force-clean, which is the correct behavior, not a bug in this feature.

### `ui-shell`'s mutable `:dev` image tag means "Synced/Healthy" doesn't prove the new build is actually running

Found 2026-09-10, `feature/ui-shell-addons-mutation` branch, and hit again identically the very next
branch (`feature/module-proxy`, same day) — worth its own entry rather than staying buried in two
different items' write-ups in `docs/architecture/ui-shell-plan.md`, since it will keep recurring on
every future `ui-shell` (and gateway) branch until something changes about the image-tagging strategy.

**What happens:** `manifests/ui-shell.yaml`'s Deployment (like `gateway.yaml`'s and
`catalog-service.yaml`'s) points at `ghcr.io/dougallpercival/ui-shell:dev` — a mutable tag, not a
per-commit digest — with `imagePullPolicy: Always`. Argo CD's `Synced`/`Healthy` status only reflects
whether the Deployment's *spec* (which always just says `:dev`, unchanged) matches what's in git, not
whether the actually-running pod has pulled today's build. A pod that's been up for hours will happily
sit there `Healthy`, serving yesterday's JavaScript bundle, even though the merge that was supposed to
change its behavior landed cleanly and Argo CD shows everything green.

**Symptom both times:** a page that should reflect a just-merged UI change (the Add-ons page's
Install/Remove buttons the first time; `ModuleDetail.tsx`'s embedded iframe the second) kept showing
the *old* behavior/copy after merge, with nothing in `kubectl`/Argo CD's own UI pointing at why —
everything upstream (the build, the push, the sync) looked completely correct.

**Fix, both times, the same:**

```bash
sudo kubectl -n ui-shell rollout restart deployment/ui-shell
```

— followed by a hard-refresh in the browser (`Ctrl+Shift+R`) to also bypass any cached JS bundle
client-side, since the old page may still be sitting in the browser's own cache independent of which
pod served it. A changed image digest after the restart (`kubectl -n ui-shell get pods -o
jsonpath='{.items[0].status.containerStatuses[0].imageID}'`, or just diffing pod age before/after) is
the real confirmation a new build is actually running — `Synced`/`Healthy` alone is not.

**Built, 2026-09-11, dedicated branch (not a side effect of an unrelated feature branch — deliberate
infrastructure work, as this entry originally said a real fix would need to be):** `ci.yml`'s three
`build-and-push*` jobs (catalog-service, gateway, ui-shell) each gained a step that bumps a new
`platform.io/git-sha` pod-template annotation to the triggering commit's SHA and pushes that straight
back to the branch, after every successful push-triggered image build. Changing the pod template
(not just the image reference, which is what `:dev` never does) is what actually forces Kubernetes to
roll new pods — the exact mechanism `kubectl rollout restart` itself uses, just automatic and CI-
authored instead of requiring the manual step this entry used to document as the only fix. Full
writeup: `argocd/README.md`'s "The `:dev` image tag and the git-sha rollout trick" section and
`.github/scripts/set-git-sha-annotation.sh`'s own comment for the exact mechanism.

Deliberately NOT per-commit image tags or digest-pinning — that would still leave `:dev`'s own
promotion-model meaning (`ARCHITECTURE.md` §10) untouched but adds real new complexity (Argo CD
Image Updater or equivalent, a new controller) for a problem a much smaller change already solves:
the actual bug was never "which tag," it was "nothing forces a real rollout on every deploy."

**Confirmed live, 2026-09-11:** `feature/git-sha-rollout` merged to `dev` as PR #55 (the manifests/docs
changes) followed by PR #56 (`ci.yml` + `.github/scripts/set-git-sha-annotation.sh` — added separately
after the remote file-write bridge refused to write anything under `.github/`, a deliberate safety
guard, not a bug). Since `ci.yml` sits in every package's own `paths:` filter, PR #56's merge push
tripped all three `build-and-push-*` jobs at once — the first real exercise of all three new steps
simultaneously, exactly as anticipated when this was built. All three went green
(`gh run view` on the run for merge commit `f0c7f1d`), and all three produced their own
`chore(<service>): bump deployed git-sha annotation to f0c7f1d... [skip ci]` commit on `dev` — no
partial failures, no infinite CI loop from `[skip ci]` plus the annotation-only diff staying outside
`ci.yml`'s path filters, exactly as designed.

`kubectl get deploy <svc> -n <svc> -o jsonpath='...platform\.io/git-sha...'` read
`f0c7f1d3e9d1e4c6821333f9736e62c4f7acf49e` for gateway, ui-shell, and catalog-service alike, matching
the PR #56 merge commit exactly. And the actual pods: `gateway-6f689fd9bc-xc48z`,
`ui-shell-56675bb984-4hbv4`, `catalog-service-68c6dbdccb-xcc5x` all showed `RESTARTS: 0` with an `AGE`
far younger than their Deployment objects (9d/8d/6d21h) — brand-new pod objects, not just a container
restart — with nobody running `kubectl rollout restart` anywhere in the sequence. Argo CD's existing
`selfHeal` picked up the annotation-only commit and rolled fresh pods on its own, exactly as designed.

**Status:** fixed. The manual `rollout restart` above is no longer needed after a normal deploy —
it stays documented here only as a fallback for the genuinely unrelated failure mode (a deploy that
completes without a new commit at all, e.g. a manually re-triggered build).

## Already fixed in the scripts — nothing to do, kept here as a changelog

- **`bootstrap/lib/common.sh` now prepends `/usr/local/bin` to `PATH`.** Some `sudo` configs (a
  trimmed `secure_path`, common on hardened RHEL-family systems) don't include `/usr/local/bin`
  even though k3s's `kubectl` symlink lives there — which silently broke every bare `kubectl` call
  in these scripts. This is what caused the "stuck waiting for node Ready" hang: the failure was
  real (`kubectl: command not found`), just invisible, because the wait loop redirected stderr to
  `/dev/null` to suppress unrelated noise and swallowed the real error along with it.
- **`bootstrap/install.sh`'s Argo CD manifest apply now uses `kubectl apply --server-side
  --force-conflicts`.** Plain client-side `kubectl apply` fails on Argo CD's
  `applicationsets.argoproj.io` CRD — it's large enough that the `last-applied-configuration`
  annotation client-side apply generates exceeds Kubernetes' 256KiB annotation limit.
- **`bootstrap/install.sh`'s node-Ready wait is now bounded (3 minutes) and checks `kubectl`
  actually resolves before entering the loop**, instead of being able to spin silently forever on
  a masked failure the way it did above.
- **`bootstrap/install.sh` now installs and enables `iscsid`** (Longhorn's host prerequisite —
  `iscsi-initiator-utils` on dnf, `open-iscsi` on apt), best-effort and non-fatal, skippable with
  `--skip-iscsi`. Deliberately leaves `iscsi.service` alone (a separate unit that does unrelated
  boot-time node auto-discovery — enabling it can add 2-3 minutes to every boot for no benefit
  here); verified the `iscsid`-vs-`iscsi.service` distinction against
  [Longhorn's own docs](https://longhorn.io/kb/troubleshooting-open-iscsi-on-rhel/) rather than
  assuming.
- **`bootstrap/install.sh` now generates `platform-postgres-keycloak-credentials`** (step 2d) and
  `apps/reflector.yaml` mirrors it across namespaces — see "Postgres is a shared core service, but
  its auto-generated credential can't leave its namespace" above.
