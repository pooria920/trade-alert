"""Explicit local-only Compose helpers. Never SSH, remove volumes or auto-rollback."""

import argparse
import os
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def run_operation(args: argparse.Namespace) -> None:
    if args.operation != "health":
        database = args.database
        if (
            not database.startswith("/app/data/")
            or not database.endswith(".db")
            or str(PurePosixPath(database)) != database
            or ".." in PurePosixPath(database).parts
        ):
            raise ValueError("Use an explicit .db path below /app/data")
        host = Path(args.host)
        if (
            not host.is_absolute()
            or not host.exists()
            or (
                not host.is_file() if args.operation == "restore" else not host.is_dir()
            )
        ):
            raise ValueError("Use an existing absolute backup file/directory")
        host = host.resolve()
        if "," in str(host):
            raise ValueError("Docker mount paths cannot contain commas")
    if not (ROOT / ".env").is_file():
        raise ValueError("Create the project .env first; see DEPLOYMENT.md")
    env = dict(os.environ)
    endpoint = subprocess.run(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout.strip()
    if env.get("DOCKER_HOST") and not env.get("DOCKER_CONTEXT"):
        endpoint = env["DOCKER_HOST"]
    if not endpoint.startswith(("unix://", "npipe://")):
        raise ValueError("Remote Docker endpoints are refused; use a local socket")
    env["DOCKER_HOST"] = endpoint
    env.pop("DOCKER_CONTEXT", None)
    compose = [
        "docker",
        "compose",
        "--project-name",
        env.get("COMPOSE_PROJECT_NAME", "stock-alert-bot"),
        "--project-directory",
        str(ROOT),
        "--env-file",
        str(ROOT / ".env"),
        "-f",
        str(ROOT / "compose.yaml"),
    ]

    def dc(*arguments: str) -> str:
        return subprocess.run(
            [*compose, *arguments], capture_output=True, text=True, check=True, env=env
        ).stdout.strip()

    def backup() -> None:
        extra = (
            ["--days", str(args.days), "--confirm-prune"]
            if getattr(args, "confirm_prune", False)
            else []
        )
        print(
            dc(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{host}:/backups",
                "bot",
                "python",
                "-m",
                "trade_alert.maintenance",
                "backup",
                args.database,
                "/backups",
                *extra,
            )
        )

    dc("config", "--quiet")
    if args.operation == "health":
        print(dc("exec", "-T", "bot", "python", "-m", "trade_alert.health"))
    elif args.operation == "backup":
        backup()
    elif args.operation == "restore":
        if dc("ps", "--status", "running", "-q", "bot"):
            raise ValueError("Stop this project bot before restoring")
        print(
            dc(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "-v",
                f"{host.parent}:/restore:ro",
                "bot",
                "python",
                "-m",
                "trade_alert.maintenance",
                "restore",
                f"/restore/{host.name}",
                args.database,
                "--confirm-restore",
            )
        )
    else:
        configured = dc(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "bot",
            "python",
            "-c",
            "import os; from pathlib import Path; print(Path(os.environ['DATABASE_URL']).resolve())",
        )
        if configured != args.database:
            raise ValueError("Backup path does not match configured database")
        backup()
        dc("build", "bot")
        dc("stop", "bot")
        dc(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "bot",
            "python",
            "-m",
            "trade_alert.maintenance",
            "migrate",
            args.database,
        )
        dc("up", "-d", "--no-deps", "--scale", "bot=1", "bot")
        print(dc("ps", "bot"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("health")
    for operation in ("backup", "restore", "deploy"):
        command = commands.add_parser(operation)
        command.add_argument("host")
        command.add_argument("database")
        if operation == "backup":
            command.add_argument("--days", type=int)
            command.add_argument("--confirm-prune", action="store_true")
        else:
            command.add_argument(
                f"--confirm-{operation}", action="store_true", required=True
            )
    args = parser.parse_args(argv)
    if args.operation == "backup" and (
        (args.days is not None) != args.confirm_prune
        or (args.days is not None and not 1 <= args.days <= 3650)
    ):
        parser.error("Retention requires --days 1..3650 --confirm-prune")
    run_operation(args)


if __name__ == "__main__":
    try:
        main()
    except OSError, ValueError, subprocess.CalledProcessError:
        raise SystemExit(
            "Local operation failed; inspect configuration/service status. No automatic rollback was performed."
        ) from None
