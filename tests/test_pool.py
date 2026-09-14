import asyncio

from app.mineru_pool import MineruPool


class FakeWorker:
    def __init__(self, port):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.bound = None
        self.running = False

    async def start(self):
        self.running = True

    async def wait_ready(self, timeout=90):
        return True

    def bind(self, tracker, on_progress, log_file):
        self.bound = (tracker, on_progress, log_file)

    def unbind(self):
        self.bound = None

    async def stop(self):
        self.running = False


def test_pool_ports_and_worker_count():
    async def run():
        pool = MineruPool(3, 52000, worker_factory=FakeWorker, warmup=False)
        await pool.start()
        assert [w.port for w in pool._workers] == [52000, 52001, 52002]
        assert all(w.running for w in pool._workers)
        await pool.stop()
        assert pool._workers == []

    asyncio.run(run())


def test_pool_caps_concurrency_and_reuses_workers():
    async def run():
        pool = MineruPool(2, 52000, worker_factory=FakeWorker, warmup=False)
        await pool.start()

        first = await pool.acquire()
        second = await pool.acquire()
        assert first is not second

        third = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0.05)
        assert not third.done()  # 池已空，第三个必须等待

        pool.release(first)
        same = await asyncio.wait_for(third, timeout=1)
        assert same is first  # 释放后被复用

        await pool.stop()

    asyncio.run(run())


def test_pool_start_fails_and_cleans_up_when_not_ready():
    class NeverReady(FakeWorker):
        async def wait_ready(self, timeout=90):
            return False

    async def run():
        pool = MineruPool(2, 52000, worker_factory=NeverReady, warmup=False)
        try:
            await pool.start()
            assert False, "should raise"
        except RuntimeError:
            pass
        assert pool._workers == []

    asyncio.run(run())


def test_pool_publishes_workers_only_after_warmup(monkeypatch):
    import app.mineru_pool as mp

    warmed = []

    async def fake_warm(worker, timeout=180):
        await asyncio.sleep(0.2)
        warmed.append(worker.port)

    monkeypatch.setattr(mp, "_warm_worker", fake_warm)

    async def run():
        pool = MineruPool(2, 52000, worker_factory=FakeWorker, warmup=True)
        await pool.start()

        acquire_task = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0.05)
        assert not acquire_task.done()  # 预热完成前不可获取

        await pool._warmup_task
        worker = await asyncio.wait_for(acquire_task, timeout=1)
        assert worker.port in warmed
        await pool.stop()

    asyncio.run(run())
