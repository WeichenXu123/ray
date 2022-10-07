from collections import defaultdict
import sys
import os

import pytest

import ray

from ray._private.metrics_agent import RAY_WORKER_TIMEOUT_S
from ray._private.test_utils import (
    raw_metrics,
    run_string_as_driver,
    run_string_as_driver_nonblocking,
    wait_for_condition,
)


METRIC_CONFIG = {
    "_system_config": {
        "metrics_report_interval_ms": 100,
    }
}

SLOW_METRIC_CONFIG = {
    "_system_config": {
        "metrics_report_interval_ms": 3000,
    }
}


def tasks_by_state(info) -> dict:
    return tasks_breakdown(info, lambda s: s.labels["State"])


def tasks_by_name_and_state(info) -> dict:
    return tasks_breakdown(info, lambda s: (s.labels["Name"], s.labels["State"]))


def tasks_breakdown(info, key_fn) -> dict:
    res = raw_metrics(info)
    if "ray_tasks" in res:
        breakdown = defaultdict(int)
        for sample in res["ray_tasks"]:
            key = key_fn(sample)
            breakdown[key] += sample.value
            if breakdown[key] == 0:
                del breakdown[key]
        print("Task label breakdown: {}".format(breakdown))
        return breakdown
    else:
        return {}


