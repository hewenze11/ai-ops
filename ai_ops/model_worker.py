"""Bounded in-process workers; role-turn reservation is serialized in SQLite."""
from concurrent.futures import ThreadPoolExecutor
import logging
import threading

log = logging.getLogger(__name__)


def start_model_workers(engine, client, workers=4):
    stop = threading.Event()

    def run():
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="role-model") as pool:
            futures = set()
            while not stop.is_set():
                for future in list(futures):
                    if future.done():
                        futures.remove(future)
                        try:
                            future.result()
                        except Exception as error:
                            log.warning("Role worker failed: %s", type(error).__name__)
                while len(futures) < workers and not stop.is_set():
                    futures.add(pool.submit(engine.advance, client))
                stop.wait(1)

    thread = threading.Thread(target=run, name="role-worker-supervisor", daemon=True)
    thread.start()
    return stop, thread
