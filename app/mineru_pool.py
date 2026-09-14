"""常驻 mineru-api 工作进程池。

每次转换都新起 `mineru` 进程会导致模型冷启动（约 10s）。这里维护若干个
常驻的 `mineru-api` 服务：模型加载一次后常驻内存，转换时用 `--api-url`
复用即可。

为了让进度条仍能精确归属到某个任务，池中每个 worker 同一时刻只服务一次
转换，并读取该 worker 的 stderr（tqdm 由服务端进程输出），交给
`_ProgressTracker` 解析。
"""

import asyncio
import os
import sys
import re
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

from app import config


def _minimal_pdf() -> bytes:
    """生成一张空白页 PDF，用于把 worker 的模型预热加载。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def _warm_worker(worker: "MineruWorker", timeout: float = 180.0) -> None:
    """跑一张空白页把 worker 的 pipeline 模型加载进内存（结果丢弃）。"""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "warmup.pdf"
        pdf.write_bytes(_minimal_pdf())
        cmd = [
            "mineru", "-p", str(pdf), "-o", str(Path(tmp) / "out"),
            "--backend", config.MINERU_BACKEND, "-m", "ocr",
            "--api-url", worker.url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()


class MineruWorker:
    """单个常驻 mineru-api 进程。"""

    def __init__(self, port: int):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._tracker = None
        self._on_progress: Optional[Callable[[dict], None]] = None
        self._log = None

    async def start(self) -> None:
        env = os.environ.copy()
        env.setdefault("MINERU_API_DISABLE_ACCESS_LOG", "1")
        self.proc = await asyncio.create_subprocess_exec(
            "mineru-api", "--host", "127.0.0.1", "--port", str(self.port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._reader = asyncio.create_task(self._read_stderr())

    async def wait_ready(self, timeout: float = 90.0) -> bool:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self.proc is None or self.proc.returncode is not None:
                return False
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                await asyncio.sleep(0.5)
        return False

    def bind(self, tracker, on_progress: Callable[[dict], None], log_file) -> None:
        self._tracker = tracker
        self._on_progress = on_progress
        self._log = log_file

    def unbind(self) -> None:
        self._tracker = None
        self._on_progress = None
        self._log = None

    async def _read_stderr(self) -> None:
        while True:
            chunk = await self.proc.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode(errors="replace")
            if self._log is not None:
                try:
                    self._log.write(text)
                    self._log.flush()
                except ValueError:
                    pass
            tracker, on_progress = self._tracker, self._on_progress
            if tracker is None or on_progress is None:
                continue
            for seg in re.split(r"[\r\n]+", text):
                p = tracker.feed(seg)
                if p:
                    on_progress(p)

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()


class MineruPool:
    """固定大小的 worker 池，队列容量即最大并发数。"""

    def __init__(self, size: int, port_base: int, worker_factory=MineruWorker, warmup: bool = True):
        self.size = max(1, size)
        self.port_base = port_base
        self._worker_factory = worker_factory
        self._warmup = warmup
        self._workers: list[MineruWorker] = []
        self._queue: asyncio.Queue[MineruWorker] = asyncio.Queue()
        self._warmup_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._workers = [self._worker_factory(self.port_base + i) for i in range(self.size)]
        for worker in self._workers:
            await worker.start()
        ready = await asyncio.gather(*(w.wait_ready() for w in self._workers))
        if not all(ready):
            await self.stop()
            raise RuntimeError("mineru-api worker 启动超时")
        if self._warmup:
            # 后台预热：模型加载完成后 worker 才进入队列，期间到来的转换显示排队
            self._warmup_task = asyncio.create_task(self._warmup_and_publish())
        else:
            for worker in self._workers:
                self._queue.put_nowait(worker)

    async def _warmup_and_publish(self) -> None:
        try:
            await asyncio.gather(
                *(_warm_worker(w) for w in self._workers),
                return_exceptions=True,
            )
            print(f"[pool] {len(self._workers)} 个 mineru-api worker 预热完成", file=sys.stderr, flush=True)
        except Exception as e:  # pragma: no cover - 预热失败也不影响可用性
            print(f"[warn] mineru 预热失败: {e}", file=sys.stderr, flush=True)
        finally:
            for worker in self._workers:
                self._queue.put_nowait(worker)

    async def acquire(self) -> MineruWorker:
        return await self._queue.get()

    def release(self, worker: MineruWorker) -> None:
        self._queue.put_nowait(worker)

    async def stop(self) -> None:
        if self._warmup_task is not None:
            self._warmup_task.cancel()
            try:
                await self._warmup_task
            except asyncio.CancelledError:
                pass
            self._warmup_task = None
        for worker in self._workers:
            await worker.stop()
        self._workers = []
        self._queue = asyncio.Queue()


_pool: Optional[MineruPool] = None


def get_pool() -> Optional[MineruPool]:
    return _pool


async def start_pool() -> None:
    global _pool
    _pool = MineruPool(config.MINERU_MAX_CONCURRENCY, config.MINERU_API_PORT_BASE)
    await _pool.start()


async def stop_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.stop()
        _pool = None
