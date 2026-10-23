#!/usr/bin/env python3
"""Isolated tests for runworkload-full's immutable HWDB snapshot.

No real daemon, FireSim installation, FPGA, or shared queue state is touched.
The queue dispatcher is imported against a temporary FIRESIM_QUEUE_ROOT and
drives a fake ``firesim`` executable that records its argv.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid


QUEUE_MODULE = Path(__file__).resolve().parents[1] / "bin" / "firesim_queue.py"


class HwdbSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="fq-hwdb-test-")
        self.tmp = Path(self.tmp_ctx.name)
        self.queue_root = self.tmp / "queue"
        self.chipyard = self.tmp / "chipyard"
        self.deploy = self.chipyard / "sims" / "firesim" / "deploy"
        self.workload_dir = self.deploy / "workloads" / "calibration"
        self.workload_dir.mkdir(parents=True)

        (self.chipyard / "env.sh").write_text(":\n", encoding="utf-8")
        sourceme = self.chipyard / "sims" / "firesim" / "sourceme-manager.sh"
        sourceme.write_text(
            'export PATH="$PWD/deploy:$PATH"\n', encoding="utf-8"
        )
        (self.deploy / "config_runtime.yaml").write_text(
            "target_config:\n"
            "    default_hw_config: shared_default\n"
            "workload:\n"
            "    workload_name: old.json\n"
            "    suffix_tag: old\n",
            encoding="utf-8",
        )
        (self.deploy / "workloads" / "calibration.json").write_text(
            json.dumps({"common_bootbinary": "calibration.elf"}) + "\n",
            encoding="utf-8",
        )
        self.stage_from = self.tmp / "calibration.elf"
        self.stage_from.write_bytes(b"\x7fELFtest-only")

        self.firesim_log = self.tmp / "firesim.argv"
        fake_firesim = self.deploy / "firesim"
        fake_firesim.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {self.firesim_log}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_firesim.chmod(0o755)

        self.hwdb = self.tmp / "universal-hwdb.yaml"
        self.hwdb.write_text(
            "test_hw:\n"
            "    bitstream_tar: file:///tmp/test-image.tar.gz\n"
            "    deploy_quintuplet_override: null\n"
            "    custom_runtime_config: null\n",
            encoding="utf-8",
        )

        old_root = os.environ.get("FIRESIM_QUEUE_ROOT")
        self.addCleanup(self._restore_env, old_root)
        os.environ["FIRESIM_QUEUE_ROOT"] = str(self.queue_root)
        spec = importlib.util.spec_from_file_location(
            f"firesim_queue_test_{uuid.uuid4().hex}", QUEUE_MODULE
        )
        assert spec is not None and spec.loader is not None
        self.queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.queue)
        # These tests are about the lifecycle implementation, not host group
        # provisioning. Avoid a dependency on the test runner's unix groups.
        self.queue._require_shared_group = lambda: None
        self.queue.POLL_INTERVAL_SECONDS = 0.01
        self.queue.HEARTBEAT_INTERVAL_SECONDS = 0.01
        self.queue.HWDB_SNAPSHOT_ROOT = self.tmp / "daemon-private-hwdb"

    @staticmethod
    def _restore_env(old_root: str | None) -> None:
        if old_root is None:
            os.environ.pop("FIRESIM_QUEUE_ROOT", None)
        else:
            os.environ["FIRESIM_QUEUE_ROOT"] = old_root

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def _submit(self) -> tuple[sqlite3.Connection, dict]:
        args = argparse.Namespace(
            chipyard=str(self.chipyard),
            workload="calibration",
            bootbinary="calibration.elf",
            stage_from=str(self.stage_from),
            priority=5,
            user="snapshot-test",
            project="queue-self-test",
            timeout=30,
            hw_config="test_hw",
            hwdb_config_artifact=str(self.hwdb),
            background=True,
        )
        self.assertEqual(self.queue.cmd_runworkload_full(args), 0)
        conn = self.queue._connect()
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        return conn, dict(row)

    def test_every_lifecycle_command_uses_exact_readonly_snapshot(self) -> None:
        expected = hashlib.sha256(self.hwdb.read_bytes()).hexdigest()
        conn, job = self._submit()
        spec = json.loads(job["kind_args"])
        self.assertEqual(spec["hwdb_config_artifact"], str(self.hwdb.resolve()))
        self.assertEqual(spec["hwdb_config_artifact_sha256"], expected)

        # This is the exact unlink/replace surface the old implementation
        # exposed. A same-group user can create any bytes in the group-writable
        # job directory; the dispatcher must neither consume nor overwrite it.
        decoy = Path(job["workdir"]) / "config_hwdb.snapshot.yaml"
        decoy.write_bytes(b"malicious replacement in job directory")

        self.assertEqual(self.queue._run_one_job_runworkload_full(conn, job), 0)
        terminal = conn.execute(
            "SELECT state, phase, exit_code FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
        self.assertEqual(tuple(terminal), ("DONE", "DONE", 0))

        snapshot = self.queue.HWDB_SNAPSHOT_ROOT / f"job-{job['id']}.yaml"
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), expected)
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o444)
        self.assertEqual(snapshot.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(snapshot.parent.stat().st_uid, os.geteuid())
        self.assertEqual(
            decoy.read_bytes(), b"malicious replacement in job directory"
        )

        calls = self.firesim_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 4)
        self.assertEqual([line.split()[-1] for line in calls], [
            "kill", "infrasetup", "runworkload", "kill"
        ])
        for line in calls:
            fields = line.split()
            self.assertIn("-c", fields)
            self.assertIn("-a", fields)
            self.assertEqual(fields[fields.index("-a") + 1], str(snapshot))
            self.assertNotIn(str(self.hwdb.resolve()), fields)

        stdout = (Path(job["workdir"]) / "stdout.log").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            f"hwdb snapshot {self.hwdb.resolve()} sha256={expected} -> {snapshot}",
            stdout,
        )
        verify_lines = [
            line for line in stdout.splitlines() if line.startswith("hwdb verify ")
        ]
        self.assertEqual(
            verify_lines,
            [
                f"hwdb verify phase={phase} sha256={expected} path={snapshot}"
                for phase in (
                    "before_leading_kill",
                    "before_infrasetup",
                    "before_runworkload",
                    "before_trailing_kill",
                    "after_trailing_kill",
                )
            ],
        )

    def test_source_tamper_after_submit_fails_before_any_firesim_call(self) -> None:
        conn, job = self._submit()
        self.hwdb.write_text("test_hw:\n    bitstream_tar: changed\n", encoding="utf-8")

        self.assertEqual(self.queue._run_one_job_runworkload_full(conn, job), 1)
        terminal = conn.execute(
            "SELECT state, phase, exit_code FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
        self.assertEqual(tuple(terminal), ("FAILED", "FAILED", 1))

        snapshot = self.queue.HWDB_SNAPSHOT_ROOT / f"job-{job['id']}.yaml"
        self.assertFalse(snapshot.exists())
        self.assertFalse(self.firesim_log.exists())
        stderr = (Path(job["workdir"]) / "stderr.log").read_text(
            encoding="utf-8"
        )
        stdout = (Path(job["workdir"]) / "stdout.log").read_text(
            encoding="utf-8"
        )
        self.assertIn("hwdb source changed after submission", stderr)
        self.assertIn("FireSim teardown skipped: no FireSim phase started", stdout)

    def test_untrusted_owner_cannot_authorize_parent_by_mode(self) -> None:
        attacker_uid = os.geteuid() + 1
        predicate = self.queue._hwdb_snapshot_parent_is_protected

        # A sticky directory's owner can remove another user's children, and
        # an owner can chmod a currently read-only directory. Neither mode is
        # sufficient when the parent belongs to an unrelated uid.
        self.assertFalse(predicate(owner_uid=attacker_uid, mode=0o1777))
        self.assertFalse(predicate(owner_uid=attacker_uid, mode=0o0555))
        self.assertTrue(predicate(owner_uid=0, mode=0o1777))
        self.assertTrue(predicate(owner_uid=os.geteuid(), mode=0o0700))

    def test_private_snapshot_tamper_between_commands_stops_lifecycle(self) -> None:
        # Model a process with the daemon uid (strictly stronger than the
        # same-group attacker this design excludes) changing the snapshot
        # after the leading kill.  Reverification must prevent infrasetup,
        # runworkload, and the trailing kill from consuming changed bytes.
        fake_firesim = self.deploy / "firesim"
        fake_firesim.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {self.firesim_log}\n"
            "snapshot=\n"
            "previous=\n"
            "for field in \"$@\"; do\n"
            "  if [ \"$previous\" = \"-a\" ]; then snapshot=$field; break; fi\n"
            "  previous=$field\n"
            "done\n"
            "chmod 600 \"$snapshot\"\n"
            "printf 'tampered-after-leading-kill\\n' > \"$snapshot\"\n"
            "chmod 444 \"$snapshot\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_firesim.chmod(0o755)

        conn, job = self._submit()
        self.assertEqual(self.queue._run_one_job_runworkload_full(conn, job), 1)
        terminal = conn.execute(
            "SELECT state, phase, exit_code FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
        self.assertEqual(tuple(terminal), ("FAILED", "FAILED", 1))

        calls = self.firesim_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].split()[-1], "kill")
        stderr = (Path(job["workdir"]) / "stderr.log").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "immutable HWDB snapshot changed before before_infrasetup", stderr
        )
        self.assertIn("FireSim teardown HWDB check failed", stderr)


if __name__ == "__main__":
    unittest.main()
