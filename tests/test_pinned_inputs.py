"""Isolated checks for the optional, exact input handoff and deploy overlay."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
import uuid


BIN = Path(__file__).resolve().parents[1] / "bin"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(path: Path, name: str, payload: bytes) -> None:
    with tarfile.open(path, "w:gz") as stream:
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        stream.addfile(member, io.BytesIO(payload))


class PinnedInputsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="fq-pinned-test-")
        self.addCleanup(self.tmp_ctx.cleanup)
        self.root = Path(self.tmp_ctx.name)
        install = self.root / "install"
        install.mkdir()
        for name in ("firesim_queue.py", "pinned_files.py"):
            shutil.copyfile(BIN / name, install / name)
        old_root = os.environ.get("FIRESIM_QUEUE_ROOT")
        old_snapshot = os.environ.get("FIRESIM_QUEUE_HWDB_SNAPSHOT_ROOT")
        self.addCleanup(self._restore_env, "FIRESIM_QUEUE_ROOT", old_root)
        self.addCleanup(self._restore_env, "FIRESIM_QUEUE_HWDB_SNAPSHOT_ROOT", old_snapshot)
        os.environ["FIRESIM_QUEUE_ROOT"] = str(self.root / "queue")
        os.environ["FIRESIM_QUEUE_HWDB_SNAPSHOT_ROOT"] = str(self.root / "private")
        spec = importlib.util.spec_from_file_location(
            f"firesim_queue_pinned_test_{uuid.uuid4().hex}", install / "firesim_queue.py"
        )
        assert spec is not None and spec.loader is not None
        self.queue = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.queue)
        self.install = install

    @staticmethod
    def _restore_env(name: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def _inputs(self):
        elf = self.root / "control.elf"
        elf.write_bytes(b"selected control ELF")
        bit = self.root / "bit.tgz"
        driver = self.root / "driver.tgz"
        archive(bit, "platform/image.bin", b"selected image")
        archive(driver, "driver.bin", b"selected driver")
        hwdb = self.root / "hwdb.yaml"
        hwdb.write_text(
            f"selected:\n  bitstream_tar: {bit.as_uri()}\n"
            f"  driver_tar: {driver.as_uri()}\n"
        )
        files = [[str(path), digest(path)] for path in (elf, bit, driver)]
        slots = [["owned-slot", "job-control.elf"]]
        uris = [
            ["bitstream_tar", bit.as_uri(), "firesim.tar.gz"],
            ["driver_tar", driver.as_uri(), "driver-bundle.tar.gz"],
        ]
        return elf, hwdb, files, slots, uris

    def _selection(self, inputs):
        elf, hwdb, files, slots, uris = inputs
        return self.queue._committed_declaration(
            files, slots, uris, stage_from=str(elf), bootbinary=elf.name,
            hw_config="selected", hwdb_raw=hwdb.read_bytes(),
        )

    def test_private_copy_and_consumed_slot_bytes(self) -> None:
        inputs = self._inputs()
        selection = self._selection(inputs)
        snapshot_root = self.queue._prepare_hwdb_snapshot_root()
        original = snapshot_root / "original.yaml"
        original.write_bytes(inputs[1].read_bytes())
        state = self.queue._private_committed_inputs(
            selection, snapshot_root, 1, original, "selected", str(inputs[0])
        )
        self.assertNotEqual(state["effective"].sha256, state["original_hwdb"].sha256)
        self.assertEqual(state["root"].stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(item.path.stat().st_mode & 0o777 == 0o444
                            for item in state["private_files"]))

        staged = self.root / "staged.elf"
        shutil.copyfile(state["staged"].path, staged)
        simulation = self.root / "simulation"
        slot = simulation / "owned-slot"
        slot.mkdir(parents=True)
        shutil.copyfile(staged, slot / "job-control.elf")
        for item in state["archives"]:
            source = self.queue._local_input_path(item["private_uri"])
            shutil.copyfile(source, slot / item["basename"])
            with tarfile.open(source, "r:gz") as stream:
                for member in stream.getmembers():
                    if member.isfile():
                        output = slot / member.name
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with stream.extractfile(member) as payload:
                            output.write_bytes(payload.read())
        self.queue._verify_committed_inputs(
            state, staged, simulation, "before_runworkload", consumed=True
        )
        self.assertTrue((state["root"] / "observed-before_runworkload.json").is_file())

        # A changed submitter path cannot change a private snapshot. A changed
        # consumed byte must be refused before the workload runs.
        inputs[0].write_bytes(b"new source bytes")
        (slot / "driver.bin").write_bytes(b"wrong consumed driver")
        with self.assertRaisesRegex(Exception, "changed"):
            self.queue._verify_committed_inputs(
                state, staged, simulation, "after_tamper", consumed=True
            )

    def test_refuses_incomplete_membership_and_helper_tamper(self) -> None:
        inputs = self._inputs()
        inputs[3].clear()
        with self.assertRaisesRegex(RuntimeError, "membership"):
            self._selection(inputs)
        inputs = self._inputs()
        (self.install / "pinned_files.py").write_text("raise RuntimeError('changed')\n")
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            self._selection(inputs)

    def test_overlay_uses_private_writers_and_original_platforms(self) -> None:
        deploy = self.root / "chipyard" / "sims" / "firesim" / "deploy"
        (deploy / "workloads" / "control").mkdir(parents=True)
        (deploy.parent / "platforms").mkdir()
        (deploy.parent / "platforms" / "platform.py").write_text("# source\n")
        (deploy / "firesim").write_text("# source CLI\n")
        work = self.root / "job"
        work.mkdir()
        overlay = self.queue._deploy_overlay(work, deploy, "control", "control.elf")
        self.assertEqual((work / "platforms").resolve(), (deploy.parent / "platforms").resolve())
        self.assertEqual((overlay / "firesim").resolve(), deploy / "firesim")
        for name in ("logs", "results-workload", "generated-topology-diagrams", "workloads/control"):
            self.assertTrue((overlay / name).is_dir())
            self.assertFalse((overlay / name).is_symlink())
        self.assertEqual(self.queue._deploy_overlay(work, deploy, "control", "control.elf"), overlay)
        (work / "platforms").unlink()
        foreign = self.root / "foreign"
        foreign.mkdir()
        (work / "platforms").symlink_to(foreign, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "identity differs"):
            self.queue._deploy_overlay(work, deploy, "control", "control.elf")


if __name__ == "__main__":
    unittest.main()
