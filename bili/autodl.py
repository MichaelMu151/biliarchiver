from __future__ import annotations

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
WORKER_PORT = 6006
SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "data",
    "__pycache__",
    ".cursor",
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
        try:
            channel = transport.open_channel(
                "direct-tcpip",
                (self.chain_host, self.chain_port),
                self.request.getpeername(),
            )
        except Exception:
            return
        if channel is None:
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
        transport.set_keepalive(30)
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
    output = _run(
        client,
        "command -v python3 || command -v python || true",
        log,
        timeout=30,
        check=False,
        chatter=False,
        get_pty=False,
    )
    binary = ""
    for line in output.splitlines():
        candidate = line.strip()
        if candidate.startswith("/") or candidate in {"python3", "python"}:
            binary = candidate.split()[0]
            break
    if not binary:
        raise RuntimeError("这台 AutoDL 里找不到 python3。请换一个 PyTorch 镜像。")
    log(f"远程 Python：{binary}", "ok")
    return binary


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
        emit("正在安装 faster-whisper / OCR（第一次会几分钟）…", "info")
        quoted_dir = shlex.quote(remote_dir)
        _run(
            client,
            f"cd {quoted_dir} && {shlex.quote(python)} -m pip install -r requirements-ai.txt",
            emit,
            timeout=1800,
        )
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
        emit(f"正在启动 GPU 工作机（端口 6006，模型 {model or 'large-v3'}）…", "info")
        token_q = shlex.quote(token.strip())
        model_q = shlex.quote(model or "large-v3")
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
            (
                f"cd {quoted_dir} && PYTHONUNBUFFERED=1 setsid nohup {shlex.quote(python)} "
                f"tools/gpu_worker.py --host 0.0.0.0 --port {WORKER_PORT} --token {token_q} "
                f"--model {model_q} </dev/null >/tmp/bili_gpu_worker.log 2>&1 & echo $!"
            ),
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

        ok, message = gpu_worker_ready(f"http://127.0.0.1:{local_port}", token.strip(), timeout=8.0)
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
