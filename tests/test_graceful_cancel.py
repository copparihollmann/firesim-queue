#!/usr/bin/env python3
"""A cancel must never interrupt the FPGA flash (INFRASETUP).

FireSim's infrasetup removes the card's xdma device, programs the bitstream
and rescans PCIe.  Job 1139 (2026-09-26) was SIGTERM'd between the remove and
the rescan and left the U250 off the bus for every user.  A cancel observed
during INFRASETUP is therefore deferred until the phase exits on its own; in
every other phase it still signals the process group at once.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import uuid


QUEUE_MODULE = Path(__file__).resolve().parents[1] / "bin" / "firesim_queue.py"


class GracefulCancelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="fq-graceful-test-")
        self.addCleanup(self.tmp_ctx.cleanup)
        old_root = os.environ.get("FIRESIM_QUEUE_ROOT")
        self.addCleanup(self._restore_env, old_root)
        os.environ["FIRESIM_QUEUE_ROOT"] = str(Path(self.tmp_ctx.name) / "q")
        spec = importlib.util.spec_from_file_location(
            f"firesim_queue_graceful_test_{uuid.uuid4().hex}", QUEUE_MODULE)
        assert spec is not None and spec.loader is not None
        self.queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.queue)

    @staticmethod
    def _restore_env(old_root: str | None) -> None:
        if old_root is None:
            os.environ.pop("FIRESIM_QUEUE_ROOT", None)
        else:
            os.environ["FIRESIM_QUEUE_ROOT"] = old_root

    def _phase_proc(self, seconds: float, marker: Path) -> subprocess.Popen:
        # Stands in for a phase: sleeps, then writes a marker only if it was
        # allowed to finish (a SIGTERM'd process never writes it).
        script = f"sleep {seconds}; echo finished > '{marker}'"
        return subprocess.Popen(["bash", "-c", script],
                                start_new_session=True)

    def _supervise(self, proc, phase: str, *, cancel_after: float,
                   wall_timeout: float | None = None):
        t0 = time.time()
        return self.queue._supervise_phase(
            proc, phase=phase, job_id=1,
            check_cancel=lambda: time.time() - t0 >= cancel_after,
            heartbeat=lambda: None, poll_cancel=True,
            wall_timeout=wall_timeout, sleep_s=0.05)

    def test_cancel_during_infrasetup_lets_the_flash_finish(self) -> None:
        marker = Path(self.tmp_ctx.name) / "flash_done"
        proc = self._phase_proc(1.0, marker)
        rc, hint = self._supervise(proc, "INFRASETUP", cancel_after=0.1)
        self.assertEqual(hint, "CANCELLED")
        self.assertEqual(rc, 0)
        self.assertTrue(marker.exists(),
                        "the flash was interrupted by the cancel")

    def test_cancel_during_running_signals_at_once(self) -> None:
        marker = Path(self.tmp_ctx.name) / "run_done"
        proc = self._phase_proc(30.0, marker)
        t0 = time.time()
        rc, hint = self._supervise(proc, "RUNNING", cancel_after=0.1)
        self.assertEqual(hint, "CANCELLED")
        self.assertNotEqual(rc, 0)
        self.assertLess(time.time() - t0, 10.0)
        self.assertFalse(marker.exists())

    def test_cancel_before_the_flash_never_starts_it(self) -> None:
        # LEADING_KILL precedes INFRASETUP; cancelling there stops the job
        # before any flash begins.
        marker = Path(self.tmp_ctx.name) / "kill_done"
        proc = self._phase_proc(30.0, marker)
        rc, hint = self._supervise(proc, "LEADING_KILL", cancel_after=0.1)
        self.assertEqual(hint, "CANCELLED")
        self.assertFalse(marker.exists())

    def test_uncancelled_phase_reports_done(self) -> None:
        marker = Path(self.tmp_ctx.name) / "plain_done"
        proc = self._phase_proc(0.2, marker)
        rc, hint = self._supervise(proc, "INFRASETUP", cancel_after=1e9)
        self.assertEqual((rc, hint), (0, "DONE"))

    def test_wall_timeout_still_bounds_a_wedged_infrasetup(self) -> None:
        # The deferral is for owner cancels, not for a control plane that
        # never returns: the timeout must still end the phase.
        marker = Path(self.tmp_ctx.name) / "wedged_done"
        proc = self._phase_proc(30.0, marker)
        rc, hint = self._supervise(proc, "INFRASETUP", cancel_after=1e9,
                                   wall_timeout=0.3)
        self.assertEqual(hint, "TIMEOUT")
        self.assertFalse(marker.exists())

    def test_infrasetup_is_the_deferred_phase(self) -> None:
        self.assertIn("INFRASETUP", self.queue.CANCEL_DEFERRED_PHASES)
        self.assertNotIn("RUNNING", self.queue.CANCEL_DEFERRED_PHASES)


if __name__ == "__main__":
    unittest.main()
