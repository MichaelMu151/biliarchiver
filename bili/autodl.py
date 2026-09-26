from __future__ import annotations

import json
import re
import select
import shlex
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Callable

from bili.paths import ROOT

LogFn = Callable[[str, str], None]

DEFAULT_REMOTE_DIR = "/root/autodl-tmp/biliarchiver"
REMOTE_INBOX = f"{DEFAULT_REMOTE_DIR}/inbox"
WORKER_PORT = 6006
HF_MIRROR = "https://hf-mirror.com"
HF_HOME_REMOTE = "/root/autodl-tmp/huggingface"
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    ".venv-py314.bak",
    "venv",
    "data",
    "__pycache__",
    ".cursor",
    ".vscode",
    "node_modules",
    "canvases",
    ".pytest_cache",
}
SKIP_FILE_NAMES = {".ds_store", ".env"}
SKIP_SUFFIXES = {".pyc", ".pyo"}


class SshTarget:
    def __init__(self, user: str, host: str, port: int) -> None:
        self.user = user
        self.host = host
        self.port = port

    def label(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


def parse_ssh_command(text: str) -> SshTarget:
    raw = (text or "").strip().replace("\u00a0", " ")
    if not raw:
        raise ValueError("请粘贴 AutoDL 实例页的 SSH 登录指令")
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise ValueError("SSH 指令格式不对，请整行复制实例页那条 ssh -p …") from exc
    if parts and parts[0] != "ssh":
        parts = ["ssh", *parts]
    port = 22
    user = "root"
    host = ""
    i = 1
    while i < len(parts):
        item = parts[i]
        if item == "-p" and i + 1 < len(parts):
            port = _as_port(parts[i + 1])
            i += 2
            continue
        if item.startswith("-p") and item[2:].isdigit():
            port = _as_port(item[2:])
            i += 1
            continue
        if item in {"-o", "-i", "-l", "-F", "-E"} and i + 1 < len(parts):
            if item == "-l":
                user = parts[i + 1]
            i += 2
            continue
        if item.startswith("-"):
            i += 1
            continue
        if "@" in item:
            left, right = item.split("@", 1)
            user = left or user
            host = right
            i += 1
            continue
        if not host:
            host = item
        i += 1
    if not host:
        raise ValueError("指令里没有主机名。请复制类似 ssh -p 12345 root@region-9.seetacloud.com")
    if port < 1:
        raise ValueError("SSH 端口无效")
    return SshTarget(user=user or "root", host=host, port=port)


def _as_port(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("SSH 端口必须是数字，不要把「你的SSH端口」这类占位符贴进来") from exc


def _should_skip(path: Path) -> bool:
    if path.name.lower() in SKIP_FILE_NAMES:
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    parts = set(path.parts)
    if parts & SKIP_DIR_NAMES:
        return True
    if any(part.startswith(".venv") for part in path.parts):
        return True
    if "tools" in path.parts and "bin" in path.parts:
        return True
    return False


def iter_upload_files(root: Path | None = None) -> list[Path]:
    base = root or ROOT
    files: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        if _should_skip(rel):
            continue
        files.append(path)
    return files


class _TunnelHandler(socketserver.BaseRequestHandler):
    chain_host = "127.0.0.1"
    chain_port = WORKER_PORT
    ssh_transport = None

    def handle(self) -> None:
        transport = self.ssh_transport
        if transport is None:
            return
        channel = None
        for _ in range(8):
            try:
                channel = transport.open_channel(
                    "direct-tcpip",
                    (self.chain_host, self.chain_port),
                    self.request.getpeername(),
                )
            except Exception:
                channel = None
            if channel is not None:
                break
            time.sleep(0.25)
        if channel is None:
            try:
                self.request.close()
            except Exception:
                pass
            return
        try:
            while True:
                readable, _, _ = select.select([self.request, channel], [], [], 60)
                if not readable:
                    continue
                if self.request in readable:
                    data = self.request.recv(32768)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in readable:
                    data = channel.recv(32768)
                    if not data:
                        break
                    self.request.sendall(data)
        finally:
            channel.close()
            self.request.close()


class _TunnelServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AutodlSession:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.client = None
        self.server: _TunnelServer | None = None
        self.thread: threading.Thread | None = None
        self.target = ""
        self.local_port = 0
        self.remote_dir = DEFAULT_REMOTE_DIR

    def status(self) -> dict[str, Any]:
        alive = bool(self.client and self.client.get_transport() and self.client.get_transport().is_active())
        return {
            "connected": alive and self.local_port > 0,
            "target": self.target,
            "local_port": self.local_port,
            "url": f"http://127.0.0.1:{self.local_port}" if self.local_port else "",
            "remote_dir": self.remote_dir,
        }

    def close(self) -> None:
        with self.lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:
                pass
            try:
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        self.thread = None
        self.local_port = 0
        self.target = ""

    def attach(self, client: Any, target: str, local_port: int, remote_dir: str) -> None:
        with self.lock:
            self._close_locked()
            self.client = client
            self.target = target
            self.local_port = local_port
            self.remote_dir = remote_dir
            transport = client.get_transport()

            class Handler(_TunnelHandler):
                chain_host = "127.0.0.1"
                chain_port = WORKER_PORT
                ssh_transport = transport

            server = _TunnelServer(("127.0.0.1", local_port), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.server = server
            self.thread = thread


SESSION = AutodlSession()


def remote_inbox_path(local_path: Path | str) -> str:
    path = Path(local_path)
    suffix = path.suffix or ".m4a"
    stem = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in path.stem)[:80]
    return f"{REMOTE_INBOX}/bili_whisper_{stem or 'audio'}{suffix}"


def session_alive() -> bool:
    client = SESSION.client
    transport = client.get_transport() if client else None
    return bool(transport and transport.is_active())


def _open_sftp(client: Any, timeout: float = 20.0):
    box: dict[str, Any] = {}
    done = threading.Event()

    def worker() -> None:
        try:
            box["sftp"] = client.open_sftp()
        except Exception as exc:
            box["exc"] = exc
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    if not done.wait(timeout):
        raise RuntimeError("打开 SFTP 超时")
    if "exc" in box:
        raise RuntimeError(f"打开 SFTP 失败：{box['exc']}") from box["exc"]
    return box["sftp"]


def put_via_client(
    client: Any,
    local_path: Path | str,
    log: LogFn | None = None,
    stall_sec: float = 45.0,
) -> str:
    """Copy a file over a live SSH client. Used by the exclusive GPU path."""
    path = Path(local_path)
    if not path.is_file():
        raise RuntimeError(f"本地文件不存在：{path}")
    if client is None:
        raise RuntimeError("没有 SSH 连接")

    def emit(message: str, level: str = "info") -> None:
        if log:
            log(message, level)

    remote = remote_inbox_path(path)
    total = path.stat().st_size
    last_beat = [time.time()]
    last_logged = [0]
    box: dict[str, Any] = {}

    def callback(sent: int, _tot: int) -> None:
        last_beat[0] = time.time()
        if sent - last_logged[0] >= 2 * 1024 * 1024 or sent >= total:
            last_logged[0] = sent
            emit(f"SFTP {sent // 1048576}/{max(total // 1048576, 1)} MB · {path.name}")

    sftp = _open_sftp(client)
    try:
        _sftp_mkdirs(sftp, REMOTE_INBOX)
        chan = sftp.get_channel()
        chan.settimeout(max(stall_sec, 30.0))
        done = threading.Event()

        def worker() -> None:
            try:
                sftp.put(str(path), remote, callback=callback)
            except Exception as exc:
                box["exc"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, daemon=True).start()
        while not done.wait(1.0):
            if time.time() - last_beat[0] > stall_sec:
                try:
                    chan.close()
                except Exception:
                    pass
                raise RuntimeError(f"SFTP 卡住（{int(stall_sec)}s 无进度），已中止 {path.name}")
        if "exc" in box:
            raise RuntimeError(f"SFTP 失败：{box['exc']}") from box["exc"]
        st = sftp.stat(remote)
        if int(getattr(st, "st_size", 0) or 0) != total:
            raise RuntimeError(f"远程文件大小不符：{getattr(st, 'st_size', 0)} != {total}")
    finally:
        try:
            sftp.close()
        except Exception:
            pass
    emit(f"SFTP 完成 {path.name} → {remote}（{total} 字节）", "ok")
    return remote


def put_via_session(
    local_path: Path | str,
    log: LogFn | None = None,
    stall_sec: float = 45.0,
) -> str:
    """Copy a file over the already-open AutoDL SSH. Do not open a second login."""
    if not session_alive():
        raise RuntimeError("AutoDL 未接入，无法走已有 SSH 传文件")
    return put_via_client(SESSION.client, local_path, log=log, stall_sec=stall_sec)


def connect_exclusive(log: LogFn) -> Any:
    """One SSH for GPU-side download + Whisper. Call after disconnecting the local tunnel."""
    from bili.settings import load_settings

    settings = load_settings()
    ssh_command = (settings.autodl_ssh_command or "").strip()
    password = (settings.autodl_ssh_password or "").strip()
    if not ssh_command or not password:
        raise RuntimeError("没有 AutoDL SSH")
    target = parse_ssh_command(ssh_command)
    return _connect_client(target, password, log)


def ssh_alive(client: Any) -> bool:
    try:
        transport = client.get_transport() if client else None
        return bool(transport and transport.is_active())
    except Exception:
        return False


def ssh_nvidia_smi(client: Any) -> str:
    try:
        text = _run(
            client,
            "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits",
            lambda *_a: None,
            timeout=15,
            check=False,
            chatter=False,
            get_pty=False,
        )
        return (text or "").strip().splitlines()[-1].strip()
    except Exception:
        return ""


def ssh_transcribe_audio(
    client: Any,
    local_path: Path | str,
    token: str,
    model: str,
    language: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """SFTP on this SSH, then curl the GPU worker on localhost so the tunnel is unused."""

    def emit(message: str, level: str = "info") -> None:
        if log:
            log(message, level)

    path = Path(local_path)
    remote = put_via_client(client, path, log=log)
    emit(f"GPU 本机开始转写 {path.name}", "info")
    smi = ssh_nvidia_smi(client)
    if smi:
        emit(f"转写前 nvidia-smi {smi}", "info")
    auth = (token or "").strip().replace("'", "")
    cmd = (
        "curl -sS --max-time 1200 "
        f"-H 'Authorization: Bearer {auth}' "
        f"-F 'path={remote}' -F 'model={model or 'large-v3'}' -F 'language={language or 'auto'}' "
        f"http://127.0.0.1:{WORKER_PORT}/v1/transcribe_path"
    )
    out = _run(client, cmd, emit if log else (lambda *_a: None), timeout=1260, chatter=False, get_pty=False)
    text = (out or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"远程转写没有返回 JSON：{text[:240]}")
    data = json.loads(text[start : end + 1])
    if data.get("detail") and not data.get("markdown"):
        raise RuntimeError(str(data.get("detail")))
    if not data.get("markdown"):
        raise RuntimeError("云端转写没有返回文字")
    smi_after = ssh_nvidia_smi(client)
    if smi_after:
        emit(f"转写后 nvidia-smi {smi_after}", "ok")
    return data


REMOTE_PULL_PY = r"""
import json
import os
import subprocess
import sys
import urllib.request

job_path = sys.argv[1]
with open(job_path, encoding="utf-8") as fh:
    job = json.load(fh)
dest = job["dest"]
os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
raw = dest + ".part"
err = "download failed"
ok = False
for url in job.get("urls") or []:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": job.get("ua") or "Mozilla/5.0",
            "Referer": job.get("referer") or "https://www.bilibili.com/",
            "Cookie": job.get("cookie") or "",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp, open(raw, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
        if os.path.isfile(raw) and os.path.getsize(raw) > 1000:
            ok = True
            break
    except Exception as exc:
        err = str(exc)
        continue
if not ok:
    print(json.dumps({"ok": False, "error": err[:300]}))
    raise SystemExit(2)
ff = subprocess.run(
    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", raw, "-vn", "-c:a", "copy", dest],
    capture_output=True,
    text=True,
)
if ff.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) < 1000:
    ff = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", raw, "-vn", "-ac", "1", "-ar", "16000", dest],
        capture_output=True,
        text=True,
    )
if ff.returncode != 0:
    print(json.dumps({"ok": False, "error": (ff.stderr or "ffmpeg failed")[-300:]}))
    raise SystemExit(3)
try:
    os.remove(raw)
except OSError:
    pass
model = job.get("model") or "large-v3"
language = job.get("language") or "auto"
curl = subprocess.run(
    [
        "curl", "-sS", "--max-time", "1200",
        "-H", "Authorization: Bearer " + (job.get("token") or ""),
        "-F", "path=" + dest,
        "-F", "model=" + model,
        "-F", "language=" + language,
        "http://127.0.0.1:6006/v1/transcribe_path",
    ],
    capture_output=True,
    text=True,
)
sys.stdout.write(curl.stdout or "")
if curl.returncode != 0:
    sys.stderr.write((curl.stderr or curl.stdout or "curl failed")[-400:])
    raise SystemExit(curl.returncode or 4)
"""


def _put_text(client: Any, remote: str, text: str, timeout: int = 30) -> None:
    cmd = f"mkdir -p $(dirname {shlex.quote(remote)}) && cat > {shlex.quote(remote)}"
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout, get_pty=False)
    try:
        stdin.write(text)
    except TypeError:
        stdin.write(text.encode("utf-8"))
    stdin.flush()
    stdin.channel.shutdown_write()
    code = stdout.channel.recv_exit_status()
    if code != 0:
        err = stderr.read().decode("utf-8", "replace")[:200]
        raise RuntimeError("写入远程文件失败" + (f"：{err}" if err else ""))


def _parse_remote_json(text: str) -> dict[str, Any]:
    blob = (text or "").strip()
    start = blob.find("{")
    end = blob.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"远程转写没有返回 JSON：{blob[:240]}")
    data = json.loads(blob[start : end + 1])
    if data.get("ok") is False:
        raise RuntimeError(str(data.get("error") or "远程任务失败"))
    if data.get("detail") and not data.get("markdown"):
        raise RuntimeError(str(data.get("detail")))
    if not data.get("markdown"):
        raise RuntimeError("云端转写没有返回文字")
    return data


def ssh_fetch_and_transcribe(
    client: Any,
    *,
    bvid: str,
    urls: list[str],
    referer: str,
    cookie: str,
    token: str,
    model: str,
    language: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """GPU box downloads audio from Bilibili, then Whisper locally. No Mac upload."""
    from bili.client import UA

    def emit(message: str, level: str = "info") -> None:
        if log:
            log(message, level)

    clean_urls = [url for url in urls if url]
    if not clean_urls:
        raise RuntimeError("没有音频地址")
    remote_audio = f"{REMOTE_INBOX}/bili_whisper_{bvid}.m4a"
    job = {
        "urls": clean_urls,
        "referer": referer or f"https://www.bilibili.com/video/{bvid}",
        "cookie": cookie or "",
        "dest": remote_audio,
        "ua": UA,
        "token": token,
        "model": model or "large-v3",
        "language": language or "auto",
    }
    emit(f"GPU 从 B 站拉音频 {bvid}（{len(clean_urls)} 个地址）")
    smi = ssh_nvidia_smi(client)
    if smi:
        emit(f"转写前 nvidia-smi {smi}")
    _put_text(client, "/tmp/bili_pull_transcribe.py", REMOTE_PULL_PY)
    _put_text(client, "/tmp/bili_pull_job.json", json.dumps(job, ensure_ascii=False))
    python = _pick_python(client, emit if log else (lambda *_a: None))
    out = _run(
        client,
        f"{shlex.quote(python)} -u /tmp/bili_pull_transcribe.py /tmp/bili_pull_job.json",
        emit if log else (lambda *_a: None),
        timeout=1500,
        chatter=False,
        get_pty=False,
    )
    data = _parse_remote_json(out)
    smi_after = ssh_nvidia_smi(client)
    if smi_after:
        emit(f"转写后 nvidia-smi {smi_after}", "ok")
    return data


def fetch_and_transcribe_via_session(
    *,
    bvid: str,
    urls: list[str],
    referer: str,
    cookie: str,
    token: str,
    model: str,
    language: str,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """GPU pulls audio from Bilibili over the existing AutoDL SSH. No Mac SFTP."""
    if not session_alive():
        raise RuntimeError("AutoDL 未接入，无法让 GPU 直拉音频")
    return ssh_fetch_and_transcribe(
        SESSION.client,
        bvid=bvid,
        urls=urls,
        referer=referer,
        cookie=cookie,
        token=token,
        model=model,
        language=language,
        log=log,
    )


def _require_paramiko():
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("本机还缺 paramiko：在项目目录执行 .venv/bin/pip install paramiko") from exc
    return paramiko


def _connect_client(target: SshTarget, password: str, log: LogFn):
    paramiko = _require_paramiko()
    log(f"正在 SSH 连接 {target.label()} …", "info")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=target.host,
            port=target.port,
            username=target.user,
            password=password,
            timeout=20,
            allow_agent=False,
            look_for_keys=False,
            banner_timeout=20,
            auth_timeout=20,
        )
    except Exception as exc:
        message = str(exc)
        if "Authentication" in message or "auth" in message.lower():
            raise RuntimeError("SSH 密码不对。请用 AutoDL 实例页「SSH 登录指令」旁边显示的密码。") from exc
        raise RuntimeError(f"SSH 连不上：{message}") from exc
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(5)
    log("SSH 已接通。", "ok")
    return client


def _sftp_mkdirs(sftp: Any, remote_dir: str) -> None:
    parts = [part for part in remote_dir.split("/") if part]
    cursor = ""
    for part in parts:
        cursor += "/" + part
        try:
            sftp.stat(cursor)
        except OSError:
            sftp.mkdir(cursor)


def _upload(sftp: Any, remote_dir: str, log: LogFn) -> int:
    files = iter_upload_files()
    log(f"开始同步代码，共 {len(files)} 个文件 → {remote_dir}", "info")
    _sftp_mkdirs(sftp, remote_dir)
    sent = 0
    for index, path in enumerate(files, start=1):
        rel = path.relative_to(ROOT).as_posix()
        remote_path = f"{remote_dir.rstrip('/')}/{rel}"
        parent = remote_path.rsplit("/", 1)[0]
        _sftp_mkdirs(sftp, parent)
        skip = False
        try:
            remote_stat = sftp.stat(remote_path)
            if remote_stat.st_size == path.stat().st_size:
                skip = True
        except OSError:
            skip = False
        if not skip:
            sftp.put(str(path), remote_path)
            sent += 1
        if index == 1 or index % 40 == 0 or index == len(files):
            log(f"同步进度 {index}/{len(files)}", "info")
    log(f"代码同步完成，实际上传 {sent} 个文件。", "ok")
    return sent


def _run(
    client: Any,
    command: str,
    log: LogFn,
    timeout: int = 1800,
    check: bool = True,
    chatter: bool = True,
    get_pty: bool = True,
) -> str:
    stdin, stdout, stderr = client.exec_command(command, get_pty=get_pty, timeout=timeout)
    channel = stdout.channel
    channel.settimeout(1.0)
    chunks: list[str] = []
    deadline = time.time() + timeout
    while True:
        if time.time() > deadline:
            raise RuntimeError(f"远程命令超时：{command[:120]}")
        if channel.exit_status_ready() and not channel.recv_ready():
            break
        try:
            data = channel.recv(4096)
        except socket.timeout:
            continue
        if not data:
            if channel.exit_status_ready():
                break
            continue
        text = data.decode("utf-8", "replace")
        chunks.append(text)
        if chatter:
            for line in text.splitlines():
                cleaned = line.strip()
                if cleaned:
                    log(cleaned[:400], "info")
    code = channel.recv_exit_status()
    output = "".join(chunks)
    if check and code != 0:
        err = stderr.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"远程命令失败（{code}）{(': ' + err) if err else ''}")
    return output


def _pick_python(client: Any, log: LogFn) -> str:
    """AutoDL PyTorch images often hide python in conda, not a login PATH."""
    lookup = r"""
set +e
candidates=""
for cmd in python3 python; do
  found=$(command -v "$cmd" 2>/dev/null || true)
  if [ -n "$found" ]; then candidates="$candidates $found"; fi
done
for p in \
  /root/miniconda3/bin/python \
  /root/miniconda3/bin/python3 \
  /opt/conda/bin/python \
  /opt/conda/bin/python3 \
  /usr/bin/python3 \
  /usr/local/bin/python3 \
  /root/anaconda3/bin/python \
  /root/anaconda3/bin/python3
do
  if [ -x "$p" ]; then candidates="$candidates $p"; fi
done
if [ -d /root/miniconda3/envs ]; then
  for p in /root/miniconda3/envs/*/bin/python /root/miniconda3/envs/*/bin/python3; do
    if [ -x "$p" ]; then candidates="$candidates $p"; fi
  done
fi
printf '%s\n' $candidates | awk 'NF && !seen[$0]++'
"""
    output = _run(client, lookup, log, timeout=30, check=False, chatter=False, get_pty=False)
    binary = ""
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("/") and "python" in candidate.lower():
            binary = candidate.split()[0]
            break
        if candidate in {"python3", "python"}:
            binary = candidate
            break
    if not binary:
        raise RuntimeError(
            "这台 AutoDL 里找不到 python3。请在控制台选带 PyTorch / Miniconda 的官方镜像开机，"
            "不要选空白 Ubuntu。也可以打开 JupyterLab 看终端里 python 在哪。"
        )
    log(f"远程 Python：{binary}", "ok")
    return binary


def parse_nvidia_smi_header(text: str) -> tuple[str, str]:
    """Return (driver, max_cuda) from ``nvidia-smi`` header, e.g. ('570.124.04', '12.8')."""
    driver = ""
    cuda = ""
    match = re.search(r"Driver Version:\s*([\d.]+)", text or "")
    if match:
        driver = match.group(1)
    match = re.search(r"CUDA Version:\s*([\d.]+)", text or "")
    if match:
        cuda = match.group(1)
    return driver, cuda


def _cuda_tuple(raw: str) -> tuple[int, ...]:
    parts = []
    for item in (raw or "").split("."):
        if not item.isdigit():
            break
        parts.append(int(item))
    return tuple(parts) if parts else (0,)


def _log_remote_cuda(client: Any, log: LogFn) -> None:
    """Explain host driver vs image toolkit. AutoDL cards do not share one driver."""
    smi = _run(client, "nvidia-smi", log, timeout=20, check=False, chatter=False, get_pty=False)
    driver, max_cuda = parse_nvidia_smi_header(smi)
    image = _run(
        client,
        "readlink -f /usr/local/cuda 2>/dev/null || true; nvcc --version 2>/dev/null | tail -n 1 || true",
        log,
        timeout=20,
        check=False,
        chatter=False,
        get_pty=False,
    )
    image_cuda = ""
    found = re.search(r"cuda-(\d+\.\d+)", image, re.I)
    if found:
        image_cuda = found.group(1)
    else:
        found = re.search(r"release\s+(\d+\.\d+)", image, re.I)
        if found:
            image_cuda = found.group(1)
    if driver and max_cuda:
        log(f"宿主机驱动 {driver}，最高支持 CUDA {max_cuda}（由这张卡所在的物理机决定，换卡会变）。", "info")
    if image_cuda:
        log(f"容器镜像里的 CUDA toolkit 是 {image_cuda}。", "info")
    if max_cuda and image_cuda and _cuda_tuple(image_cuda) > _cuda_tuple(max_cuda):
        log(
            f"镜像 CUDA {image_cuda} 高于驱动上限 {max_cuda}。请关机后换成 CUDA {max_cuda} 或更低的 PyTorch 官方镜像再开机。"
            "例如驱动写 CUDA 12.8 时，不要选 CUDA 13.0 镜像。",
            "warn",
        )
    elif max_cuda:
        major = _cuda_tuple(max_cuda)[0]
        if major >= 13:
            log("这张卡可以选 CUDA 12 或 13 的 PyTorch 镜像。Whisper 仍会使用 CUDA 12 的 cublas。", "ok")
        else:
            log(f"这张卡请选 CUDA 12.x 的 PyTorch 镜像（不要选 13.0）。当前驱动上限是 {max_cuda}。", "ok")


def _hf_exports() -> str:
    """AutoDL cannot reach huggingface.co; faster-whisper must use the China mirror."""
    return (
        f"export HF_ENDPOINT={shlex.quote(HF_MIRROR)}; "
        f"export HF_HOME={shlex.quote(HF_HOME_REMOTE)}; "
        "export HF_HUB_ENABLE_HF_TRANSFER=0; "
        "export HF_HUB_DISABLE_XET=1; "
        "export HF_HUB_DISABLE_TELEMETRY=1"
    )


def _cuda12_probe_command(python: str) -> str:
    return (
        f"{shlex.quote(python)} -c "
        + shlex.quote(
            "import glob,os,sys\n"
            "hits=glob.glob('/root/miniconda3/lib/python*/site-packages/nvidia/*/lib/libcublas.so.12*')"
            "+glob.glob('/usr/local/cuda*/lib64/libcublas.so.12*')\n"
            "print('CUBLAS12_OK' if hits else 'CUBLAS12_MISSING')\n"
            "print('\\n'.join(hits[:8]))\n"
        )
    )


def _ensure_cuda12_runtime(client: Any, python: str, log: LogFn) -> None:
    """ctranslate2 wheels need libcublas.so.12 even on CUDA 13 AutoDL images."""
    probe = _run(client, _cuda12_probe_command(python), log, timeout=30, check=False, chatter=False, get_pty=False)
    if "CUBLAS12_OK" in probe:
        log("已有 libcublas.so.12，跳过 CUDA 12 运行库安装。", "ok")
        return
    log("这台镜像是 CUDA 13。Whisper/ctranslate2 还要 libcublas.so.12，正在安装（约 300–500MB，只需一次）…", "info")
    packages = "nvidia-cublas-cu12==12.4.5.8 nvidia-cuda-nvrtc-cu12==12.4.127"
    try:
        _run(client, f"{shlex.quote(python)} -m pip install {packages}", log, timeout=1800)
    except Exception as exc:
        log(f"指定版本安装失败，改装最新 CUDA 12 运行库：{exc}", "warn")
        _run(
            client,
            f"{shlex.quote(python)} -m pip install nvidia-cublas-cu12 nvidia-cuda-nvrtc-cu12",
            log,
            timeout=1800,
        )
    again = _run(client, _cuda12_probe_command(python), log, timeout=30, check=False, chatter=False, get_pty=False)
    if "CUBLAS12_OK" not in again:
        raise RuntimeError(
            "装完仍找不到 libcublas.so.12。请换 AutoDL 的 PyTorch 官方镜像（CUDA 12.1/12.4 或 13.0 均可），不要选空白 Ubuntu。"
        )
    log("CUDA 12 cublas 已就绪。", "ok")


def _prefetch_whisper_model(client: Any, python: str, remote_dir: str, model: str, log: LogFn) -> None:
    size = model or "large-v3"
    log(f"正在用镜像 {HF_MIRROR} 预下载 Whisper {size}（约 3GB，只需一次）…", "info")
    quoted_dir = shlex.quote(remote_dir)
    command = (
        f"{_hf_exports()}; mkdir -p {shlex.quote(HF_HOME_REMOTE)}; "
        f"cd {quoted_dir} && {shlex.quote(python)} -c "
        + shlex.quote(
            f"from faster_whisper.utils import download_model; path = download_model({size!r}); print('MODEL_READY', path)"
        )
    )
    output = _run(client, command, log, timeout=1800, get_pty=False)
    if "MODEL_READY" not in output:
        raise RuntimeError("Whisper 模型没有下载成功。远程输出：\n" + output[-600:])
    log(f"Whisper {size} 已就绪。", "ok")


def _worker_launch_command(python: str, remote_dir: str, token: str, model: str) -> str:
    py = shlex.quote(python)
    directory = shlex.quote(remote_dir)
    token_q = shlex.quote(token.strip())
    model_q = shlex.quote(model or "large-v3")
    locator = py + " -c " + shlex.quote(
        "from bili.runtime import cuda_library_dirs; print(':'.join(cuda_library_dirs()))"
    )
    return (
        f"{_hf_exports()}; cd {directory} && "
        f'export LD_LIBRARY_PATH="$({locator}):$LD_LIBRARY_PATH" && '
        f"PYTHONUNBUFFERED=1 setsid nohup {py} tools/gpu_worker.py "
        f"--host 0.0.0.0 --port {WORKER_PORT} --token {token_q} --model {model_q} "
        f"</dev/null >/tmp/bili_gpu_worker.log 2>&1 & echo $!"
    )


def _pick_local_port(preferred: int = WORKER_PORT) -> int:
    for port in range(preferred, preferred + 30):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    raise RuntimeError("本机找不到空闲端口做隧道")


def connect_autodl(
    ssh_command: str,
    password: str,
    token: str,
    model: str = "large-v3",
    remote_dir: str = DEFAULT_REMOTE_DIR,
    log: LogFn | None = None,
) -> dict[str, Any]:
    def emit(message: str, level: str = "info") -> None:
        if log:
            log(message, level)

    if not (password or "").strip():
        raise ValueError("请填写 AutoDL SSH 密码")
    if not (token or "").strip():
        raise ValueError("缺少工作机 Token")
    target = parse_ssh_command(ssh_command)
    SESSION.close()
    client = _connect_client(target, password.strip(), emit)
    try:
        sftp = client.open_sftp()
        try:
            _upload(sftp, remote_dir, emit)
        finally:
            sftp.close()
        python = _pick_python(client, emit)
        _log_remote_cuda(client, emit)
        emit("正在安装 faster-whisper / OCR（第一次会几分钟）…", "info")
        quoted_dir = shlex.quote(remote_dir)
        _run(
            client,
            f"cd {quoted_dir} && {shlex.quote(python)} -m pip install -r requirements-ai.txt",
            emit,
            timeout=1800,
        )
        emit("正在安装 CUDA 12 运行库（ctranslate2 需要 libcublas.so.12；CUDA 13 镜像也适用）…", "info")
        _ensure_cuda12_runtime(client, python, emit)
        ffmpeg = _run(
            client,
            "command -v ffmpeg >/dev/null && echo ffmpeg_ok || echo ffmpeg_missing",
            emit,
            timeout=20,
            check=False,
            chatter=False,
            get_pty=False,
        )
        if "ffmpeg_missing" in ffmpeg:
            emit("远程没有 ffmpeg，正在安装…", "info")
            _run(
                client,
                "DEBIAN_FRONTEND=noninteractive apt-get update -qq && apt-get install -y -qq ffmpeg",
                emit,
                timeout=300,
            )
        try:
            _prefetch_whisper_model(client, python, remote_dir, model or "large-v3", emit)
        except Exception as exc:
            emit(f"预下载模型失败，工作机启动后第一次转写还会再试：{exc}", "warn")
        emit(f"正在启动 GPU 工作机（端口 6006，模型 {model or 'large-v3'}）…", "info")
        _run(
            client,
            "pkill -f 'tools/gpu_worker.py' >/dev/null 2>&1 || true",
            emit,
            timeout=20,
            check=False,
            chatter=False,
            get_pty=False,
        )
        _run(
            client,
            _worker_launch_command(python, remote_dir, token, model or "large-v3"),
            emit,
            timeout=30,
            get_pty=False,
        )
        emit("等待工作机就绪…", "info")
        ready = False
        auth = token.strip().replace("'", "")
        curl_cmd = (
            f"curl -sf -H 'Authorization: Bearer {auth}' "
            f"http://127.0.0.1:{WORKER_PORT}/health >/dev/null && echo HEALTH_OK || true"
        )
        py_cmd = (
            f"{shlex.quote(python)} -c "
            f"\"import urllib.request; r=urllib.request.Request('http://127.0.0.1:{WORKER_PORT}/health',"
            f"headers={{'Authorization':'Bearer {auth}'}}); urllib.request.urlopen(r,timeout=5).read(); print('HEALTH_OK')\""
        )
        for _ in range(40):
            probe = _run(client, curl_cmd, emit, timeout=20, check=False, chatter=False, get_pty=False)
            if "HEALTH_OK" not in probe:
                probe = _run(client, py_cmd, emit, timeout=20, check=False, chatter=False, get_pty=False)
            if "HEALTH_OK" in probe:
                ready = True
                break
            time.sleep(2)
        if not ready:
            tail = _run(
                client,
                "tail -n 40 /tmp/bili_gpu_worker.log || true",
                emit,
                timeout=20,
                check=False,
                get_pty=False,
            )
            raise RuntimeError("工作机没有在 6006 端口起来。远程日志：\n" + tail[-800:])
        local_port = _pick_local_port(WORKER_PORT)
        SESSION.attach(client, target.label(), local_port, remote_dir)
        emit(f"本机隧道已打开：http://127.0.0.1:{local_port} → 远程 6006", "ok")
        from bili.gpu_remote import gpu_worker_ready

        ok, message = False, ""
        for attempt in range(1, 9):
            time.sleep(0.4 if attempt == 1 else 1.0)
            ok, message = gpu_worker_ready(
                f"http://127.0.0.1:{local_port}", token.strip(), timeout=12.0
            )
            if ok:
                break
            if "reset" in message.lower() or "timed out" in message.lower() or "refused" in message.lower():
                emit(f"隧道探测 {attempt}/8：{message}", "info")
                continue
            break
        if not ok:
            raise RuntimeError("隧道已开，但本机还访问不到工作机：" + message)
        emit(message, "ok")
        return {
            "ok": True,
            "url": f"http://127.0.0.1:{local_port}",
            "target": target.label(),
            "remote_dir": remote_dir,
            "local_port": local_port,
        }
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise
