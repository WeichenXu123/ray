import os.path

import pytest
import ray

from ray import spark as ray_spark
from pyspark.sql import SparkSession
import time


class RayOnSparkTestBase:

    spark = None
    num_total_cpus = None
    num_total_gpus = None
    num_cpus_per_spark_task = None
    num_gpus_per_spark_task = None
    max_spark_tasks = None

    @classmethod
    def setup_class(cls):
        raise NotImplementedError()

    @classmethod
    def teardown_class(cls):
        cls.spark.stop()

    @staticmethod
    def get_ray_worker_resources_list():
        wr_list = []
        for node in ray.nodes():
            # exclude dead node and head node (with 0 CPU resource)
            if node['Alive'] and node['Resources'].get('CPU', 0) > 0:
                wr_list.append(node['Resources'])
        return wr_list

    def test_cpu_allocation(self):
        for num_spark_tasks in [self.max_spark_tasks // 2, self.max_spark_tasks]:
            with ray_spark.init_cluster(num_spark_tasks=num_spark_tasks):
                time.sleep(2)
                worker_res_list = self.get_ray_worker_resources_list()
                assert len(worker_res_list) == num_spark_tasks
                for worker_res in worker_res_list:
                    assert worker_res['CPU'] == self.num_cpus_per_spark_task

    def test_gpu_allocation(self):
        if self.num_gpus_per_spark_task == 0:
            pytest.skip("Skip GPU test on cluster without GPU configured.")

        for num_spark_tasks in [self.max_spark_tasks // 2, self.max_spark_tasks]:
            with ray_spark.init_cluster(num_spark_tasks=num_spark_tasks) as cluster:
                time.sleep(2)
                worker_res_list = self.get_ray_worker_resources_list()
                assert len(worker_res_list) == num_spark_tasks
                for worker_res in worker_res_list:
                    assert worker_res['GPU'] == self.num_gpus_per_spark_task

    def test_basic_ray_app(self):
        with ray_spark.init_cluster(num_spark_tasks=self.max_spark_tasks) as cluster:
            @ray.remote
            def f(x):
                return x * x

            futures = [f.remote(i) for i in range(32)]
            results = ray.get(futures)
            assert results == [i * i for i in range(32)]


class TestBasicSparkCluster(RayOnSparkTestBase):

    @classmethod
    def setup_class(cls):
        cls.num_total_cpus = 2
        cls.num_total_gpus = 0
        cls.num_cpus_per_spark_task = 1
        cls.num_gpus_per_spark_task = 0
        cls.max_spark_tasks = 2
        os.environ["SPARK_WORKER_CORES"] = "2"
        cls.spark = SparkSession.builder \
            .master("local-cluster[1, 2, 1024]") \
            .config("spark.task.cpus", "1") \
            .config("spark.task.maxFailures", "1") \
            .getOrCreate()


class TestBasicSparkGPUCluster(RayOnSparkTestBase):

    @classmethod
    def setup_class(cls):
        cls.num_total_cpus = 2
        cls.num_total_gpus = 2
        cls.num_cpus_per_spark_task = 1
        cls.num_gpus_per_spark_task = 1
        cls.max_spark_tasks = 2
        gpu_discovery_script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "discover_2_gpu.sh"
        )
        os.environ["SPARK_WORKER_CORES"] = "4"
        cls.spark = SparkSession.builder \
            .master("local-cluster[1, 2, 1024]") \
            .config("spark.task.cpus", "1") \
            .config("spark.task.resource.gpu.amount", "1") \
            .config("spark.executor.cores", "2") \
            .config("spark.worker.resource.gpu.amount", "2") \
            .config("spark.executor.resource.gpu.amount", "2") \
            .config("spark.task.maxFailures", "1") \
            .config("spark.worker.resource.gpu.discoveryScript", gpu_discovery_script_path) \
            .getOrCreate()


class TestMultiCoresPerTaskCluster(RayOnSparkTestBase):

    @classmethod
    def setup_class(cls):
        cls.num_total_cpus = 4
        cls.num_total_gpus = 4
        cls.num_cpus_per_spark_task = 2
        cls.num_gpus_per_spark_task = 2
        cls.max_spark_tasks = 2
        gpu_discovery_script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "discover_4_gpu.sh"
        )
        os.environ["SPARK_WORKER_CORES"] = "4"
        cls.spark = SparkSession.builder \
            .master("local-cluster[1, 4, 1024]") \
            .config("spark.task.cpus", "2") \
            .config("spark.task.resource.gpu.amount", "2") \
            .config("spark.executor.cores", "4") \
            .config("spark.worker.resource.gpu.amount", "4") \
            .config("spark.executor.resource.gpu.amount", "4") \
            .config("spark.task.maxFailures", "1") \
            .config("spark.worker.resource.gpu.discoveryScript", gpu_discovery_script_path) \
            .getOrCreate()
