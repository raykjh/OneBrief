"""Independent, browser-rendered evidence for approved local web applications."""

from __future__ import annotations

import hashlib
import html
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from onebrief.development_toolpack import DevelopmentCommandResult
from onebrief.reality_check import ObservationReceipt, ObservationStatus, RealityCapability


def _chrome() -> Path | None:
    configured = os.environ.get("ONEBRIEF_CHROME")
    candidates = [Path(configured)] if configured else []
    if os.name == "nt":
        candidates.extend([
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ])
    candidates.extend(Path(item) for item in filter(None, [shutil.which("google-chrome"), shutil.which("chromium")]))
    return next((item.resolve() for item in candidates if item.is_file()), None)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(url: str, process: subprocess.Popen[bytes]) -> None:
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError("approved web start script exited before observation")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("approved web start script did not expose a local HTTP page")


@contextmanager
def _observer_proxy(upstream: str, pages: dict[str, str]):
    """Expose trusted observer pages and the app through one loopback origin.

    Framework servers do not consistently serve files written into their build
    directory. A fixed local reverse proxy gives the observer iframe same-origin
    access without depending on a framework-specific static-file policy.
    """

    encoded = {path: content.encode("utf-8") for path, content in pages.items()}
    upstream = upstream.rstrip("/")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            observer = encoded.get(self.path.split("?", 1)[0])
            if observer is not None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(observer)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(observer)
                return
            try:
                with urllib.request.urlopen(upstream + self.path, timeout=10) as response:
                    payload = response.read(20_000_001)
                    if len(payload) > 20_000_000:
                        raise RuntimeError("observer proxy response exceeded 20 MB")
                    self.send_response(response.status)
                    content_type = response.headers.get("Content-Type")
                    if content_type:
                        self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)
            except Exception as exc:
                detail = str(exc).encode("utf-8", errors="replace")[:1000]
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(detail)))
                self.end_headers()
                self.wfile.write(detail)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _stop_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=15,
        )
    else:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _wrapper_v5(*, toggle: bool, viewport_width: int) -> str:
    mode = "true" if toggle else "false"
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><style>
html,body{{width:{viewport_width}px;max-width:{viewport_width}px;height:100%;margin:0;overflow:hidden}}iframe{{display:block;width:{viewport_width}px;height:100%;margin:0;border:0}}#result{{display:none}}
</style></head><body><iframe id=\"app\" src=\"/\"></iframe><pre id=\"result\"></pre><script>
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const frame=document.getElementById('app'), out=document.getElementById('result');
function sample(){{const d=frame.contentDocument, de=d.documentElement, b=d.body;
 const text=(b?.innerText||'').replace(/\\s+/g,' ').trim();
 const clipped=[...d.querySelectorAll('header,main,section,h1,h2,h3,p,a,button,img')].filter(el=>{{
  const r=el.getBoundingClientRect(),s=getComputedStyle(el);return s.display!=='none'&&s.visibility!=='hidden'&&r.width>1&&r.height>1&&(r.left < -2 || r.right > frame.contentWindow.innerWidth+2);
 }}).slice(0,20).map(el=>({{tag:el.tagName,id:el.id||'',className:String(el.className||'').slice(0,80),left:Math.round(el.getBoundingClientRect().left),right:Math.round(el.getBoundingClientRect().right)}}));
 return {{lang:de?.lang||'', text:text.slice(0,5000), horizontalOverflow:de.scrollWidth>de.clientWidth+2,
  images:[...d.images].map(i=>({{src:i.getAttribute('src')||'',complete:i.complete,width:i.naturalWidth,height:i.naturalHeight}})),
  replacement:text.includes('�'),clipped}};}}