# TODO(ekl) in all these tests, we use run_string_as_driver_nonblocking to work around
# stats reporting issues if Ray is repeatedly restarted in unit tests.
def test_task_basic(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote
def f():
    time.sleep(999)
a = [f.remote() for _ in range(10)]
ray.get(a)
"""
    proc = run_string_as_driver_nonblocking(driver)

    expected = {
        "RUNNING": 2.0,
        "PENDING_NODE_ASSIGNMENT": 8.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    assert tasks_by_name_and_state(info) == {
        ("f", "RUNNING"): 2.0,
        ("f", "PENDING_NODE_ASSIGNMENT"): 8.0,
    }
    proc.kill()


def test_task_job_ids(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote(num_cpus=0)
def f():
    time.sleep(999)
a = [f.remote() for _ in range(1)]
ray.get(a)
"""
    procs = [run_string_as_driver_nonblocking(driver) for _ in range(3)]
    expected = {
        "RUNNING": 3.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )

    # Check we have three jobs reporting "RUNNING".
    metrics = raw_metrics(info)
    jobs_at_state = defaultdict(set)
    for sample in metrics["ray_tasks"]:
        jobs_at_state[sample.labels["State"]].add(sample.labels["JobId"])
    print("Jobs at state: {}".format(jobs_at_state))
    assert len(jobs_at_state["RUNNING"]) == 3, jobs_at_state

    for proc in procs:
        proc.kill()


def test_task_nested(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote(num_cpus=0)
def wrapper():
    @ray.remote
    def f():
        time.sleep(999)

    ray.get([f.remote() for _ in range(10)])

w = wrapper.remote()
ray.get(w)
"""
    proc = run_string_as_driver_nonblocking(driver)

    expected = {
        "RUNNING": 2.0,
        "RUNNING_IN_RAY_GET": 1.0,
        "PENDING_NODE_ASSIGNMENT": 8.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=2000
    )
    assert tasks_by_name_and_state(info) == {
        ("wrapper", "RUNNING_IN_RAY_GET"): 1.0,
        ("f", "RUNNING"): 2.0,
        ("f", "PENDING_NODE_ASSIGNMENT"): 8.0,
    }
    proc.kill()


def test_task_nested_wait(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote(num_cpus=0)
def wrapper():
    @ray.remote
    def f():
        time.sleep(999)

    ray.wait([f.remote() for _ in range(10)])

w = wrapper.remote()
ray.get(w)
"""
    proc = run_string_as_driver_nonblocking(driver)

    expected = {
        "RUNNING": 2.0,
        "RUNNING_IN_RAY_WAIT": 1.0,
        "PENDING_NODE_ASSIGNMENT": 8.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=2000
    )
    assert tasks_by_name_and_state(info) == {
        ("wrapper", "RUNNING_IN_RAY_WAIT"): 1.0,
        ("f", "RUNNING"): 2.0,
        ("f", "PENDING_NODE_ASSIGNMENT"): 8.0,
    }
    proc.kill()


def test_task_wait_on_deps(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote
def f():
    time.sleep(999)

@ray.remote
def g(x):
    time.sleep(999)

x = f.remote()
a = [g.remote(x) for _ in range(5)]
ray.get(a)
"""
    proc = run_string_as_driver_nonblocking(driver)
    expected = {
        "RUNNING": 1.0,
        "PENDING_ARGS_AVAIL": 5.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    assert tasks_by_name_and_state(info) == {
        ("f", "RUNNING"): 1.0,
        ("g", "PENDING_ARGS_AVAIL"): 5.0,
    }
    proc.kill()


def test_actor_tasks_queued(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote
class F:
    def f(self):
        time.sleep(999)

    def g(self):
        pass

a = F.remote()
[a.g.remote() for _ in range(10)]
[a.f.remote() for _ in range(1)]  # Further tasks should be blocked on this one.
z = [a.g.remote() for _ in range(9)]
ray.get(z)
"""
    proc = run_string_as_driver_nonblocking(driver)
    expected = {
        "RUNNING": 1.0,
        "SUBMITTED_TO_WORKER": 9.0,
        "FINISHED": 11.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    assert tasks_by_name_and_state(info) == {
        ("F.__init__", "FINISHED"): 1.0,
        ("F.g", "FINISHED"): 10.0,
        ("F.f", "RUNNING"): 1.0,
        ("F.g", "SUBMITTED_TO_WORKER"): 9.0,
    }
    proc.kill()


def test_task_finish(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote
def f():
    return "ok"

@ray.remote
def g():
    assert False

f.remote()
g.remote()
time.sleep(999)
"""

    proc = run_string_as_driver_nonblocking(driver)
    expected = {
        "FAILED": 1.0,
        "FINISHED": 1.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    assert tasks_by_name_and_state(info) == {
        ("g", "FAILED"): 1.0,
        ("f", "FINISHED"): 1.0,
    }
    proc.kill()


def test_task_retry(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote(retry_exceptions=True)
def f():
    assert False

f.remote()
time.sleep(999)
"""

    proc = run_string_as_driver_nonblocking(driver)
    expected = {
        "FAILED": 1.0,  # Only recorded as finished once.
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    proc.kill()


def test_task_failure(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import time
import os

ray.init("auto")

@ray.remote
def f():
    print("RUNNING FAILING TASK")
    os._exit(1)

@ray.remote
def g():
    assert False

f.remote()
g.remote()
time.sleep(999)
"""

    proc = run_string_as_driver_nonblocking(driver)
    expected = {
        "FAILED": 2.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    proc.kill()


def test_concurrent_actor_tasks(shutdown_only):
    info = ray.init(num_cpus=2, **METRIC_CONFIG)

    driver = """
import ray
import asyncio

ray.init("auto")

@ray.remote(max_concurrency=30)
class A:
    async def f(self):
        await asyncio.sleep(300)

a = A.remote()
ray.get([a.f.remote() for _ in range(40)])
"""

    proc = run_string_as_driver_nonblocking(driver)
    expected = {
        "RUNNING": 30.0,
        "SUBMITTED_TO_WORKER": 10.0,
        "FINISHED": 1.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    proc.kill()


@pytest.mark.skipif(sys.platform == "win32", reason="Flaky on Windows.")
def test_metrics_export_now(shutdown_only):
    info = ray.init(num_cpus=2, **SLOW_METRIC_CONFIG)

    driver = """
import ray
import time

ray.init("auto")

@ray.remote
def f():
    pass
a = [f.remote() for _ in range(10)]
ray.get(a)
"""

    # If force export at process death is broken, we won't see the recently completed
    # tasks from the drivers.
    for i in range(10):
        print("Run job", i)
        run_string_as_driver(driver)
        tasks_by_state(info)

    expected = {
        "FINISHED": 100.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )


@pytest.mark.skipif(sys.platform == "darwin", reason="Flaky on macos")
def test_pull_manager_stats(shutdown_only):
    info = ray.init(num_cpus=2, object_store_memory=100_000_000, **METRIC_CONFIG)

    driver = """
import ray
import time
import numpy as np

ray.init("auto")

# Spill a lot of 10MiB objects. The object store is 100MiB, so pull manager will
# only be able to pull ~9 total into memory at once, including running tasks.
buf = []
for _ in range(100):
    buf.append(ray.put(np.ones(10 * 1024 * 1024, dtype=np.uint8)))

@ray.remote
def f(x):
    time.sleep(999)

ray.get([f.remote(x) for x in buf])"""

    proc = run_string_as_driver_nonblocking(driver)
    expected = {
        "RUNNING": 2.0,
        "PENDING_ARGS_FETCH": 7.0,
        "PENDING_OBJ_STORE_MEM_AVAIL": 91.0,
    }
    wait_for_condition(
        lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
    )
    assert tasks_by_name_and_state(info) == {
        ("f", "RUNNING"): 2.0,
        ("f", "PENDING_ARGS_FETCH"): 7.0,
        ("f", "PENDING_OBJ_STORE_MEM_AVAIL"): 91.0,
    }
    proc.kill()


def test_stale_view_cleanup_when_job_exits(monkeypatch, shutdown_only):
    with monkeypatch.context() as m:
        m.setenv(RAY_WORKER_TIMEOUT_S, 5)
        info = ray.init(num_cpus=2, **METRIC_CONFIG)
        print(info)

        driver = """
import ray
import time
import numpy as np

ray.init("auto")

@ray.remote
def g():
    time.sleep(999)

ray.get(g.remote())
    """

        proc = run_string_as_driver_nonblocking(driver)
        expected = {
            "RUNNING": 1.0,
        }
        wait_for_condition(
            lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
        )

        proc.kill()
        print("Killing a driver.")
        expected = {}
        wait_for_condition(
            lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
        )


def test_stale_view_cleanup_when_worker_exits_complicated(monkeypatch, shutdown_only):
    with monkeypatch.context() as m:
        m.setenv(RAY_WORKER_TIMEOUT_S, 5)
        info = ray.init(num_cpus=6, **METRIC_CONFIG)
        print(info)

        driver = """
import ray
import time
import numpy as np

ray.init("auto")

@ray.remote
def g(sleep_s):
    time.sleep(sleep_s)

@ray.remote
class Actor:
    def submit(self, sleep_s):
        ray.get(g.remote(sleep_s))

a = Actor.remote()
b = Actor.remote()

a_ref = a.submit.remote(5)
b_ref = b.submit.remote(999)

ray.get(a_ref)
ray.kill(a)
ray.get(b_ref)
    """

        # 2 actor creation tasks + 2 tasks owned by actors + 2 nested tasks.
        proc = run_string_as_driver_nonblocking(driver)
        expected = {"RUNNING": 1.0, "RUNNING_IN_RAY_GET": 1.0, "FINISHED": 4.0}
        wait_for_condition(
            lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
        )
        print("The first actor is killed.")

        # The first actor is killed. One owned nested task from a dead actor
        # should stop reporting, so FINISHED: 4 -> 3.
        expected = {"RUNNING": 1.0, "RUNNING_IN_RAY_GET": 1.0, "FINISHED": 3.0}
        wait_for_condition(
            lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
        )

        proc.kill()
        print("Killing a driver.")
        expected = {}
        wait_for_condition(
            lambda: tasks_by_state(info) == expected, timeout=20, retry_interval_ms=500
        )


if __name__ == "__main__":
    import sys

    if os.environ.get("PARALLEL_CI"):
        sys.exit(pytest.main(["-n", "auto", "--boxed", "-vs", __file__]))
    else:
        sys.exit(pytest.main(["-sv", __file__]))
