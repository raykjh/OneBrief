"""Authority-bounded greenfield web application scaffold and runtime verifier."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from onebrief.development_toolpack import (
    DevelopmentCommandResult,
    DevelopmentRun,
    RepositoryContextFile,
    RepositoryInspection,
    _default_runner,
)
from onebrief.generic_development_toolpack import ProjectCodeChangeSet, generic_safe_relative
from onebrief.schemas import InternalSource, SourcePriority
from onebrief.web_runtime_evidence import observe_web_application


_SCAFFOLD = {
    "package.json": json.dumps({
        "name": "onebrief-greenfield-web",
        "private": True,
        "type": "module",
        "scripts": {
            "test": "node --test tests/*.test.mjs",
            "build": "node scripts/build.mjs",
            "start": "node scripts/server.mjs",
            "verify:http": "node scripts/verify-http.mjs",
        },
    }, indent=2) + "\n",
    "scripts/build.mjs": """import {cp, rm} from 'node:fs/promises';\nawait rm('dist',{recursive:true,force:true});\nawait cp('public','dist',{recursive:true});\n""",
    "scripts/server.mjs": """import http from 'node:http';import {readFile} from 'node:fs/promises';import {extname,join} from 'node:path';\nconst root=new URL('../dist/',import.meta.url);const port=Number(process.env.PORT||8080);\nhttp.createServer(async(req,res)=>{try{const rel=req.url==='/'?'index.html':req.url.slice(1).split('?')[0];const data=await readFile(new URL(rel,root));const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.json':'application/json'};res.writeHead(200,{'content-type':types[extname(rel)]||'application/octet-stream'});res.end(data)}catch{res.writeHead(404);res.end('Not found')}}).listen(port,'127.0.0.1');\n""",
    "scripts/verify-http.mjs": """import {spawn} from 'node:child_process';const port=18765;const child=spawn(process.execPath,['scripts/server.mjs'],{env:{...process.env,PORT:String(port)}});\n+try{for(let i=0;i<30;i++){try{const r=await fetch(`http://127.0.0.1:${port}/`);if(r.ok&&(await r.text()).includes('<main'))process.exitCode=0;else throw Error('invalid page');break}catch{await new Promise(r=>setTimeout(r,100))}}if(process.exitCode!==0)throw Error('HTTP page unavailable')}finally{child.kill()}\n""".replace("\n+", "\n"),
    "tests/scaffold.test.mjs": """import test from 'node:test';import assert from 'node:assert/strict';import {readFile} from 'node:fs/promises';\ntest('product exposes an accessible main application',async()=>{const html=await readFile('public/index.html','utf8');assert.match(html,/<main[\\s>]/);assert.match(html,/<meta[^>]+viewport/);assert.doesNotMatch(html,/�/)});\n""",
    "public/index.html": """<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>OneBrief App</title><link rel=\"stylesheet\" href=\"/styles.css\"></head><body><main id=\"app\"><h1>OneBrief App</h1><p>요구사항에 맞게 이 화면을 완성하세요.</p></main><script type=\"module\" src=\"/app.js\"></script></body></html>\n""",
    "public/styles.css": """*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#f5f7fb;color:#172033}main{max-width:72rem;margin:auto;padding:2rem}@media(max-width:600px){main{padding:1rem}}\n""",
    "public/app.js": """document.documentElement.dataset.ready='true';\n""",
    "README.md": "# OneBrief verified web application\n\nRun `npm test`, `npm run build`, then `npm start`.\n",
}


class GreenfieldWebDevelopmentToolPack:
    """Creates only a disposable seed repository and returns a verified package."""

    source_prefix = "greenfield-source/"

    def __init__(self, root: Path, runner=None):
        self.root = root.resolve()
        self.runner = runner or _default_runner
        self._ensure_scaffold()

    def _ensure_scaffold(self) -> None:
        if (self.root / ".git").is_dir():
            return
        self.root.mkdir(parents=True, exist_ok=True)
        for relative, content in _SCAFFOLD.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "onebrief@local.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "OneBrief Scaffold"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "Seed bounded web scaffold"], cwd=self.root, check=True)

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError((result.stdout + result.stderr)[-4000:])
        return result.stdout.strip()

    def approved_edit_path(self, value: str) -> str | None:
        try:
            path = generic_safe_relative(value).as_posix()
        except ValueError:
            return None
        if path in {"public/index.html", "public/styles.css", "public/app.js", "README.md"}:
            return path
        if re.fullmatch(r"tests/product[-_a-z0-9]*\.test\.mjs", path, re.IGNORECASE):
            return path
        return None

    def inspect(self, output_dir: Path) -> tuple[RepositoryInspection, list[InternalSource]]:
        head = self._git("rev-parse", "HEAD")
        records=[]; sources=[]
        for relative in sorted(_SCAFFOLD):
            raw = (self.root / relative).read_bytes()
            content = raw.decode("utf-8")
            packaged = output_dir / "repository_context" / relative
            packaged.parent.mkdir(parents=True, exist_ok=True)
            packaged.write_text(content, encoding="utf-8", newline="\n")
            digest=hashlib.sha256(raw).hexdigest()
            records.append(RepositoryContextFile(path=relative,size_bytes=len(raw),sha256=digest))
            sources.append(InternalSource(name=self.source_prefix+relative,priority=SourcePriority.MANDATORY,requirement_keys=["greenfield_web_development"],summary="Fixed scaffold or editable product source.",content=content,size_bytes=len(raw),sha256=digest))
        inspection=RepositoryInspection(repository_name="onebrief-greenfield-web",head_sha=head,context_files=records,safety_boundary=["disposable scaffold only","fixed dependency-free build/server adapters","no deployment, credentials, package installation, or external side effects"])
        output_dir.mkdir(parents=True,exist_ok=True)
        (output_dir/"repository_inspection.json").write_text(inspection.model_dump_json(indent=2)+"\n",encoding="utf-8")
        return inspection,sources

    def apply_and_verify(self, change_set: ProjectCodeChangeSet, output_dir: Path, verification_goal: str="") -> DevelopmentRun:
        head=self._git("rev-parse","HEAD")
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="onebrief_greenfield_") as temporary:
            clone=Path(temporary)/"repository"
            self._git(
                "-c", "core.autocrlf=false", "clone", "--local", "--no-hardlinks",
                str(self.root), str(clone), cwd=Path(temporary)
            )
            new_paths=[]
            for change in change_set.changes:
                approved=self.approved_edit_path(change.path)
                if approved is None: raise PermissionError(f"path is outside greenfield product area: {change.path}")
                target=clone/Path(*PurePosixPath(approved).parts)
                if target.exists():
                    actual=hashlib.sha256(target.read_bytes()).hexdigest()
                    if change.base_sha256 != actual: raise RuntimeError(f"stale or missing base hash: {approved}")
                elif change.base_sha256 is not None: raise RuntimeError(f"new file cannot declare a base hash: {approved}")
                else: new_paths.append(approved)
                target.parent.mkdir(parents=True,exist_ok=True);target.write_text(change.content,encoding="utf-8",newline="\n")
            if new_paths:self._git("add","-N","--",*new_paths,cwd=clone)
            npm="npm.cmd" if __import__('os').name=="nt" else "npm"
            commands=[self.runner("greenfield_tests",[npm,"test"],clone,120),self.runner("greenfield_build",[npm,"run","build"],clone,120),self.runner("greenfield_http",[npm,"run","verify:http"],clone,120)]
            language=bool(re.search(r"language|locale|translation|다국어|언어|번역",verification_goal,re.IGNORECASE))
            evidence_dir=output_dir/"web_observation_evidence"
            observation,receipt=observe_web_application(clone,evidence_dir,require_language_switch=language)
            commands.append(observation)
            patch=self._git("diff","--binary","--no-ext-diff",cwd=clone)
            (output_dir/"changes.patch").write_text(patch,encoding="utf-8",newline="\n")
            changed_dir=output_dir/"changed_files"
            for change in change_set.changes:
                destination=changed_dir/Path(*PurePosixPath(change.path).parts);destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(clone/change.path,destination)
            (output_dir.parent/"independent_observations").mkdir(parents=True,exist_ok=True)
            (output_dir.parent/"independent_observations"/"web_ui_observation.json").write_text(receipt.model_dump_json(indent=2)+"\n",encoding="utf-8")
            run=DevelopmentRun(status="verified",repository_name="onebrief-greenfield-web",base_head_sha=head,summary=change_set.summary,changed_paths=[c.path for c in change_set.changes],commands=commands,patch_path=(output_dir/"changes.patch").relative_to(output_dir.parent).as_posix(),evidence_paths=[evidence_dir.relative_to(output_dir.parent).as_posix()],safety_boundary=["all code generated in a disposable scaffold","only approved product paths were writable","fixed tests, build, HTTP probe, and headless browser observation passed","no deployment or credential access"])
            (output_dir/"development_run.json").write_text(run.model_dump_json(indent=2)+"\n",encoding="utf-8")
            (output_dir/"change_set.json").write_text(change_set.model_dump_json(indent=2)+"\n",encoding="utf-8")
            return run
