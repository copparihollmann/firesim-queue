#!/usr/bin/env python3
"""Authorization regressions for owner/admin-only cancellation."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid


QUEUE_MODULE = Path(__file__).resolve().parents[1] / "bin" / "firesim_queue.py"


class CancelAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="fq-cancel-test-")
        self.queue_root = Path(self.tmp_ctx.name) / "queue"
        old_root = os.environ.get("FIRESIM_QUEUE_ROOT")
        self.addCleanup(self._restore_env, old_root)
        os.environ["FIRESIM_QUEUE_ROOT"] = str(self.queue_root)
        spec = importlib.util.spec_from_file_location(
            f"firesim_queue_cancel_test_{uuid.uuid4().hex}", QUEUE_MODULE
        )
        assert spec is not None and spec.loader is not None
        self.queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.queue)

    @staticmethod
    def _restore_env(old_root: str | None) -> None:
        if old_root is None:
            os.environ.pop("FIRESIM_QUEUE_ROOT", None)
        else:
            os.environ["FIRESIM_QUEUE_ROOT"] = old_root

    def tearDown(self) -> None:
        self.tmp_ctx.cleanup()

    def _queued_job(self, owner_uid: int = 1111, user: str = "owner") -> int:
        conn = self.queue._connect()
        try:
            cur = conn.execute(
                "INSERT INTO jobs(user, priority, submitted_at, state, cmd, "
                "cwd, env_json, workdir, project, owner_uid) "
                "VALUES(?, 5, ?, 'QUEUED', 'true', '/tmp', '{}', '/tmp/job', "
                "'test', ?)",
                (user, time.time(), owner_uid),
            )
            return int(cur.lastrowid)
        finally:
            conn.close()

    def _cancel(self, job_id: int, *, caller_uid: int, caller: str,
                force: bool, is_admin: bool) -> int:
        args = argparse.Namespace(job_id=job_id, force=force, no_wait=True)
        with (
            mock.patch.object(self.queue.os, "geteuid", return_value=caller_uid),
            mock.patch.object(self.queue.getpass, "getuser", return_value=caller),
            mock.patch.object(self.queue, "_is_queue_admin", return_value=is_admin),
        ):
            return self.queue.cmd_cancel(args)

    def _state(self, job_id: int) -> str:
        conn = self.queue._connect()
        try:
            row = conn.execute(
                "SELECT state FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            assert row is not None
            return str(row[0])
        finally:
            conn.close()

    def test_owner_can_cancel_own_job(self) -> None:
        job_id = self._queued_job()
        self.assertEqual(
            self._cancel(job_id, caller_uid=1111, caller="owner",
                         force=False, is_admin=False),
            0,
        )
        self.assertEqual(self._state(job_id), "CANCELLED")

    def test_non_admin_cannot_force_cancel_another_users_job(self) -> None:
        job_id = self._queued_job()
        self.assertEqual(
            self._cancel(job_id, caller_uid=2222, caller="other",
                         force=True, is_admin=False),
            1,
        )
        self.assertEqual(self._state(job_id), "QUEUED")

    def test_admin_can_force_cancel_another_users_job(self) -> None:
        job_id = self._queued_job()
        self.assertEqual(
            self._cancel(job_id, caller_uid=2222, caller="admin",
                         force=True, is_admin=True),
            0,
        )
        self.assertEqual(self._state(job_id), "CANCELLED")


if __name__ == "__main__":
    unittest.main()
