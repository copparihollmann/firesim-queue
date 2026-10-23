# firesim-queue

A single-FPGA job queue for FireSim. It runs one job at a time, with priority
hints and round-robin scheduling across users. The daemon holds one FPGA lock
through each job's setup, workload, and teardown.

## Run it

Set one shared state directory for every client and the daemon:

```bash
export FIRESIM_QUEUE_ROOT=/path/to/shared/firesim-queue-state
mkdir -p "$FIRESIM_QUEUE_ROOT"
export PATH="$(pwd)/bin:$PATH"
firesim-queue daemon                 # leave running in another shell
firesim-queue submit -- bash -c 'echo hello; sleep 5'
firesim-queue interactive            # q quits
```

The CLI and daemon use `FIRESIM_QUEUE_ROOT` for `queue.db`, `jobs/`,
`daemon.log`, and `fpga.lock`. If it is unset, they use the checkout root.
**Every user of one FPGA must use the same state directory.** Separate roots
have separate locks and cannot coordinate access to the device. A production
launcher or service should pin the root, and device permissions should prevent
users from bypassing the queue.

The interactive view uses [Rich](https://github.com/Textualize/rich) when
installed, with a curses fallback. It displays the running job and phase,
queue, projected timeline, per-user usage, recent jobs, and a live UART tail
when readable. On terminals at least 100 columns wide, its fire animation
refreshes with the dashboard. Keys: `s` submit, `c` cancel, `t` tail, `u`
cycle usage window, `j`/`k` scroll, `r` refresh, `q` quit.

## Multi-user setup

Create a shared Unix group (default `firesim`) and add each queue user and the
daemon account. Make the state root and its `jobs/` directory group writable
and setgid. The queue maintains group permissions on new job directories.
Submitting users outside the group are refused when the group exists. Set
`FIRESIM_QUEUE_GROUP` on every process if your site uses a different group.

Only the job owner can cancel a job normally. Root and members of the fixed
`firesim-admins` group can force-cancel another user's job. The administrator
group is intentionally not selected from a caller-controlled environment
variable.

A daemon running under a service account may lack write access to a user's
Chipyard checkout. For `runworkload-full`, the queue creates a per-job deploy
overlay for `workloads/`, `logs/`, and `results-workload/` in that case.

Host-specific settings:

| Variable | Purpose |
|---|---|
| `FIRESIM_QUEUE_ROOT` | Shared database, lock, jobs, and logs |
| `FIRESIM_QUEUE_GROUP` | Shared Unix group; default `firesim` |
| `FIRESIM_QUEUE_CONDA_BIN` | Optional directory prepended to `PATH` before sourcing Chipyard's environment |
| `FIRESIM_QUEUE_UARTLOG` | Optional absolute UART log path to probe before the job's default simulation directory |
| `FIRESIM_QUEUE_HWDB_SNAPSHOT_ROOT` | Optional private directory for immutable hardware configuration snapshots |
| `FIRESIM_QUEUE_KEEP_SLOT=1` | Keep large simulation inputs after a job finishes |

## Commands

```text
firesim-queue submit [--priority N] [--project NAME] [--cwd PATH] [--background] -- COMMAND...
firesim-queue runworkload-full --chipyard PATH --workload NAME [--bootbinary FILE]
  [--stage-from ELF] [--hw-config KEY --hwdb-config-artifact FILE]
  [--committed-file PATH SHA256 ...] [--consumed-slot DIR ELF_NAME ...]
  [--hwdb-local-uri FIELD URI ARCHIVE_NAME ...]
  [--timeout SECONDS] [--priority N] [--project NAME] [--background]
firesim-queue status [--user USER] [--all]
firesim-queue cancel JOB_ID [--force] [--no-wait]
firesim-queue tail JOB_ID [-f] [--uartlog]
firesim-queue interactive [--window SECONDS]
firesim-queue usage [--window SECONDS]
firesim-queue daemon
firesim-queue stop-daemon
```

Priorities `0`, `5` (default), and `10` represent low, normal, and high.
Within the highest waiting tier, the scheduler chooses the user served least
recently, then the oldest submission. Running jobs are not preempted. Project
names are inferred from the working directory or supplied with `--project`.

Use `runworkload-full` when a job needs the whole FireSim lifecycle. The daemon
stages the boot binary, runs a leading `firesim kill`, `infrasetup`,
`runworkload`, and a trailing `kill` under one lock. It renders a per-job
`config_runtime.yaml` and creates a private simulation directory. A positive
`--timeout` bounds the workload phase; `0` leaves it unbounded. Leading and
trailing kill and infrasetup have separate safety timeouts.

For a bitstream-specific run, pass `--hw-config`. With
`--hwdb-config-artifact`, the queue hashes the supplied hardware database
entry at submission, makes a daemon-owned private snapshot under the lock,
and verifies that snapshot before each FireSim phase. This avoids races with a
shared hardware database. Jobs without the artifact retain the legacy shared
configuration behavior.

The optional exact-input handoff adds `--committed-file` for the staged ELF,
bitstream archive, and driver archive; `--hwdb-local-uri` for the two exact
archive URIs and their slot names; and `--consumed-slot` for each selected
local simulation slot and its renamed ELF. All three option families, plus
`--stage-from`, `--hw-config`, and `--hwdb-config-artifact`, are required
together. Under the FPGA lock, the daemon verifies the selected hashes, takes
private copies, and checks declared consumed slot bytes before the workload
and after teardown. This opt-in path requires PyYAML. The adjacent
`bin/pinned_files.py` helper is checked against its embedded SHA-256 before
use. A caller's slot list does not prove that it includes every physical
consumer; the FireSim integration must establish that correspondence.

A cancellation during `INFRASETUP` is deferred until flashing completes, then
the job skips `runworkload` and performs teardown. Cancelling a running
workload sends SIGTERM promptly. The infrasetup safety timeout still applies
if flashing is wedged. Legacy `submit` jobs are opaque to the daemon; callers
must manage their own FireSim cleanup.

Job output is in `$FIRESIM_QUEUE_ROOT/jobs/<id>/{stdout,stderr}.log`.
`tail --uartlog -f` follows the simulator's UART log if it is readable.
After a job finishes, the queue removes files of at least 8 MiB from its
simulation slot and keeps smaller files. If a workload writes large results
there, set `FIRESIM_QUEUE_KEEP_SLOT=1` on the daemon to preserve the full slot.

## Development

The implementation is in `bin/firesim_queue.py`; `bin/firesim-queue` is a
launcher. Rich is optional. PyYAML is needed for the optional exact-input handoff;
other queue commands use the Python standard library. The tests use temporary queue roots and a fake
FireSim executable; they do not require an FPGA:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

New integrations should use `runworkload-full` so setup, workload, and
teardown share the same lock. Set host-specific environment variables on the
daemon as well as on clients when the daemon needs them.
