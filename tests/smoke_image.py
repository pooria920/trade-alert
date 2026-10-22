"""Run explicitly after building. Only UUID-named, network-disabled disposable resources."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


def main():
    env = dict(os.environ)

    def docker(*args):
        return subprocess.run(
            ["docker", *args], check=True, text=True, capture_output=True, env=env
        ).stdout.strip()

    endpoint = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    if env.get("DOCKER_HOST") and not env.get("DOCKER_CONTEXT"):
        endpoint = env["DOCKER_HOST"]
    if not endpoint.startswith(("unix://", "npipe://")):
        raise ValueError("Disposable checks require a local Docker socket")
    env["DOCKER_HOST"] = endpoint
    env.pop("DOCKER_CONTEXT", None)
    volume = "stock-alert-python-smoke-" + str(uuid4())
    script = Path(__file__).with_name("container_smoke.py").resolve()
    image = sys.argv[1] if len(sys.argv) > 1 else "tse-stock-alert-bot:local"
    with tempfile.TemporaryDirectory(prefix="python-image-smoke-") as directory:
        # Validate the release Compose with synthetic settings, without starting it.
        root = Path(__file__).resolve().parents[1]
        check = Path(directory) / "compose-check"
        check.mkdir()
        shutil.copyfile(root / "compose.yaml", check / "compose.yaml")
        (check / ".env").write_text("SYNTHETIC_ONLY=true\n", encoding="utf-8")
        model = json.loads(
            docker(
                "compose",
                "--project-name",
                volume,
                "--project-directory",
                str(check),
                "--env-file",
                str(check / ".env"),
                "-f",
                str(check / "compose.yaml"),
                "config",
                "--format",
                "json",
            )
        )
        assert set(model["services"]) == {"bot"}
        bot = model["services"]["bot"]
        assert bot["build"].get("dockerfile", "Dockerfile") == "Dockerfile"
        assert bot["image"].startswith("tse-stock-alert-bot:")
        assert bot["read_only"] and not bot.get("ports")
        assert bot["volumes"][0]["source"] == "stock-alert-data"
        docker("volume", "create", volume)
        try:
            result = docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--pids-limit",
                "128",
                "--mount",
                f"type=volume,source={volume},target=/app/data",
                "--mount",
                f"type=bind,source={directory},target=/backups",
                "--mount",
                f"type=bind,source={script},target=/tmp/check.py,readonly",
                "--mount",
                f"type=bind,source={script.with_name('telethon_fake.py')},target=/tmp/telethon_fake.py,readonly",
                "-e",
                "PYTHONPATH=/app",
                image,
                "python",
                "/tmp/check.py",
            )
            assert list(Path(directory).glob("stock-alert-*.db"))
            print(result)
        finally:
            # Only the uniquely named volume created by this check is removed.
            docker("volume", "rm", volume)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr, file=sys.stderr)
        raise SystemExit(1) from None
