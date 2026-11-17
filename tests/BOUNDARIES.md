# Test boundaries: what a green run proves, and what it does not

This file is the contract for every skip reason and for the honesty of the test summary.
It is deliberately short. If a claim is not written here, a green run does not make it.

The suite has two parts:

* `tests/test_*.py` — unittest modules, run one process per module by `tests/testgate.py`
  (and by the module-gate mode of `tests/run_tests.sh`). The gate records `testsRun`,
  passed, failed, skipped (with the reason unittest reported), timeout and rc per module.
* `tests/run_tests.sh` — a shell scenario runner (S1–S59) that executes the installer on a
  fake root (`GM_PREFIX`) with stub commands. The vast majority of its checks are `grep`
  assertions over sources; about 15 are behavioural `t()` calls and 27 are installer runs.

Neither part is a sandbox. `tests/test_sandbox_guard.py` is an after-the-fact detector for
six modules, not an enforcement boundary (see below).

## What a Windows / Git Bash run can prove

* Python and shell logic: parsers, path handling, state machines, argument validation.
* Static properties: build map / manifest parity, generated-region markers, wiring between
  backend, rpcd wrapper, ACL and panel, rule ordering, absence of known bad patterns.
* File-level behaviour inside a fixture root: create/read/rename/copy, missing-file and
  foreign-file cases, rollback decisions, conflict detection driven by injected commands.
* YAML/JSON round-trips and config-shape tolerance, when PyYAML is present.
* Symlink-aware code paths **only** when the native-symlink probe succeeds (see
  `B-WIN-NOSYMLINK`). A copy is not a symlink, and a copy-based green does not count.
* Test-harness honesty: a module that ran zero tests or skipped all of them is not "ok";
  a timeout is reported as a timeout; an unknown skip reason fails the run.

## What a Windows / Git Bash run does NOT prove

| Boundary ID | Not proven on Windows | Where it can be proven |
|---|---|---|
| `B-LINUX-POSIX` | `flock(2)`/`fcntl` serialization, file identity/inode, hardlinks, `openat`/`O_NOFOLLOW`, TOCTOU at the kernel level, FIFO, atomic `rename`/directory `fsync`, fd inheritance across `exec` | disposable Linux VM/stand with the real filesystem |
| `B-LINUX-ROOT` | POSIX mode/uid/gid/umask, `chown`, root-only files, setuid behaviour | disposable Linux VM as root |
| `B-WIN-ACL` | Windows ACLs as a model of POSIX permissions (the suite deliberately skips 0600/0700 assertions) | not modelled; do not claim parity |
| `B-WIN-NOSYMLINK` | router-style symlink layout (absolute/relative/dangling links, `rc.d` links) when the probe cannot create a real symlink | Linux/router stand with native symlinks |
| `B-WIN-NOBASH` | every fixture test that drives the shipped shell helpers through Git Bash; those classes skip wholly | Linux CI (bash present) |
| `B-ROUTER` | procd, netifd, fw4/nft, rpcd/ubus, uci, kernel routing/sysctl, real services, BusyBox applets, flash/overlay behaviour | OpenWrt stand, disposable, no route to a production LAN |
| `B-LINUX` | Linux-only syscall/`/proc`/netlink behaviour | disposable Linux VM |
| `B-ENV-PYTHON` | python gates when no real interpreter is found (the `tests/bin/python3` stub must never fake success) | any host with python3 + PyYAML |
| `B-ENV-PYYAML` | YAML-dependent gates (manifest check, slot patcher, node scan) without PyYAML | install `python3-yaml` / `pyyaml` |
| `B-DOCS` | documentation-claim tests when `docs/` is not next to the checkout | full checkout |

Also not proven anywhere in this suite: crash/power-loss/SIGKILL between steps, real
concurrent writers (only cooperative fixtures), PID reuse, `ENOSPC`, overlay size limits
(185 MB), BusyBox `ash` semantics (Git Bash is bash; dash is not ash), network end-to-end,
DNS/VPN/UDP behaviour, and any property of a live router's configuration.

R04/R05 specifically: the Windows run proves the lock **logic** (refusal, budget, rank
`routes-worker=35` between `config-tx=30` and `route-<n>=40`), the queue protocol
(`pending -> work`, atomic rename, one drain pass over ALL pending tables, generation
recheck, bounded retry/park, 77 not retried) and the top-entrypoint timing/exit contract
against stubs. It does **not** prove `flock(2)` release after `SIGKILL`, inode identity,
descriptor inheritance by a detached worker, real `ip route`/`ip rule`, netns, procd/netifd
call paths or PPPoE stalls. Those are `B-LINUX-POSIX` / `B-ROUTER` and are skipped with those
reasons; a router/stand run is required before deployment.