frame.addEventListener('load',async()=>{{await delay(400);const before=sample();let clicked=false;
 if({mode}){{const d=frame.contentDocument;const buttons=[...d.querySelectorAll('button,[role=button]')];
  const current=(d.documentElement.lang||'').toLowerCase();
  const wanted=current.startsWith('ko')?/^(en|english)$/i:/^(ko|korean|한국어)$/i;
  const button=buttons.find(x=>wanted.test((x.textContent||'').trim())||wanted.test((x.getAttribute('aria-label')||'').trim()))||d.querySelector('#lang-toggle,[data-language-toggle]');
  if(button){{button.click();clicked=true;await delay(500);}}
  else {{const selects=[...d.querySelectorAll('select')];
   const select=selects.find(x=>[...x.options].some(o=>wanted.test((o.textContent||'').trim())||wanted.test((o.value||'').trim())));
   if(select){{const option=[...select.options].find(o=>wanted.test((o.textContent||'').trim())||wanted.test((o.value||'').trim()));
    select.value=option.value;select.dispatchEvent(new Event('input',{{bubbles:true}}));select.dispatchEvent(new Event('change',{{bubbles:true}}));clicked=true;await delay(500);}}}}}}
 const after=sample();out.textContent=JSON.stringify({{clicked,before,after}});document.body.dataset.done='true';}});
</script></body></html>"""


def _wrapper(*, toggle: bool, viewport_width: int) -> str:
    """Build a same-origin observer that exercises every advertised locale."""
    mode = "true" if toggle else "false"
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><style>
html,body{{width:{viewport_width}px;max-width:{viewport_width}px;height:100%;margin:0;overflow:hidden}}iframe{{display:block;width:{viewport_width}px;height:100%;margin:0;border:0}}#result{{display:none}}
</style></head><body><iframe id=\"app\" src=\"about:blank\"></iframe><pre id=\"result\"></pre><script>
const delay=ms=>new Promise(r=>setTimeout(r,ms));
const frame=document.getElementById('app'),out=document.getElementById('result');
function sample(){{const d=frame.contentDocument,de=d.documentElement,b=d.body;
 const text=(b?.innerText||'').replace(/\\s+/g,' ').trim();
 const semanticText=((d.querySelector('main')||b)?.innerText||'').replace(/\\s+/g,' ').trim();
 const clipped=[...d.querySelectorAll('header,main,section,h1,h2,h3,p,a,button,img')].filter(el=>{{
  const r=el.getBoundingClientRect(),s=getComputedStyle(el);return s.display!=='none'&&s.visibility!=='hidden'&&r.width>1&&r.height>1&&(r.left < -2||r.right > frame.contentWindow.innerWidth+2);
 }}).slice(0,20).map(el=>({{tag:el.tagName,id:el.id||'',className:String(el.className||'').slice(0,80),left:Math.round(el.getBoundingClientRect().left),right:Math.round(el.getBoundingClientRect().right)}}));
 return {{lang:de?.lang||'',text:text.slice(0,5000),semanticText:semanticText.slice(0,5000),horizontalOverflow:de.scrollWidth>de.clientWidth+2,
  images:[...d.images].map(i=>({{src:i.getAttribute('src')||'',complete:i.complete,width:i.naturalWidth,height:i.naturalHeight}})),replacement:text.includes('\\uFFFD'),clipped}};}}
async function exerciseLanguages(){{const d=frame.contentDocument,states=[];
 const locale=/^(ko|en|ja|zh(?:[-_](?:cn|tw))?|es|fr|de|it|pt(?:[-_]br)?)(?:$|[-_])/i;
 const selects=[...d.querySelectorAll('select')],select=selects.find(x=>{{
  const identity=[x.id,x.name,x.getAttribute('aria-label'),x.getAttribute('data-language'),x.getAttribute('data-locale')].filter(Boolean).join(' ');
  return /(lang|language|locale)/i.test(identity)||[...x.options].filter(o=>locale.test(String(o.value||'').trim())).length>=2;
 }});
 if(select){{for(const option of [...select.options].filter(o=>locale.test(String(o.value||'').trim()))){{
  select.value=option.value;select.dispatchEvent(new Event('input',{{bubbles:true}}));select.dispatchEvent(new Event('change',{{bubbles:true}}));await delay(350);
  states.push({{control:'select',requested:String(option.value),label:(option.textContent||'').trim(),state:sample()}});
 }}return states;}}
 const buttons=[...d.querySelectorAll('button[data-language],button[data-lang],[role=button][data-language],[role=button][data-lang]')];
 for(const button of buttons){{button.click();await delay(350);states.push({{control:'button',requested:button.dataset.language||button.dataset.lang||'',label:(button.textContent||'').trim(),state:sample()}});}}
 if(!states.length){{const button=d.querySelector('#lang-toggle,[data-language-toggle]');if(button){{button.click();await delay(350);states.push({{control:'toggle',requested:'',label:(button.textContent||'').trim(),state:sample()}});}}}}
 return states;}}
frame.addEventListener('load',async()=>{{if(frame.contentWindow.location.href==='about:blank')return;await delay(400);const before=sample();const states={mode}?await exerciseLanguages():[];
 const after=states.length?states[states.length-1].state:sample();out.textContent=JSON.stringify({{clicked:states.length>0,before,after,states}});document.body.dataset.done='true';}});
frame.src='/';
</script></body></html>"""


