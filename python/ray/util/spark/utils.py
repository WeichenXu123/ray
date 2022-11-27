import subprocess
import os
import sys
import random
import math
import time
import fcntl
import threading


_MEMORY_BUFFER_OFFSET = 0.8
_HEAP_TO_SHARED_RATIO = 0.4


def is_in_databricks_runtime():
    return "DATABRICKS_RUNTIME_VERSION" in os.environ


class _NoDbutilsError(Exception):
    pass


def get_dbutils():
    try:
        import IPython

        ip_shell = IPython.get_ipython()
        if ip_shell is None:
            raise _NoDbutilsError
        return ip_shell.ns_table["user_global"]["dbutils"]
    except ImportError:
        raise _NoDbutilsError
    except KeyError:
        raise _NoDbutilsError


class ShellCommandException(Exception):
    @classmethod
    def from_completed_process(cls, process):
        lines = [
            f"Non-zero exit code: {process.returncode}",
            f"Command: {process.args}",
        ]
        if process.stdout:
            lines += [
                "",
                "STDOUT:",
                process.stdout,
            ]
        if process.stderr:
            lines += [
                "",
                "STDERR:",
                process.stderr,
            ]
        return cls("\n".join(lines))


def exec_cmd(
    cmd,
    *,
    throw_on_error=True,
    extra_env=None,
    capture_output=True,
    synchronous=True,
    stream_output=False,
    **kwargs,
):
    """
    A convenience wrapper of `subprocess.Popen` for running a command from a Python script.

    :param cmd: The command to run, as a list of strings.
    :param throw_on_error: If True, raises an Exception if the exit code of the program is nonzero.
    :param extra_env: Extra environment variables to be defined when running the child process.
                      If this argument is specified, `kwargs` cannot contain `env`.
    :param capture_output: If True, stdout and stderr will be captured and included in an exception
                           message on failure; if False, these streams won't be captured.
    :param synchronous: If True, wait for the command to complete and return a CompletedProcess
                        instance, If False, does not wait for the command to complete and return
                        a Popen instance, and ignore the `throw_on_error` argument.
    :param stream_output: If True, stream the command's stdout and stderr to `sys.stdout`
                          as a unified stream during execution.
                          If False, do not stream the command's stdout and stderr to `sys.stdout`.
    :param kwargs: Keyword arguments (except `text`) passed to `subprocess.Popen`.
    :return:  If synchronous is True, return a `subprocess.CompletedProcess` instance,
              otherwise return a Popen instance.
    """
    illegal_kwargs = set(kwargs.keys()).intersection({"text"})
    if illegal_kwargs:
        raise ValueError(f"`kwargs` cannot contain {list(illegal_kwargs)}")

    env = kwargs.pop("env", None)
    if extra_env is not None and env is not None:
        raise ValueError("`extra_env` and `env` cannot be used at the same time")

    if capture_output and stream_output:
        raise ValueError(
            "`capture_output=True` and `stream_output=True` cannot be specified at the same time"
        )

    env = env if extra_env is None else {**os.environ, **extra_env}

    # In Python < 3.8, `subprocess.Popen` doesn't accept a command containing path-like
    # objects (e.g. `["ls", pathlib.Path("abc")]`) on Windows. To avoid this issue,
    # stringify all elements in `cmd`. Note `str(pathlib.Path("abc"))` returns 'abc'.
    cmd = list(map(str, cmd))

    if capture_output or stream_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError(
                "stdout and stderr arguments may not be used with capture_output or stream_output"
            )
        kwargs["stdout"] = subprocess.PIPE
        if capture_output:
            kwargs["stderr"] = subprocess.PIPE
        elif stream_output:
            # Redirect stderr to stdout in order to combine the streams for unified printing to
            # `sys.stdout`, as documented in
            # https://docs.python.org/3/library/subprocess.html#subprocess.run
            kwargs["stderr"] = subprocess.STDOUT

    process = subprocess.Popen(
        cmd,
        env=env,
        text=True,
        **kwargs,
    )
    if not synchronous:
        return process

    if stream_output:
        for output_char in iter(lambda: process.stdout.read(1), ""):
            sys.stdout.write(output_char)

    stdout, stderr = process.communicate()
    returncode = process.poll()
    comp_process = subprocess.CompletedProcess(
        process.args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if throw_on_error and returncode != 0:
        raise ShellCommandException.from_completed_process(comp_process)
    return comp_process


def check_port_open(host, port):
    import socket
    from contextlib import closing
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        return sock.connect_ex((host, port)) == 0


def get_random_unused_port(host, min_port=1024, max_port=65535, max_retries=100, exclude_list=None):
    """
    Get random unused port.
    """
    # Use true random generator
    rng = random.SystemRandom()

    exclude_list = exclude_list or []
    for _ in range(max_retries):
        port = rng.randint(min_port, max_port)
        if port in exclude_list:
            continue
        if not check_port_open(host, port):
            return port
    raise RuntimeError(f"Get available port between range {min_port} and {max_port} failed.")


def get_spark_session():
    from pyspark.sql import SparkSession

    spark_session = SparkSession.getActiveSession()
    if spark_session is None:
        raise RuntimeError(
            "Spark session haven't been initiated yet. Please use `SparkSession.builder` to "
            "create a spark session and connect to a spark cluster."
        )
    return spark_session


def get_spark_application_driver_host(spark):
    return spark.conf.get("spark.driver.host")


def get_max_num_concurrent_tasks(spark_context):
    """Gets the current max number of concurrent tasks."""
    # pylint: disable=protected-access
    # spark version 3.1 and above have a different API for fetching max concurrent tasks
    if spark_context._jsc.sc().version() >= "3.1":
        return spark_context._jsc.sc().maxNumConcurrentTasks(
            spark_context._jsc.sc().resourceProfileManager().resourceProfileFromId(0)
        )
    return spark_context._jsc.sc().maxNumConcurrentTasks()


def _get_total_physical_memory():
    import psutil

    return psutil.virtual_memory().total


def _get_total_shared_memory():
    import shutil

    return shutil.disk_usage("/dev/shm").total


def _get_cpu_cores():
    import multiprocessing

    return multiprocessing.cpu_count()


def _calc_mem_per_ray_worker_node(
    num_task_slots, physical_mem_bytes, shared_mem_bytes, heap_to_object_store_ratio
):
    available_physical_mem_per_node = int(
        physical_mem_bytes / num_task_slots * _MEMORY_BUFFER_OFFSET
    )
    available_shared_mem_per_node = int(
        shared_mem_bytes / num_task_slots * _MEMORY_BUFFER_OFFSET
    )

    object_store_bytes = int(
        min(
            available_physical_mem_per_node * heap_to_object_store_ratio,
            available_shared_mem_per_node,
        )
    )
    heap_mem_bytes = (
        available_physical_mem_per_node - object_store_bytes
    )
    return heap_mem_bytes, object_store_bytes


def _resolve_target_spark_tasks(calculated_limits):
    """
    Return the max value of a list of spark task total count calculations based on the
    provided configuration arguments to `init_ray_cluster`.
    Args:
        calculated_limits: A list of calculated values wherein the highest value based on
            spark cluster worker instance sizes and user-specified ray cluster configuration is
            taken to ensure that spark cluster limits are not exceeded.

    Returns: The maximum calculated number of spark tasks for the configured ray cluster.

    """
    return max(*calculated_limits)


def get_target_spark_tasks(
    max_concurrent_tasks,
    num_spark_task_cpus,
    num_spark_task_gpus,
    ray_worker_node_heap_memory_bytes,
    ray_worker_node_object_store_memory_bytes,
    num_spark_tasks,
    total_cpus,
    total_gpus,
    total_heap_memory_bytes,
    total_object_store_memory_bytes,
):

    if num_spark_tasks is not None:
        if num_spark_tasks == -1:
            # num_worker_nodes=-1 represents using all available spark task slots
            num_spark_tasks = max_concurrent_tasks
        elif num_spark_tasks <= 0:
            raise ValueError(
                "The value of 'num_worker_nodes' argument must be either a positive integer or -1."
            )
    else:
        calculated_tasks = [1]
        if total_cpus is not None:
            if total_cpus <= 0:
                raise ValueError(
                    "The value of 'total_cpus' argument must be a positive integer."
                )

            calculated_tasks.append(int(math.ceil(total_cpus / num_spark_task_cpus)))

        if total_gpus is not None:
            if total_gpus <= 0:
                raise ValueError(
                    "The value of 'total_gpus' argument must be a positive integer."
                )

            calculated_tasks.append(int(math.ceil(total_gpus / num_spark_task_gpus)))

        if total_heap_memory_bytes is not None:
            if total_heap_memory_bytes <= 0:
                raise ValueError(
                    "The value of 'total_heap_memory_bytes' argument must be a positive integer."
                )

            calculated_tasks.append(
                int(math.ceil(total_heap_memory_bytes / ray_worker_node_heap_memory_bytes))
            )

        if total_object_store_memory_bytes is not None:
            if total_object_store_memory_bytes <= 0:
                raise ValueError(
                    "The value of 'total_object_store_memory_bytes' argument must be a "
                    "positive integer."
                )

            calculated_tasks.append(
                int(
                    math.ceil(
                        total_object_store_memory_bytes
                        / ray_worker_node_object_store_memory_bytes
                    )
                )
            )

        num_spark_tasks = _resolve_target_spark_tasks(calculated_tasks)
    return num_spark_tasks


def get_avail_mem_per_ray_worker_node(spark, heap_to_object_store_ratio):
    """
    Return the available heap memory and object store memory for each ray worker.
    NB: We have one ray node per spark task.
    """
    num_cpus_per_spark_task = int(
        spark.sparkContext.getConf().get("spark.task.cpus", "1")
    )

    def mapper(_):
        try:
            num_cpus = _get_cpu_cores()
            num_task_slots = num_cpus // num_cpus_per_spark_task

            physical_mem_bytes = _get_total_physical_memory()
            shared_mem_bytes = _get_total_shared_memory()

            (
                ray_worker_node_heap_mem_bytes,
                ray_worker_node_object_store_bytes,
            ) = _calc_mem_per_ray_worker_node(
                num_task_slots,
                physical_mem_bytes,
                shared_mem_bytes,
                heap_to_object_store_ratio,
            )
            return ray_worker_node_heap_mem_bytes, ray_worker_node_object_store_bytes, None
        except Exception as e:
            return -1, -1, repr(e)

    # Running memory inference routine on spark executor side since the spark worker nodes may
    # have a different machine configuration compared to the spark driver node.
    inferred_ray_worker_node_heap_mem_bytes, inferred_ray_worker_node_object_store_bytes, err = (
        spark.sparkContext.parallelize([1], 1).map(mapper).collect()[0]
    )

    if err is not None:
        raise RuntimeError(
            f"Inferring ray worker available memory failed, error: {err}"
        )
    return inferred_ray_worker_node_heap_mem_bytes, inferred_ray_worker_node_object_store_bytes


def get_spark_task_assigned_physical_gpus(gpu_addr_list):
    if "CUDA_VISIBLE_DEVICES" in os.environ:
        visible_cuda_dev_list = [
            int(dev.strip()) for dev in os.environ["CUDA_VISIBLE_DEVICES"].split(",")
        ]
        return [visible_cuda_dev_list[addr] for addr in gpu_addr_list]
    else:
        return gpu_addr_list


def _acquire_lock_for_ray_worker_node_startup():
    """
    If we start multiple ray workers on a machine concurrently, some ray worker processes
    might fail due to ray port conflicts, this is because race condition on getting free
    port and opening the free port.
    To address the issue, this function use an exclusive file lock to delay the worker processes
    to ensure that port acquisition does not create a resource contention issue due to a race
    condition.

    Returns: None
    """
    def acquire_lock(file_path):
        mode = os.O_RDWR | os.O_CREAT | os.O_TRUNC
        try:
            fd = os.open(file_path, mode)
            # Allow for retrying getting a file lock a maximum number of seconds
            max_lock_iter = 600
            for _ in range(max_lock_iter):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Lock is used by other processes, continue loop to wait for lock available
                    pass
                else:
                    # Acquire lock successfully.
                    return fd
                time.sleep(1.0)
            raise TimeoutError(f"Acquiring lock on file {file_path} timeout.")
        except Exception:
            os.close(fd)

    lock_file_path = "/tmp/ray_on_spark_worker_startup_barrier_lock.lock"
    try:
        lock_fd = acquire_lock(lock_file_path)
    except TimeoutError:
        # If timeout happens, the file lock might be hold by another process and that process
        # does not release the lock in time by some unexpected reason.
        # In this case, remove the existing lock file and create the file again, and then
        # acquire file lock on the new file.
        try:
            os.remove(lock_file_path)
        except Exception:
            pass
        lock_fd = acquire_lock(lock_file_path)

    def hold_lock_for_10s_and_release():
        time.sleep(10)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    threading.Thread(target=hold_lock_for_10s_and_release, args=()).start()


def _display_databricks_driver_proxy_url(spark_context, port, title):
    from dbruntime.display import displayHTML
    driverLocal = spark_context._jvm.com.databricks.backend.daemon.driver.DriverLocal
    commandContextTags = driverLocal.commandContext().get().toStringMap().apply("tags")
    orgId = commandContextTags.apply("orgId")
    clusterId = commandContextTags.apply("clusterId")

    template = "/driver-proxy/o/{orgId}/{clusterId}/{port}/"
    proxy_url = template.format(orgId=orgId, clusterId=clusterId, port=port)

    displayHTML(f"""
      <div style="margin-bottom: 16px">
          <a href="{proxy_url}">
              Open {title} in a new tab
          </a>
      </div>
    """)
