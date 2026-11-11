"""Замены упавших исполнителей получают работу только после передачи коллекции."""
from types import SimpleNamespace

from conftest import pytest_xdist_make_scheduler


class Worker:
    def __init__(self):
        self.gateway = SimpleNamespace(id="test-worker")
        self.shutting_down = False
        self.sent = []

    def send_runtest_some(self, indices):
        self.sent.extend(indices)

    def shutdown(self):
        self.shutting_down = True


def test_replacement_collection_and_another_crash_can_arrive_in_either_order():
    config = SimpleNamespace(getoption=lambda name: "loadgroup", getvalue=lambda name: ["2*popen"],
                             option=SimpleNamespace(loadscopereorder=True))
    scheduler = pytest_xdist_make_scheduler(config, None)
    collection = [f"case.py::test_{n}" for n in range(8)]
    first, second, waiting, collected = [Worker() for _ in range(4)]
    for node in (first, second):
        scheduler.add_node(node)
        scheduler.add_node_collection(node, collection)
    scheduler.schedule()
    crashed = {scheduler.remove_node(first)}
    scheduler.add_node(waiting)
    # Второй процесс погиб, пока замена первого ещё собирает тесты.
    crashed.add(scheduler.remove_node(second))
    assert not waiting.sent and not waiting.shutting_down
    scheduler.add_node(collected)
    scheduler.add_node_collection(collected, collection)
    scheduler.schedule()
    assert collected.sent and not waiting.sent and not waiting.shutting_down
    scheduler.add_node_collection(waiting, collection)
    scheduler.schedule()
    scheduled = {collection[index] for node in (waiting, collected) for index in node.sent}
    pending = {nid for unit in scheduler.workqueue.values() for nid, done in unit.items() if not done}
    assert scheduled | pending == set(collection) - crashed
    assert not scheduled & crashed
