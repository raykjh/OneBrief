"""Safe local preview delivery for verified existing-project web results."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from onebrief.development_toolpack import DevelopmentRun, approved_edit_path
from onebrief.jobs import JobStatus, JobStore
from onebrief.project_catalog import RegisteredProject


class PreviewReceipt(BaseModel):
    schema_version: str = "onebrief-preview-receipt-v1"
    project_id: str
    job_id: str
    artifact_type: str = "web_application"
    delivery_status: str = "local_preview"
    preview_url: str
    shareable: bool = False
    source_applied: bool = False
    delivery_folder: str
    launcher_path: str
    instructions: list[str] = Field(default_factory=list)


class ExchangePreviewManager:
    """Rebuild a verified patch in a detached clone and expose a local preview URL."""

    def __init__(
        self,
        project: RegisteredProject,
        jobs_root: Path,
        previews_root: Path | None = None,
    ):
        self.project = project
        self.source_root = Path(project.root_path).resolve()
        self.jobs_root = jobs_root.resolve()
        self.previews_root = (
            previews_root or Path(tempfile.gettempdir()) / "onebrief-previews"
        ).resolve()

    @staticmethod
    def _run(
        argv: list[str],
        cwd: Path,
        *,
        timeout: int = 300,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(message[-3000:] or f"command failed: {argv[0]}")
        return completed

    def _verified_result(
        self, job_id: str
    ) -> tuple[Path, Path, DevelopmentRun]:
        job_dir = (self.jobs_root / job_id).resolve()
        if not job_dir.is_relative_to(self.jobs_root) or not job_dir.is_dir():
            raise FileNotFoundError("verified result job is unavailable")
        record = JobStore(job_dir).read()
        if record.status != JobStatus.COMPLETE or not record.result_package:
            raise RuntimeError("only a completed verified result can be previewed")
        package = (job_dir / record.result_package).resolve()
        if not package.is_relative_to(job_dir) or not package.is_dir():
            raise RuntimeError("verified result package is unavailable")
        run_path = package / "artifacts" / "development" / "development_run.json"
        run = DevelopmentRun.model_validate_json(run_path.read_text(encoding="utf-8"))
        if run.status != "verified":
            raise RuntimeError("development result did not pass verification")
        changed = package / "artifacts" / "development" / "changed_files"
        return job_dir, changed, run

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    @staticmethod
    def _responding(url: str, timeout: float = 0.8) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if not 200 <= response.status < 400:
                    return False
                html = response.read().decode("utf-8", errors="replace")
            scripts = list(dict.fromkeys(re.findall(r"""(?:src=["']|["'])(/assets/[^"']+\.js[^"']*)""", html)))
            for source in scripts[:3]:
                asset_url = urllib.parse.urljoin(url, source)
                with urllib.request.urlopen(asset_url, timeout=timeout) as asset:
                    if not 200 <= asset.status < 400:
                        return False
            with urllib.request.urlopen(urllib.parse.urljoin(url, "/fx-data.json"), timeout=timeout) as data:
                return 200 <= data.status < 400
        except Exception:
            return False

    @staticmethod
    def _attach_dependencies(source_root: Path, preview_repo: Path) -> None:
        source = source_root / "web" / "node_modules"
        target = preview_repo / "web" / "node_modules"
        if not source.is_dir() or target.exists():
            return
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(target), str(source)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("approved web dependencies could not be attached")
        else:
            target.symlink_to(source, target_is_directory=True)

    def _prepare_repository(
        self,
        preview_dir: Path,
        changed_dir: Path,
        run: DevelopmentRun,
    ) -> Path:
        repository = preview_dir / "delivery" / "program"
        marker = preview_dir / "prepared.json"
        expected = {
            "base_head_sha": run.base_head_sha,
            "changed_paths": run.changed_paths,
        }
        if marker.is_file() and repository.is_dir():
            try:
                if json.loads(marker.read_text(encoding="utf-8")) == expected:
                    return repository
            except (OSError, json.JSONDecodeError):
                pass
        if preview_dir.exists():
            shutil.rmtree(preview_dir)
        preview_dir.mkdir(parents=True)
        self._run(
            ["git", "clone", "--local", "--no-hardlinks", str(self.source_root), str(repository)],
            preview_dir,
            timeout=120,
        )
        self._run(["git", "checkout", "--detach", run.base_head_sha], repository, timeout=60)
        for relative in run.changed_paths:
            approved = approved_edit_path(relative)
            if approved is None:
                raise PermissionError(f"preview path is outside the approved source area: {relative}")
            pure = PurePosixPath(approved)
            source = (changed_dir / Path(*pure.parts)).resolve()
            target = (repository / Path(*pure.parts)).resolve()
            if (
                not source.is_relative_to(changed_dir.resolve())
                or not source.is_file()
                or not target.is_relative_to(repository)
            ):
                raise PermissionError(f"verified preview file is unavailable: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        self._attach_dependencies(self.source_root, repository)
        npm = "npm.cmd" if os.name == "nt" else "npm"
        build = self._run(
            [npm, "--prefix", "web", "run", "build"],
            repository,
            timeout=360,
        )
        (preview_dir / "build.log").write_text(
            build.stdout + "\n" + build.stderr, encoding="utf-8"
        )
        client_assets = repository / "web" / "dist" / "client" / "assets"
        public_assets = repository / "web" / "public" / "assets"
        if client_assets.is_dir():
            shutil.copytree(client_assets, public_assets, dirs_exist_ok=True)
        marker.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        return repository

    def start(self, job_id: str) -> PreviewReceipt:
        _, changed_dir, run = self._verified_result(job_id)
        preview_dir = self.previews_root / self.project.project_id / job_id
        state_path = preview_dir / "preview.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                url = str(state["preview_url"])
                if self._responding(url):
                    return PreviewReceipt.model_validate(state)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass

        repository = self._prepare_repository(preview_dir, changed_dir, run)
        node = shutil.which("node")
        vinext_cli = repository / "web" / "node_modules" / "vinext" / "dist" / "cli.js"
        if not node or not vinext_cli.is_file():
            raise RuntimeError("approved web runtime is unavailable")

        flags = 0
        popen_args: dict[str, object] = {}
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            popen_args["start_new_session"] = True

        render_port = self._available_port()
        render_url = f"http://127.0.0.1:{render_port}/"
        render_log_path = preview_dir / "render.log"
        render_argv = [
            node,
            str(vinext_cli),
            "start",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(render_port),
        ]
        with render_log_path.open("w", encoding="utf-8") as log:
            renderer = subprocess.Popen(
                render_argv,
                cwd=repository / "web",
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=flags,
                **popen_args,
            )
        snapshot = ""
        for _ in range(80):
            try:
                with urllib.request.urlopen(render_url, timeout=0.8) as response:
                    if 200 <= response.status < 400:
                        snapshot = response.read().decode("utf-8", errors="replace")
                        break
            except Exception:
                pass
            if renderer.poll() is not None:
                break
            time.sleep(0.25)
        if not snapshot:
            tail = render_log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            raise RuntimeError(f"verified web result could not be rendered: {tail}")
        renderer.terminate()
        try:
            renderer.wait(timeout=3)
        except subprocess.TimeoutExpired:
            renderer.kill()

        client_dir = repository / "web" / "dist" / "client"
        client_dir.mkdir(parents=True, exist_ok=True)
        (client_dir / "index.html").write_text(snapshot, encoding="utf-8")

        port = self._available_port()
        url = f"http://127.0.0.1:{port}/"
        delivery_dir = preview_dir / "delivery"
        delivery_dir.mkdir(parents=True, exist_ok=True)
        launcher = delivery_dir / "Exchange Flow \uc2e4\ud589\ud558\uae30.cmd"
        shortcut = delivery_dir / "Exchange Flow \ubc14\ub85c\uac00\uae30.url"
        python_executable = str(Path(sys.executable).resolve())
        launcher.write_text(
            "@echo off\r\n"
            f"start \"Exchange Flow\" /min \"{python_executable}\" -m http.server {port} "
            "--bind 127.0.0.1 --directory \"%~dp0program\\web\\dist\\client\"\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f"start \"\" {url}\r\n",
            encoding="utf-8",
        )
        shortcut.write_text(
            "[InternetShortcut]\r\n" f"URL={url}\r\n",
            encoding="utf-8",
        )

        log_path = preview_dir / "server.log"
        argv = [
            python_executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
            "--directory",
            str(client_dir),
        ]
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                argv,
                cwd=client_dir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=flags,
                **popen_args,
            )
        for _ in range(80):
            if self._responding(url):
                receipt = PreviewReceipt(
                    project_id=self.project.project_id,
                    job_id=job_id,
                    preview_url=url,
                    delivery_folder=str(delivery_dir),
                    launcher_path=str(launcher),
                    instructions=[
                        "This is a local preview of the verified result.",
                        "Use technical ZIP download only for source review and audit.",
                        "A public deployment URL requires a separate deployment approval.",
                    ],
                )
                state_path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
                return receipt
            if process.poll() is not None:
                break
            time.sleep(0.25)
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise RuntimeError(f"verified web preview did not start: {tail}")