def _normalized_locale(value: object) -> str:
    return str(value or "").strip().casefold().replace("_", "-")


def _language_state_issues(states: object) -> list[str]:
    """Validate every advertised locale, including obvious script leakage."""
    if not isinstance(states, list) or len(states) < 2:
        return ["Fewer than two advertised language states were exercised."]
    issues: list[str] = []
    observed_texts: set[str] = set()
    for item in states:
        if not isinstance(item, dict) or not isinstance(item.get("state"), dict):
            issues.append("A language control produced no observable rendered state.")
            continue
        state = item["state"]
        requested = _normalized_locale(item.get("requested"))
        actual = _normalized_locale(state.get("lang"))
        semantic = str(state.get("semanticText") or state.get("text") or "")
        if requested and not (
            actual == requested
            or actual.split("-", 1)[0] == requested.split("-", 1)[0]
        ):
            issues.append(
                f"Language control requested '{requested}' but the document reported '{actual or 'none'}'."
            )
        if not semantic.strip():
            issues.append(f"Language state '{actual or requested or 'unknown'}' rendered no semantic text.")
            continue
        observed_texts.add(semantic)
        locale = (actual or requested).split("-", 1)[0]
        # A parenthesized native product name such as ``JULPAE (줄패)`` is
        # intentional. Outside such labels, three foreign-script characters
        # are enough to expose short mixed-language fragments such as
        # ``의 카드`` and ``팀을``.
        prose = re.sub(r"\([^)]*\)", "", semantic)
        hangul_count = len(re.findall(r"[\uac00-\ud7af]", prose))
        kana_count = len(re.findall(r"[\u3040-\u30ff]", prose))
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", prose))
        if locale == "ja" and hangul_count >= 3:
            issues.append("The Japanese rendered content contains a Korean-script fragment.")
        elif locale == "zh" and (hangul_count >= 3 or kana_count >= 3):
            issues.append("The Chinese rendered content contains a Korean or Japanese-script fragment.")
        elif locale == "ko" and kana_count >= 3:
            issues.append("The Korean rendered content contains a Japanese-script fragment.")
        elif locale in {"en", "es", "fr", "de", "it", "pt"} and (
            hangul_count >= 3 or kana_count >= 3 or cjk_count >= 3
        ):
            issues.append(f"The {locale} rendered content contains an unexpected CJK-script fragment.")
    if len(observed_texts) < len(states):
        issues.append("At least two advertised languages rendered identical semantic content.")
    return issues