R12 specifically: `test_routes_ownership.py` proves the ownership reconcile against a stateful
`ip` stub with injectable refusals - a refused `replace`/`delete`/record write keeps the old
record and a later pass repairs the table, a partial run records every prefix it installed, a
foreign occupant is never overwritten, a record for a route removed externally drops out, a
failed `ip route show` before the first mutation is `76` (record untouched) and after a mutation
is `77` (old record kept), an empty successful read-back does not erase the record, tables are
isolated, no `*.tmp` survives, and a parked `77` leaves a `retry-<table>` marker that is not
queue work, that one drain converts, and that survives a refused conversion. The structural key
(`prefix`/`via`/`dev`/`src`, kernel decoration and a route type word ignored) is exercised in
every phase: a decorated stale record is deleted by the installed line, a changed `desired`
line repairs a decorated old form, and a raced foreign line with another `src` is neither
recorded nor deleted. It does **not** prove the real refusal codes of `iproute2`, kernel
normalisation of `ip route show` output (`linkdown`, `expires`, `proto`, `metric`, multipath),
the real scheduling of a same-prefix race on a live table, BusyBox `ip`/`awk`, `mv` atomicity
on overlayfs/tmpfs, a `src` that changes while the route stays installed, or a foreign route
that differs from ours only by `proto`/`metric` or is byte-identical (accepted as ours by the
line/key comparison). Those are `B-ROUTER` / `B-LINUX-POSIX`; a Linux/router stand with fault
injection is still required.

## Why `test_sandbox_guard.py` is only a detector

It runs six of the 72 modules and compares `(mtime_ns, size)` of regular files in a short
watched list before and after each module. It does not intercept syscalls, mount
namespaces, chroot, seccomp, `LD_PRELOAD`, audit or block the network. It cannot see:

* deletion, `chmod`, ownership changes, symlink substitution, create+delete inside one module;
* a same-size rewrite with a restored mtime;
* any module outside its `MODULES` list, and most host paths outside its short list;
* Git Bash `/etc` vs Python `Path('/etc')` on Windows (only `/tmp` is mapped).

Therefore a clean guard run means "no change was noticed in the watched subset". It never
means "the suite is isolated" or "the full suite is safe to run on a work machine". The
guard now also fails when a watched module ran zero tests or skipped all of them, and it
reports timeouts as timeouts, but that is still detection after the fact.

## When the full suite may run

* Unit modules: on a development host, target modules only, with the gate and its log.
* `tests/run_tests.sh` (scenarios) and the complete unittest suite: only on a **disposable
  Linux or OpenWrt stand without a route to the production LAN**. The stand must be
  rebuildable; do not run it on a working router or on a machine that shares the production
  network.
* Windows/Git Bash is for logic and static checks. It is not a substitute for the Linux/
  router rows above.

## Skip policy

Allowed reasons are the `B-*` IDs above plus `no-python`, `no-pyyaml`, `no-bash`,
`no-native-symlink`, `posix-only`, `windows-acl`, `docs-absent`, `needs-root`, `router-only`,
`linux-only`. A module where `skipped == testsRun` is reported as `skip`, not `ok`. A skip
whose reason does not map to this list makes the run fail. The list lives in
`tests/testgate.py` (`ALLOWED_SKIPS`); changing it is a reviewed policy change, not a test
detail. Adding a skip with a new reason without updating this file is a bug.

Final numbers (tests/skips per module) are recomputed in R11 after the other R-tasks; this
document describes the mechanism, not a promise about a particular count.

## Running the gate

```
# unit modules, one process each, bounded timeout, summary.txt + summary.json
python -B tests/testgate.py run --tests-dir tests --log-dir test-logs --timeout 600

# same through the shell runner (module mode; the S1-S59 scenarios are not executed)
GM_TESTS_DIR=tests GM_TEST_LOG_DIR=test-logs GM_MODULE_TIMEOUT=600 sh tests/run_tests.sh
```

`summary.txt` is the human-readable record; `summary.json` carries the same per-module
counts for tooling. Both are written even when a module fails or times out. The shell
runner's scenario mode writes its own `summary.txt` with `pass/fail/skip/timeout` and the
preflight facts.

