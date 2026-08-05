# firesim-queue

Single-FPGA job queue with per-user fair share. Multiple users / projects
share one physical FPGA via `firesim` — without this queue, concurrent
`firesim runworkload` invocations clobber each other. With it, jobs land
in a queue and execute one at a time, weighted round-robin across users.

**Currently installed at:** `$FIRESIM_QUEUE_ROOT/`
(testing location — once validated we can move to `/opt/firesim-queue/`
for a proper global install. See **Promoting to a global install** below.)

## What it does

- **FIFO with priority hints + weighted round-robin by user.** Higher
  priority (`--priority 10`) jumps the queue. Within a tier, alice doesn't
  get to run 10 jobs in a row while bob waits.
- **No preemption.** Running jobs finish on their own — critical jobs
  jump the *queue*, not the *FPGA*.
- **Per-job ETA from past similar runs.** Submits print expected wall;
  the interactive dashboard shows projected start times in a Gantt chart.
- **Per-user / per-job colors + project auto-detection.** The dashboard
  is readable when 5 users + 3 projects are jostling. Project name comes
  from cwd (`/scratch2/<user>/<project>/...`) — generic, no hardcoded list.
- **Chipyard-agnostic.** Each user keeps their own chipyard tree(s); the
  queue just locks the FPGA, doesn't know about chipyard. A user with
  `/scratch2/<user>/chipyard-v1/` and `/scratch2/<user>/chipyard-v2/`
  sees both as distinct projects (`chipyard-v1`, `chipyard-v2`).

## Layout

```
$FIRESIM_QUEUE_ROOT/
├── bin/
│   ├── firesim-queue              # CLI shim (calls firesim_queue.py)
│   └── firesim_queue.py           # the actual tool
├── jobs/<job_id>/                 # per-job artifacts
│   ├── cmd.sh                     # exact bash command that ran
│   ├── stdout.log
│   └── stderr.log
├── queue.db                       # SQLite (WAL mode)
├── daemon.{pid,log}
├── fpga.lock                      # flock'd while a job runs the FPGA
└── README.md
```

## Try it out (no FPGA needed, single-user smoke test)

```bash
PATH="$FIRESIM_QUEUE_ROOT/bin:$PATH"   # optional

# Start the daemon (in another shell — leave it running).
firesim-queue daemon

# Submit a fake "FireSim run" that just sleeps. Returns when job completes.
firesim-queue submit --priority 5 -- bash -c 'echo hello; sleep 5'

# Watch the queue live.
firesim-queue interactive
# Keys: s submit · c cancel · t tail · u cycle usage window · r refresh · q quit

# Plain text listings.
firesim-queue status               # active jobs
firesim-queue status --all         # incl. terminal
firesim-queue usage --window 86400 # last 24h per-user
```

## Try it out (real FireSim, opt-in per script)

`benchmarks/firesim_shuttle/run_all.sh` honors `FIRESIM_QUEUE=1`. When set,
the script routes `firesim infrasetup/runworkload/kill` through
`firesim-queue submit`. Default (env unset) is byte-identical to the
pre-queue behavior — no surprise routing.

```bash
# In two different shells (or two different users):
FIRESIM_QUEUE=1 ./benchmarks/firesim_shuttle/run_all.sh dronet gemmini
FIRESIM_QUEUE=1 ./benchmarks/firesim_shuttle/run_all.sh yolov8n rvv

# The second one blocks behind the first. Check progress:
firesim-queue interactive
```

## Subcommands

```
firesim-queue submit [--priority N] [--project NAME] [--cwd PATH] [--background] -- <command>
firesim-queue status [--user USER] [--all]
firesim-queue cancel <job_id> [--force]
firesim-queue tail <job_id> [-f]
firesim-queue interactive [--window SECONDS]
firesim-queue usage [--window SECONDS]
firesim-queue daemon
firesim-queue stop-daemon
```

### Priority levels
- `--priority 0`  — low (background work, courtesy to others)
- `--priority 5`  — normal (default)
- `--priority 10` — high (deadline-driven; jumps the queue but does NOT preempt)

### Project tag
Auto-inferred from cwd: `/scratch2/<user>/<project>/...` → `<project>`.
Override with `--project NAME` (useful for ad-hoc test invocations).

## Scheduling rule (one rule, easy to debug)

1. Among `QUEUED` jobs, pick the highest `priority`.
2. Within that tier, pick the user *not* served most recently (round-robin).
3. Ties → oldest `submitted_at`.

