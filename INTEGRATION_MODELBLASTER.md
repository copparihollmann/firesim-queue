# Integrating ModelBlaster with firesim-queue

ModelBlaster currently calls `firesim infrasetup` + `firesim runworkload` +
`firesim kill` directly from `validation/firesim_runner.py`. When another
user runs FireSim concurrently (e.g., merlin's `run_all.sh`), both clobber
the FPGA. The fix: route ModelBlaster's `runworkload` invocation through
`firesim-queue` so it lands in the shared queue at
`$FIRESIM_QUEUE_ROOT/` and waits its turn.

## The minimum-invasive change

ModelBlaster's existing flow is:

```
infrasetup (blocking, ~10s) → runworkload (Popen, watched via uartlog)
                            → firesim kill (when OUTPUT_END seen)
```

The FPGA-touching step is `runworkload` — everything else is FPGA-adjacent
or teardown. So we wrap **only** `runworkload` through the queue. Other
steps stay as-is.

### Diff for `validation/firesim_runner.py`

In `_firesim_run_async` (around line 198), replace the `subprocess.Popen`
that calls `_firesim_cmd(..., "runworkload")` with a `firesim-queue
submit --background` invocation. The queue submit prints the `job_id` and
returns immediately; the daemon then runs the wrapped `firesim
runworkload` inside its own process tree, holding the FPGA lock. From
ModelBlaster's perspective, the existing uartlog-watching and
`firesim kill` logic continues to work because:

- `firesim runworkload` (now spawned by the queue daemon) still writes to
  the standard `DEPLOY_DIR/results-workload/.../uartlog` path.
- When ModelBlaster sees the OUTPUT_END markers, it calls
  `_firesim_kill()` exactly as today. `firesim kill` reaches the
  `runworkload` process inside the queue's child and terminates it.
- The queue daemon notices the child exit, marks the job `DONE`, and
  releases the FPGA lock for the next user.

```python
# Before (current code, around line 198):
def _firesim_run_async(firesim_env: str, firesim_root: str,
                       log_path: str) -> subprocess.Popen:
    log_f = open(log_path, "w")
    return subprocess.Popen(
        _firesim_cmd(firesim_env, firesim_root, "runworkload"),
        stdout=log_f, stderr=subprocess.STDOUT,
    )


# After:
import os
FIRESIM_QUEUE_BIN = os.environ.get(
    "FIRESIM_QUEUE_BIN",
    "$FIRESIM_QUEUE_ROOT/bin/firesim-queue")
USE_FIRESIM_QUEUE = (
    os.environ.get("FIRESIM_QUEUE", "0") == "1"
    and os.path.exists(FIRESIM_QUEUE_BIN))

def _firesim_run_async(firesim_env: str, firesim_root: str,
                       log_path: str) -> subprocess.Popen:
    log_f = open(log_path, "w")
    if USE_FIRESIM_QUEUE:
        # Route through the shared FPGA queue. The queue daemon spawns the
        # actual `firesim runworkload` inside its own process tree and
        # holds the FPGA lock. ModelBlaster's uartlog-watching and
        # `firesim kill` logic continues to work — `firesim kill` reaches
        # the runworkload process inside the queue's child.
        priority = os.environ.get("FIRESIM_QUEUE_PRIORITY", "5")
        # Submit returns 0 only when the wrapped job completes. We want to
        # return immediately (uartlog-watcher decides when we're done), so
        # use --background. The job's lifetime is the runworkload process'
        # lifetime; firesim kill terminates it.
        argv = [
            FIRESIM_QUEUE_BIN, "submit",
            "--priority", str(priority),
            "--cwd", firesim_root,
            "--project", "modelblaster",
            "--background",
            "--",
        ] + _firesim_cmd(firesim_env, firesim_root, "runworkload")
        return subprocess.Popen(argv, stdout=log_f, stderr=subprocess.STDOUT)
    return subprocess.Popen(
        _firesim_cmd(firesim_env, firesim_root, "runworkload"),
        stdout=log_f, stderr=subprocess.STDOUT,
    )
```

### Why `--background`

The existing ModelBlaster flow expects `_firesim_run_async` to return a
`Popen` IMMEDIATELY (the caller continues to set up the uartlog watcher).
`firesim-queue submit` without `--background` would block until the job
completes — that would deadlock against the watcher in the same Python
process. `--background` keeps the existing async semantics: the queue
prints the `job_id` to log_path and exits 0 right after; the underlying
`firesim runworkload` runs in the daemon's process tree.

### Why `--cwd firesim_root`

The wrapped `firesim runworkload` needs to run in `firesim_root` (the
`<your-chipyard>/sims/firesim/` deploy dir). The queue
preserves cwd from `--cwd` flag.

### Activating it

Default behavior is unchanged (no queue) — ModelBlaster only routes
through the queue when `FIRESIM_QUEUE=1` is set in the env. Test with:

```bash
FIRESIM_QUEUE=1 python -m modelblaster.validation.firesim_runner ...
```

While that runs, in another terminal:

```bash
$FIRESIM_QUEUE_ROOT/bin/firesim-queue interactive
```

You should see ModelBlaster's job appear in the Gantt and queue panels.
Verify also that running both ModelBlaster and merlin's
`FIRESIM_QUEUE=1 ./benchmarks/firesim_shuttle/run_all.sh dronet gemmini`
simultaneously serializes through the queue instead of clobbering.

## What about infrasetup and kill?

- **`infrasetup`** doesn't reset FPGA hardware state — it just stages
  bitstream config files locally. Running it concurrent with another
  user's `runworkload` is fine. Leave it as a direct `subprocess.run`.
- **`firesim kill`** kills the `runworkload` process. With the queue,
  `runworkload` lives inside the queue daemon's process tree, but
  `firesim kill` works the same way (it pgrep's its target process).
  Leave it as-is too.

The only call site that goes through the queue is the long-running
`runworkload` itself. That's the right granularity — short setup/teardown
commands don't need queueing.

## Verifying the change end-to-end (Spike-style, no FPGA)

The queue + daemon work without FireSim — you can test the wiring by
having a mock that echoes instead of running real firesim:

```bash
# In terminal A:
$FIRESIM_QUEUE_ROOT/bin/firesim-queue daemon &

# In terminal B:
FIRESIM_QUEUE=1 FIRESIM_QUEUE_BIN=$FIRESIM_QUEUE_ROOT/bin/firesim-queue \
python -c "
import subprocess
# Simulate ModelBlaster's call
subprocess.run([
    '$FIRESIM_QUEUE_ROOT/bin/firesim-queue', 'submit',
    '--priority', '5', '--project', 'modelblaster', '--background', '--',
    'bash', '-c', 'echo mock-runworkload; sleep 5'
])
"

# In terminal C:
$FIRESIM_QUEUE_ROOT/bin/firesim-queue status
$FIRESIM_QUEUE_ROOT/bin/firesim-queue interactive
```

You should see the job land, run for 5s, and exit cleanly.

## Open questions for the ModelBlaster side

1. ModelBlaster's `firesim_runner.py` references `<your-chipyard>/...`
   as the FireSim root, while merlin uses `<your-chipyard>/...`.
   The queue is chipyard-agnostic (just locks the FPGA), so both work
   concurrently. No change needed — the queue tracks each user's `cwd`
   separately.

2. The `multi-model harness` mode that submits multiple sequential
   `runworkload`s: do you submit each as a separate queue job (so they
   share the FPGA fairly with other users) or as one big job? The
   recommendation is **separate jobs**: gives finer-grained queueing,
   and the queue's RR-by-user means alice's multi-model batch doesn't
   starve bob's single submission.

3. If you want per-job ETAs in the dashboard to be accurate for
   ModelBlaster, pass `--project modelblaster` consistently and let the
   queue's similar-cmd fingerprinting pick up patterns from past runs.
   The fingerprint normalizes digit runs, so commands that differ only
   by an iteration count get grouped.
