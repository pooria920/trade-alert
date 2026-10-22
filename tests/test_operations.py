"""Deployment helper tests mock every subprocess; never use a real Docker daemon."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trade_alert.operations import main


class OperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".env").write_text("SYNTHETIC_ONLY=true", encoding="utf-8")
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.calls = []
        self.endpoint = "unix:///var/run/docker.sock"
        self.configured = "/app/data/stock-alert.db"
        self.running = ""
        self.fail = None

        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            if self.fail and self.fail(command):
                raise subprocess.CalledProcessError(1, command)
            output = (
                self.endpoint
                if command[1] == "context"
                else self.configured
                if command[-2] == "-c"
                else self.running
                if "--status" in command
                else ""
            )
            return subprocess.CompletedProcess(command, 0, stdout=output)

        for target, value in (
            ("trade_alert.operations.ROOT", self.root),
            ("trade_alert.operations.subprocess.run", run),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, *args):
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            main(list(args))

    def deploy(self):
        self.call(
            "deploy", str(self.backups), "/app/data/stock-alert.db", "--confirm-deploy"
        )

    def test_deploy_orders_backup_build_stop_migrate_single_replica_and_preserves_project(
        self,
    ):
        with patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "existing-project"}):
            self.deploy()
        commands = [c for c, _ in self.calls]
        backup = next(i for i, c in enumerate(commands) if "backup" in c)
        build = next(i for i, c in enumerate(commands) if "build" in c)
        stop = next(i for i, c in enumerate(commands) if "stop" in c)
        migrate = next(i for i, c in enumerate(commands) if "migrate" in c)
        up = next(i for i, c in enumerate(commands) if "up" in c)
        self.assertLess(backup, build)
        self.assertLess(build, stop)
        self.assertLess(stop, migrate)
        self.assertLess(migrate, up)
        self.assertIn("bot=1", commands[up])
        for command, options in self.calls[1:]:
            self.assertEqual(command.count("-f"), 1)
            self.assertIn(str(self.root / "compose.yaml"), command)
            self.assertNotIn(str(self.root / "python/compose.yaml"), command)
            self.assertIn("existing-project", command)
            self.assertEqual(options["env"]["DOCKER_HOST"], self.endpoint)
            self.assertNotIn("DOCKER_CONTEXT", options["env"])
            self.assertNotIn("down", command)
            self.assertNotIn("volume", command)

    def test_each_failure_stops_before_later_mutations_without_rollback(self):
        for stage in ("backup", "build", "migrate"):
            self.calls.clear()
            self.fail = lambda c: stage in c
            with self.assertRaises(subprocess.CalledProcessError):
                self.deploy()
            self.assertFalse(any("up" in c for c, _ in self.calls))
            if stage != "migrate":
                self.assertFalse(any("stop" in c for c, _ in self.calls))
            self.assertFalse(any("restore" in c for c, _ in self.calls))

    def test_confirmations_invalid_paths_and_retention_fail_before_docker(self):
        for args in (
            ("deploy", str(self.backups), self.configured),
            ("restore", str(self.root / ".env"), self.configured),
            ("backup", str(self.backups), self.configured, "--days", "14"),
            ("backup", str(self.backups), "/tmp/a.db"),
            ("backup", str(self.backups), "/app/data/../a.db"),
        ):
            with self.assertRaises((SystemExit, ValueError)):
                self.call(*args)
        self.assertEqual(self.calls, [])

    def test_remote_endpoint_and_database_mismatch_are_refused(self):
        self.endpoint = "ssh://host"
        with self.assertRaisesRegex(ValueError, "Remote"):
            self.deploy()
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.endpoint = "unix:///var/run/docker.sock"
        self.configured = "/app/data/other.db"
        with self.assertRaisesRegex(ValueError, "match"):
            self.deploy()
        self.assertFalse(any("backup" in c or "build" in c for c, _ in self.calls))
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote:2375"}):
            with self.assertRaisesRegex(ValueError, "Remote"):
                self.deploy()

    def test_restore_refuses_running_bot_and_uses_read_only_backup_mount(self):
        backup = self.backups / "test.db"
        backup.write_bytes(b"SYNTHETIC")
        self.running = "existing-container"
        with self.assertRaisesRegex(ValueError, "Stop"):
            self.call("restore", str(backup), self.configured, "--confirm-restore")
        self.assertFalse(any("restore" in c for c, _ in self.calls))
        self.calls.clear()
        self.running = ""
        self.call("restore", str(backup), self.configured, "--confirm-restore")
        last = self.calls[-1][0]
        self.assertIn("restore", last)
        self.assertTrue(any(item.endswith(":/restore:ro") for item in last))
        self.assertFalse(any("up" in c for c, _ in self.calls))

    def test_backup_pruning_is_explicit_and_health_is_read_only(self):
        self.call(
            "backup",
            str(self.backups),
            self.configured,
            "--days",
            "14",
            "--confirm-prune",
        )
        self.assertIn("--confirm-prune", self.calls[-1][0])
        self.calls.clear()
        self.call("health")
        self.assertEqual(
            self.calls[-1][0][-4:], ["bot", "python", "-m", "trade_alert.health"]
        )
        self.assertFalse(any("run" in c or "up" in c for c, _ in self.calls))