def validate_preserved_language_states(
    baseline_evidence_dir: Path, candidate_evidence_dir: Path
) -> None:
    """Reject unrelated visible-copy drift when the work contract says preserve.

    Script checks cannot distinguish English from Spanish because both use the
    Latin alphabet.  Comparing the rendered semantic state of every advertised
    locale against the approved Git baseline closes that gap without relying on
    a model's opinion.
    """

    def states(root: Path) -> dict[str, str]:
        payload = json.loads((root / "observation.json").read_text(encoding="utf-8"))
        observed = payload.get("mobile", {}).get("states", [])
        result: dict[str, str] = {}
        for item in observed if isinstance(observed, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("state"), dict):
                continue
            locale = _normalized_locale(item.get("requested"))
            text = str(item["state"].get("semanticText") or "").strip()
            if locale and text:
                result[locale] = text
        return result

    baseline = states(baseline_evidence_dir)
    candidate = states(candidate_evidence_dir)
    if not baseline or baseline.keys() != candidate.keys():
        raise RuntimeError(
            "web content preservation failed: advertised locale states changed"
        )
    changed = [locale for locale in baseline if baseline[locale] != candidate[locale]]
    if changed:
        raise RuntimeError(
            "web content preservation failed: existing visible locale copy changed: "
            + ", ".join(changed)
        )


def _extract(dom: str) -> dict[str, object]:
    match = re.search(r'<pre id="result">(.*?)</pre>', dom, re.DOTALL | re.IGNORECASE)
    if not match or not match.group(1).strip():
        raise RuntimeError("browser observer did not publish a result")
    return json.loads(html.unescape(match.group(1)))


def _capture(chrome: Path, url: str, screenshot: Path, width: int, height: int) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", f"--window-size={width},{height}",
            "--virtual-time-budget=2500", f"--screenshot={screenshot}", "--dump-dom", url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    # A valid sparse status page can compress below 5 KiB. DOM extraction below
    # remains authoritative for visible content; this threshold only rejects a
    # missing or effectively empty PNG.
    if completed.returncode != 0 or not screenshot.is_file() or screenshot.stat().st_size < 1_000:
        raise RuntimeError(
            "headless browser did not produce a usable rendered screenshot; "
            f"exit={completed.returncode}; stderr={completed.stderr.strip()[:1000] or 'empty'}; "
            f"stdout={completed.stdout.strip()[:1000] or 'empty'}"
        )
    try:
        return _extract(completed.stdout)
    except RuntimeError as exc:
        stderr = completed.stderr.strip().replace("\n", " ")[:1000]
        stdout = completed.stdout.strip().replace("\n", " ")[:2000]
        raise RuntimeError(
            f"{exc}; chrome_exit={completed.returncode}; "
            f"stderr={stderr or 'empty'}; dom={stdout or 'empty'}"
        ) from exc


