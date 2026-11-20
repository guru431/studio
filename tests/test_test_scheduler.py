"""Замены упавших исполнителей получают работу только после передачи коллекции."""
from types import SimpleNamespace

import pytest

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
    collection = [f"case_{n // 2}.py::test_{n}" for n in range(8)]
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


def test_files_stay_together_and_explicit_groups_span_files():
    config = SimpleNamespace(getoption=lambda name: "loadgroup", getvalue=lambda name: ["2*popen"],
                             option=SimpleNamespace(loadscopereorder=True))
    scheduler = pytest_xdist_make_scheduler(config, None)
    collection = ["alpha.py::test_one", "alpha.py::test_two[email@example.com]",
                  "beta.py::test_one", "beta.py::test_two",
                  "ui_one.py::test_one@serial", "ui_two.py::test_two@serial",
                  "docs_one.py::test_one@docs", "docs_two.py::test_two@docs"]
    nodes = [Worker(), Worker()]
    for node in nodes:
        scheduler.add_node(node)
        scheduler.add_node_collection(node, collection)
    scheduler.schedule()
    for group in ({0, 1}, {2, 3}, {4, 5}, {6, 7}):
        assert any(group <= set(node.sent) for node in nodes)
    assert scheduler._split_scope(collection[1]) == "alpha.py"


def test_integration_files_run_in_at_most_lanes_processes():
    """Файлы integration — по одному целиком, не больше `LANES` процессов сразу; остальные процессы берут
    быстрые файлы и завершаются, когда им подходящей работы нет; каждый тест выдан ровно один раз."""
    from conftest import LANES
    config = SimpleNamespace(getoption=lambda name: "loadgroup", getvalue=lambda name: ["6*popen"],
                             option=SimpleNamespace(loadscopereorder=True))
    scheduler = pytest_xdist_make_scheduler(config, None)
    lanes = [f"ui_{n}.py::test_{k}@lane:ui_{n}.py" for n in range(6) for k in range(3)]
    fast = [f"fast_{n}.py::test_{k}" for n in range(4) for k in range(2)]
    collection = lanes + fast + ["live.py::test_card@serial"]
    nodes = [Worker() for _ in range(6)]
    for node in nodes:
        scheduler.add_node(node)
        scheduler.add_node_collection(node, collection)
    scheduler.schedule()
    done = {node: 0 for node in nodes}
    while any(done[node] < len(node.sent) for node in nodes):
        for node in nodes:
            if done[node] < len(node.sent):
                scheduler.mark_test_complete(node, node.sent[done[node]])
                done[node] += 1
    sent = sorted(index for node in nodes for index in node.sent)
    assert sent == list(range(len(collection)))
    lane_nodes = [node for node in nodes if any(collection[i].split("@")[-1].startswith(("lane:", "serial"))
                                                  for i in node.sent)]
    assert len(lane_nodes) == LANES
    for node in lane_nodes:                     # файл — целиком в одном процессе
        files = {collection[i].split("::")[0] for i in node.sent if "@" in collection[i]}
        assert all(not any(f in collection[i] for i in other.sent) for f in files
                   for other in lane_nodes if other is not node)
    assert all(node.shutting_down for node in nodes)


@pytest.mark.xdist_group("probe")
def test_xdist_group_reaches_the_test_id(request):
    """pytest 9.2 хранит id в `_id`: суффикс группы xdist (`_nodeid`) иначе терялся, и `serial`/`lane:`
    не доходили до планировщика — integration шёл во всех процессах."""
    if getattr(request.config.option, "loadgroup", False):
        assert request.node.nodeid.endswith("@probe")