Daemon's choice is logged to `daemon.log` and reflected in `interactive`.

## Promoting to a global install

When you're ready to make this a system-wide tool (any user, any path):

1. `sudo cp -r $FIRESIM_QUEUE_ROOT /opt/firesim-queue`
2. `sudo chgrp -R <fpga-group> /opt/firesim-queue && sudo chmod -R g+rwX /opt/firesim-queue`
3. `sudo ln -sf /opt/firesim-queue/bin/firesim-queue /usr/local/bin/firesim-queue`
4. (Optional) Add a systemd unit so the daemon starts on boot:
   ```ini
   # /etc/systemd/system/firesim-queue.service
   [Unit]
   Description=FireSim job queue
   After=network.target
   [Service]
   Type=simple
   User=<fpga-group-member>
   ExecStart=/opt/firesim-queue/bin/firesim-queue daemon
   Restart=on-failure
   [Install]
   WantedBy=multi-user.target
   ```
5. Set `FIRESIM_QUEUE_ROOT=/opt/firesim-queue` in `/etc/profile.d/firesim-queue.sh`
   so `firesim-queue` finds the shared queue.db.

## Forcing all `firesim runworkload` calls through the queue

Once you're comfortable with the explicit `FIRESIM_QUEUE=1` opt-in, you
can transparently intercept *every* `firesim runworkload` by dropping a
shim earlier on `$PATH` than the chipyard `firesim`:

```bash
# /usr/local/bin/firesim  (or wherever's earliest on PATH)
#!/bin/bash
# Resolve the user's real chipyard firesim — the next firesim on PATH
# after this shim.
real=$(which -a firesim 2>/dev/null | grep -v "$(readlink -f "$0")" | head -1)

if [[ "$1" == "runworkload" ]]; then
    exec /opt/firesim-queue/bin/firesim-queue submit \
         --cwd "$PWD" -- "$real" "$@"
else
    exec "$real" "$@"
fi
```

This is opt-in too — any user who doesn't want the queue just doesn't
have `/usr/local/bin/firesim` shadowing their chipyard one.

## Implementation notes

- **SQLite WAL mode** for concurrent reader/writer between daemon and
  multiple clients.
- **flock on `fpga.lock`** held for the entire duration a job runs.
  Even if a misbehaving caller bypasses the queue, the lock blocks
  concurrent FPGA access.
- **Daemon heartbeat** kv row updated every 30s. If clients see the
  heartbeat is older than 5 min, they assume the daemon died and the
  next submitter should restart it (`systemctl restart firesim-queue`
  or `firesim-queue daemon &`).
- **Crash safety:** jobs in `RUNNING` state with a stale daemon are
  cleaned up on next daemon start (TODO — currently they just stay
  `RUNNING` and confuse `status`).

## Per-job stdout

Every job's full stdout + stderr is at:
```
$FIRESIM_QUEUE_ROOT/jobs/<job_id>/{stdout,stderr}.log
```

Tail live with `firesim-queue tail <job_id> -f`.

## Watching a running FireSim job (screen vs uartlog)

When `firesim runworkload` is the job command, FireSim itself spawns a
`screen` session (typically named `fsim0`, sometimes derived from the
workload name — check `screen -ls`) to host the simulator. There are
three ways to watch the live output, with different reach:

| Method | Who can use it | Notes |
|---|---|---|
| `screen -r fsim0` | only the user who owns the daemon (= the user the screen socket lives under: `/run/screen/S-<daemon-user>/...`) | interactive, full terminal access to the simulator |
| `sudo -u <daemon-user> screen -r fsim0` | any user with sudo to that account | same as above |
| `firesim-queue tail <job_id> --uartlog -f` | **any user** with read perms on the FireSim results dir | non-interactive, but identical content to the screen session |

The `--uartlog` mode finds the latest `results-workload/.../uartlog` under
the job's cwd and tails it. Same UART bytes as the screen, just streamed
through a regular file instead of a screen socket — works cross-user
without sudo. Recommended for monitoring; reserve `screen -r` for cases
where you actually need to interact with the simulator console.

If you want to know which screen session FireSim spawned, run
`screen -ls` as the daemon user — the name is printed there.

## Removing it

```
firesim-queue stop-daemon
# (then optionally rm -rf $FIRESIM_QUEUE_ROOT/ — keeps no
#  user data outside that dir)
```