def observe_web_application(
    clone: Path,
    evidence_dir: Path,
    *,
    application_subdir: str = ".",
    require_language_switch: bool = True,
) -> tuple[DevelopmentCommandResult, ObservationReceipt]:
    """Exercise one approved start script and issue pipeline-owned observation evidence."""

    chrome = _chrome()
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    application_root = (clone / application_subdir).resolve()
    if not application_root.is_relative_to(clone.resolve()):
        raise RuntimeError("approved web observation path left the project clone")
    package_path = application_root / "package.json"
    if chrome is None or npm is None or not package_path.is_file():
        raise RuntimeError("approved web observation requires Chrome, npm, and package.json")
    scripts = json.loads(package_path.read_text(encoding="utf-8")).get("scripts", {})
    if not isinstance(scripts, dict) or not isinstance(scripts.get("start"), str):
        raise RuntimeError("approved web observation requires a package.json start script")

    dist = application_root / "dist"
    if not dist.is_dir():
        raise RuntimeError("approved web observation requires a built dist directory")
    (dist / "__onebrief_desktop.html").write_text(
        _wrapper(toggle=False, viewport_width=1200), encoding="utf-8"
    )
    (dist / "__onebrief_mobile.html").write_text(
        _wrapper(toggle=True, viewport_width=375), encoding="utf-8"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    port = _port()
    env = {**os.environ, "PORT": str(port)}
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    started = time.perf_counter()
    server = subprocess.Popen(
        [npm, "run", "start"], cwd=application_root, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        _wait(base + "/", server)
        desktop_path = evidence_dir / "desktop-ko.png"
        mobile_path = evidence_dir / ("mobile-en.png" if require_language_switch else "mobile.png")
        with _observer_proxy(base, {
            "/__onebrief_desktop.html": _wrapper(toggle=False, viewport_width=1200),
            "/__onebrief_mobile.html": _wrapper(toggle=require_language_switch, viewport_width=375),
        }) as observer:
            desktop = _capture(
                chrome, observer + "/__onebrief_desktop.html", desktop_path, 1200, 900
            )
            mobile = _capture(
                chrome, observer + "/__onebrief_mobile.html", mobile_path, 375, 812
            )
    finally:
        _stop_tree(server)

    before = mobile.get("before", {})
    after = mobile.get("after", {})
    language_states = mobile.get("states", [])
    desktop_after = desktop.get("after", {})
    issues: list[str] = []
    if require_language_switch:
        if not mobile.get("clicked"):
            issues.append("No language control could be activated in the rendered page.")
        if str(before.get("text", "")) == str(after.get("text", "")):
            issues.append("Visible text did not change after the language control was activated.")
        if before.get("lang") == after.get("lang"):
            issues.append("The rendered document language did not change after activation.")
        issues.extend(_language_state_issues(language_states))
    for label, state in (("desktop", desktop_after), ("mobile", after)):
        if not str(state.get("text", "")).strip():
            issues.append(f"The {label} rendered page has no visible text.")
        if state.get("horizontalOverflow"):
            issues.append(f"The {label} rendered page has horizontal overflow.")
        if state.get("clipped"):
            issues.append(
                f"The {label} rendered page clips visible elements outside the viewport: "
                + json.dumps(state["clipped"], ensure_ascii=False)
            )
        if state.get("replacement"):
            issues.append(f"The {label} rendered page contains Unicode replacement characters.")
        images = state.get("images", [])
        if any(
            not item.get("complete") or not item.get("width")
            for item in images if isinstance(item, dict)
        ):
            issues.append(f"The {label} rendered page has missing or unloaded first-party images.")
    if hashlib.sha256(desktop_path.read_bytes()).digest() == hashlib.sha256(mobile_path.read_bytes()).digest():
        issues.append("Desktop and mobile rendered screenshots are identical.")

    summary = {
        "schema_version": "onebrief-web-observation-v1",
        "desktop": desktop,
        "mobile": mobile,
        "screenshots": [desktop_path.name, mobile_path.name],
        "issues": issues,
    }
    (evidence_dir / "observation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    receipt = ObservationReceipt(
        capability=RealityCapability.SEMANTIC_OBSERVATION,
        observer_pack_id="onebrief_web_ui_observer_v7",
        status=ObservationStatus.FAILED if issues else ObservationStatus.OBSERVED,
        independent_from_maker=True,
        artifact_paths=[desktop_path.as_posix(), mobile_path.as_posix()],
        findings=(issues or ([
            "Desktop and mobile pages rendered without horizontal overflow or missing images.",
            "Rendered content contained no Unicode replacement characters.",
        ] + ([
            "Every advertised language option was activated and produced matching, distinct semantic content."
        ] if require_language_switch else []))),
        limitations=[],
    )
    if issues:
        raise RuntimeError("web observation failed: " + " | ".join(issues))
    command = DevelopmentCommandResult(
        command_id="browser_http_visual_render",
        argv=["npm", "run", "start", "+", "headless Chrome observer"],
        exit_code=0,
        duration_seconds=round(time.perf_counter() - started, 3),
        output_tail=(
            "Rendered desktop and mobile pages and verified visible text, images, and overflow."
            + (" Exercised every advertised language state." if require_language_switch else "")
        ),
    )
    return command, receipt
