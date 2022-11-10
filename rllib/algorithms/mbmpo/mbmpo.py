import logging
import numpy as np
from typing import List, Optional, Type

import ray
from ray.rllib.algorithms.mbmpo.model_ensemble import DynamicsEnsembleCustomModel
from ray.rllib.algorithms.mbmpo.utils import calculate_gae_advantages, MBMPOExploration
from ray.rllib.algorithms.algorithm import Algorithm
from ray.rllib.algorithms.algorithm_config import AlgorithmConfig, NotProvided
from ray.rllib.env.env_context import EnvContext
from ray.rllib.env.wrappers.model_vector_env import model_vector_env
from ray.rllib.evaluation.metrics import (
    collect_episodes,
    collect_metrics,
    get_learner_stats,
)
from ray.rllib.evaluation.worker_set import WorkerSet
from ray.rllib.execution.common import (
    STEPS_SAMPLED_COUNTER,
    STEPS_TRAINED_COUNTER,
    STEPS_TRAINED_THIS_ITER_COUNTER,
    _get_shared_metrics,
)
from ray.rllib.execution.metric_ops import CollectMetrics
from ray.rllib.policy.policy import Policy
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID, SampleBatch, concat_samples
from ray.rllib.utils.annotations import Deprecated, override
from ray.rllib.utils.deprecation import DEPRECATED_VALUE
from ray.rllib.utils.metrics.learner_info import LEARNER_INFO
from ray.rllib.utils.sgd import standardized
from ray.rllib.utils.torch_utils import convert_to_torch_tensor
from ray.rllib.utils.typing import EnvType, AlgorithmConfigDict
from ray.util.iter import from_actors, LocalIterator

logger = logging.getLogger(__name__)


class MBMPOConfig(AlgorithmConfig):
    """Defines a configuration class from which an MBMPO Algorithm can be built.

    Example:
        >>> from ray.rllib.algorithms.mbmpo import MBMPOConfig
        >>> config = MBMPOConfig()
        >>> config = config.training(lr=0.0003, train_batch_size=512)  # doctest: +SKIP
        >>> config = config.resources(num_gpus=4) # doctest: +SKIP
        >>> config = config.rollouts(num_rollout_workers=64)  # doctest: +SKIP
        >>> print(config.to_dict())  # doctest: +SKIP
        >>> # Build a Algorithm object from the config and run 1 training iteration.
        >>> algo = config.build(env="CartPole-v1")  # doctest: +SKIP
        >>> algo.train()  # doctest: +SKIP

    Example:
        >>> from ray.rllib.algorithms.mbmpo import MBMPOConfig
        >>> from ray import air
        >>> from ray import tune
        >>> config = MBMPOConfig()
        >>> # Print out some default values.
        >>> print(config.vtrace)  # doctest: +SKIP
        >>> # Update the config object.
        >>> config = config\  # doctest: +SKIP
        ...     .training(lr=tune.grid_search([0.0001, 0.0003]), grad_clip=20.0)
        >>> # Set the config object's env.
        >>> config = config.environment(env="CartPole-v1")  # doctest: +SKIP
        >>> # Use to_dict() to get the old-style python config dict
        >>> # when running with tune.
        >>> tune.Tuner(  # doctest: +SKIP
        ...     "AlphaStar",
        ...     run_config=air.RunConfig(stop={"episode_reward_mean": 200}),
        ...     param_space=config.to_dict(),
        ... ).fit()
    """

    def __init__(self, algo_class=None):
        """Initializes a MBMPOConfig instance."""
        super().__init__(algo_class=algo_class or MBMPO)

        # fmt: off
        # __sphinx_doc_begin__

        # MBMPO specific config settings:
        # If true, use the Generalized Advantage Estimator (GAE)
        # with a value function, see https://arxiv.org/pdf/1506.02438.pdf.
        self.use_gae = True
        # GAE(lambda) parameter.
        self.lambda_ = 1.0
        # Initial coefficient for KL divergence.
        self.kl_coeff = 0.0005

        # Coefficient of the value function loss.
        self.vf_loss_coeff = 0.5
        # Coefficient of the entropy regularizer.
        self.entropy_coeff = 0.0
        # PPO clip parameter.
        self.clip_param = 0.5
        # Clip param for the value function. Note that this is sensitive to the
        # scale of the rewards. If your expected V is large, increase this.
        self.vf_clip_param = 10.0
        # If specified, clip the global norm of gradients by this amount.
        self.grad_clip = None
        # Target value for KL divergence.
        self.kl_target = 0.01
        # Number of Inner adaptation steps for the MAML algorithm.
        self.inner_adaptation_steps = 1
        # Number of MAML steps per meta-update iteration (PPO steps).
        self.maml_optimizer_steps = 8
        # Inner adaptation step size.
        self.inner_lr = 1e-3
        # Horizon of the environment (200 in MB-MPO paper).
        self.horizon = 200
        # Dynamics ensemble hyperparameters.
        self.dynamics_model = {
            "custom_model": DynamicsEnsembleCustomModel,
            # Number of Transition-Dynamics (TD) models in the ensemble.
            "ensemble_size": 5,
            # Hidden layers for each model in the TD-model ensemble.
            "fcnet_hiddens": [512, 512, 512],
            # Model learning rate.
            "lr": 1e-3,
            # Max number of training epochs per MBMPO iter.
            "train_epochs": 500,
            # Model batch size.
            "batch_size": 500,
            # Training/validation split.
            "valid_split_ratio": 0.2,
            # Normalize data (obs, action, and deltas).
            "normalize_data": True,
        }
        # Workers sample from dynamics models, not from actual envs.
        self.custom_vector_env = model_vector_env
        # How many iterations through MAML per MBMPO iteration.
        self.num_maml_steps = 10

        # Override some of AlgorithmConfig's default values with MBMPO-specific
        # values.
        self.batch_mode = "complete_episodes"
        self.num_rollout_workers = 2
        # Size of batches collected from each worker.
        self.rollout_fragment_length = 200
        # Do create an actual env on the local worker (worker-idx=0).
        self.create_env_on_local_worker = True
        # Step size of SGD.
        self.lr = 1e-3
        # Exploration for MB-MPO is based on StochasticSampling, but uses 8000
        # random timesteps up-front for worker=0.
        self.exploration_config = {
            "type": MBMPOExploration,
            "random_timesteps": 8000,
        }

        # __sphinx_doc_end__
        # fmt: on

        self.vf_share_layers = DEPRECATED_VALUE
        self._disable_execution_plan_api = False

    @override(AlgorithmConfig)
    def training(
        self,
        *,
        use_gae: Optional[float] = NotProvided,
        lambda_: Optional[float] = NotProvided,
        kl_coeff: Optional[float] = NotProvided,
        vf_loss_coeff: Optional[float] = NotProvided,
        entropy_coeff: Optional[float] = NotProvided,
        clip_param: Optional[float] = NotProvided,
        vf_clip_param: Optional[float] = NotProvided,
        grad_clip: Optional[float] = NotProvided,
        kl_target: Optional[float] = NotProvided,
        inner_adaptation_steps: Optional[int] = NotProvided,
        maml_optimizer_steps: Optional[int] = NotProvided,
        inner_lr: Optional[float] = NotProvided,
        horizon: Optional[int] = NotProvided,
        dynamics_model: Optional[dict] = NotProvided,
        custom_vector_env: Optional[type] = NotProvided,
        num_maml_steps: Optional[int] = NotProvided,
        **kwargs,
    ) -> "MBMPOConfig":
        """Sets the training related configuration.

        Args:
            use_gae: If true, use the Generalized Advantage Estimator (GAE)
                with a value function, see https://arxiv.org/pdf/1506.02438.pdf.
            lambda_: The GAE (lambda) parameter.
            kl_coeff: Initial coefficient for KL divergence.
            vf_loss_coeff: Coefficient of the value function loss.
            entropy_coeff: Coefficient of the entropy regularizer.
            clip_param: PPO clip parameter.
            vf_clip_param: Clip param for the value function. Note that this is
                sensitive to the scale of the rewards. If your expected V is large,
                increase this.
            grad_clip: If specified, clip the global norm of gradients by this amount.
            kl_target: Target value for KL divergence.
            inner_adaptation_steps: Number of Inner adaptation steps for the MAML
                algorithm.
            maml_optimizer_steps: Number of MAML steps per meta-update iteration
                (PPO steps).
            inner_lr: Inner adaptation step size.
            horizon: Horizon of the environment (200 in MB-MPO paper).
            dynamics_model: Dynamics ensemble hyperparameters.
            custom_vector_env: Workers sample from dynamics models, not from actual
                envs.
            num_maml_steps: How many iterations through MAML per MBMPO iteration.

        Returns:
            This updated AlgorithmConfig object.
        """
        # Pass kwargs onto super's `training()` method.
        super().training(**kwargs)

        if use_gae is not NotProvided:
            self.use_gae = use_gae
        if lambda_ is not NotProvided:
            self.lambda_ = lambda_
        if kl_coeff is not NotProvided:
            self.kl_coeff = kl_coeff
        if vf_loss_coeff is not NotProvided:
            self.vf_loss_coeff = vf_loss_coeff
        if entropy_coeff is not NotProvided:
            self.entropy_coeff = entropy_coeff
        if clip_param is not NotProvided:
            self.clip_param = clip_param
        if vf_clip_param is not NotProvided:
            self.vf_clip_param = vf_clip_param
        if grad_clip is not NotProvided:
            self.grad_clip = grad_clip
        if kl_target is not NotProvided:
            self.kl_target = kl_target
        if inner_adaptation_steps is not NotProvided:
            self.inner_adaptation_steps = inner_adaptation_steps
        if maml_optimizer_steps is not NotProvided:
            self.maml_optimizer_steps = maml_optimizer_steps
        if inner_lr is not NotProvided:
            self.inner_lr = inner_lr
        if horizon is not NotProvided:
            self.horizon = horizon
        if dynamics_model is not NotProvided:
            self.dynamics_model.update(dynamics_model)
        if custom_vector_env is not NotProvided:
            self.custom_vector_env = custom_vector_env
        if num_maml_steps is not NotProvided:
            self.num_maml_steps = num_maml_steps

        return self

    @override(AlgorithmConfig)
    def validate(self) -> None:
        # Call super's validation method.
        super().validate()

        if self.num_gpus > 1:
            raise ValueError("`num_gpus` > 1 not yet supported for MB-MPO!")
        if self.framework_str != "torch":
            raise ValueError(
                "MB-MPO only supported in PyTorch so far! Try setting config. "
                "framework('torch')."
            )
        if self.inner_adaptation_steps <= 0:
            raise ValueError("Inner adaptation steps must be >=1!")
        if self.maml_optimizer_steps <= 0:
            raise ValueError("PPO steps for meta-update needs to be >=0!")
        if self.entropy_coeff < 0:
            raise ValueError("`entropy_coeff` must be >=0.0!")
        if self.batch_mode != "complete_episodes":
            raise ValueError("`batch_mode=truncate_episodes` not supported!")
        if self.num_rollout_workers <= 0:
            raise ValueError("Must have at least 1 worker/task.")
        if self.create_env_on_local_worker is False:
            raise ValueError(
                "Must have an actual Env created on the local worker process!"
                "Try setting `config.environment("
                "create_env_on_local_worker=True)`."
            )


# Select Metric Keys for MAML Stats Tracing
METRICS_KEYS = ["episode_reward_mean", "episode_reward_min", "episode_reward_max"]


class MetaUpdate:
    def __init__(self, workers, num_steps, maml_steps, metric_gen):
        """Computes the MetaUpdate step in MAML.

        Adapted for MBMPO for multiple MAML Iterations.

        Args:
            workers: Set of Workers
            num_steps: Number of meta-update steps per MAML Iteration
            maml_steps: MAML Iterations per MBMPO Iteration
            metric_gen: Generates metrics dictionary

        Returns:
            metrics: MBMPO metrics for logging.
        """
        self.workers = workers
        self.num_steps = num_steps
        self.step_counter = 0
        self.maml_optimizer_steps = maml_steps
        self.metric_gen = metric_gen
        self.metrics = {}

    def __call__(self, data_tuple):
        """Args:
        data_tuple: 1st element is samples collected from MAML
        Inner adaptation steps and 2nd element is accumulated metrics
        """
        # Metaupdate Step.
        print("Meta-Update Step")
        samples = data_tuple[0]
        adapt_metrics_dict = data_tuple[1]
        self.postprocess_metrics(
            adapt_metrics_dict, prefix="MAMLIter{}".format(self.step_counter)
        )

        # MAML Meta-update.
        fetches = None
        for i in range(self.maml_optimizer_steps):
            fetches = self.workers.local_worker().learn_on_batch(samples)
        learner_stats = get_learner_stats(fetches)

        # Update KLs.
        def update(pi, pi_id):
            assert "inner_kl" not in learner_stats, (
                "inner_kl should be nested under policy id key",
                learner_stats,
            )
            if pi_id in learner_stats:
                assert "inner_kl" in learner_stats[pi_id], (learner_stats, pi_id)
                pi.update_kls(learner_stats[pi_id]["inner_kl"])
            else:
                logger.warning("No data for {}, not updating kl".format(pi_id))

        self.workers.local_worker().foreach_policy_to_train(update)

        # Modify Reporting Metrics.
        metrics = _get_shared_metrics()
        metrics.info[LEARNER_INFO] = fetches
        metrics.counters[STEPS_TRAINED_THIS_ITER_COUNTER] = samples.count
        metrics.counters[STEPS_TRAINED_COUNTER] += samples.count

        if self.step_counter == self.num_steps - 1:
            td_metric = self.workers.local_worker().foreach_policy(fit_dynamics)[0]

            # Sync workers with meta policy.
            self.workers.sync_weights()

            # Sync TD Models with workers.
            sync_ensemble(self.workers)
            sync_stats(self.workers)

            metrics.counters[STEPS_SAMPLED_COUNTER] = td_metric[STEPS_SAMPLED_COUNTER]

            # Modify to CollectMetrics.
            res = self.metric_gen.__call__(None)
            res.update(self.metrics)
            self.step_counter = 0
            print("MB-MPO Iteration Completed")
            return [res]
        else:
            print("MAML Iteration {} Completed".format(self.step_counter))
            self.step_counter += 1

            # Sync workers with meta policy
            print("Syncing Weights with Workers")
            self.workers.sync_weights()
            return []

    def postprocess_metrics(self, metrics, prefix=""):
        """Appends prefix to current metrics

        Args:
            metrics: Dictionary of current metrics
            prefix: Prefix string to be appended
        """
        for key in metrics.keys():
            self.metrics[prefix + "_" + key] = metrics[key]


def post_process_metrics(prefix, workers, metrics):
    """Update current dataset metrics and filter out specific keys.

    Args:
        prefix: Prefix string to be appended
        workers: Set of workers
        metrics: Current metrics dictionary
    """
    res = collect_metrics(remote_workers=workers.remote_workers())
    for key in METRICS_KEYS:
        metrics[prefix + "_" + key] = res[key]
    return metrics


def inner_adaptation(workers: WorkerSet, samples: List[SampleBatch]):
    """Performs one gradient descend step on each remote worker.

    Args:
        workers: The WorkerSet of the Algorithm.
        samples (List[SampleBatch]): The list of SampleBatches to perform
            a training step on (one for each remote worker).
    """

    for i, e in enumerate(workers.remote_workers()):
        e.learn_on_batch.remote(samples[i])


def fit_dynamics(policy, pid):
    return policy.dynamics_model.fit()


def sync_ensemble(workers: WorkerSet) -> None:
    """Syncs dynamics ensemble weights from driver (main) to workers.

    Args:
        workers: Set of workers, including driver (main).
    """

    def get_ensemble_weights(worker):
        policy_map = worker.policy_map
        policies = policy_map.keys()

        def policy_ensemble_weights(policy):
            model = policy.dynamics_model
            return {k: v.cpu().detach().numpy() for k, v in model.state_dict().items()}

        return {
            pid: policy_ensemble_weights(policy)
            for pid, policy in policy_map.items()
            if pid in policies
        }

    def set_ensemble_weights(policy, pid, weights):
        weights = weights[pid]
        weights = convert_to_torch_tensor(weights, device=policy.device)
        model = policy.dynamics_model
        model.load_state_dict(weights)

    if workers.remote_workers():
        weights = ray.put(get_ensemble_weights(workers.local_worker()))
        set_func = ray.put(set_ensemble_weights)
        for e in workers.remote_workers():
            e.foreach_policy.remote(set_func, weights=weights)


def sync_stats(workers: WorkerSet) -> None:
    def get_normalizations(worker):
        policy = worker.policy_map[DEFAULT_POLICY_ID]
        return policy.dynamics_model.normalizations

    def set_normalizations(policy, pid, normalizations):
        policy.dynamics_model.set_norms(normalizations)

    if workers.remote_workers():
        normalization_dict = ray.put(get_normalizations(workers.local_worker()))
        set_func = ray.put(set_normalizations)
        for e in workers.remote_workers():
            e.foreach_policy.remote(set_func, normalizations=normalization_dict)


def post_process_samples(samples, config: AlgorithmConfigDict):
    # Instead of using NN for value function, we use regression
    split_lst = []
    for sample in samples:
        indexes = np.asarray(sample["dones"]).nonzero()[0]
        indexes = indexes + 1

        reward_list = np.split(sample["rewards"], indexes)[:-1]
        observation_list = np.split(sample["obs"], indexes)[:-1]

        paths = []
        for i in range(0, len(reward_list)):
            paths.append(
                {"rewards": reward_list[i], "observations": observation_list[i]}
            )

        paths = calculate_gae_advantages(paths, config["gamma"], config["lambda"])

        advantages = np.concatenate([path["advantages"] for path in paths])
        sample["advantages"] = standardized(advantages)
        split_lst.append(sample.count)
    return samples, split_lst


class MBMPO(Algorithm):
    """Model-Based Meta Policy Optimization (MB-MPO) Algorithm.

    This file defines the distributed Algorithm class for model-based meta
    policy optimization.
    See `mbmpo_[tf|torch]_policy.py` for the definition of the policy loss.

    Detailed documentation:
    https://docs.ray.io/en/master/rllib-algorithms.html#mbmpo
    """

    @classmethod
    @override(Algorithm)
    def get_default_config(cls) -> AlgorithmConfig:
        return MBMPOConfig()

    @classmethod
    @override(Algorithm)
    def get_default_policy_class(
        cls, config: AlgorithmConfig
    ) -> Optional[Type[Policy]]:
        from ray.rllib.algorithms.mbmpo.mbmpo_torch_policy import MBMPOTorchPolicy

        return MBMPOTorchPolicy

    @staticmethod
    @override(Algorithm)
    def execution_plan(
        workers: WorkerSet, config: AlgorithmConfigDict, **kwargs
    ) -> LocalIterator[dict]:
        assert (
            len(kwargs) == 0
        ), "MBMPO execution_plan does NOT take any additional parameters"

        # Train TD Models on the driver.
        workers.local_worker().foreach_policy(fit_dynamics)

        # Sync driver's policy with workers.
        workers.sync_weights()

        # Sync TD Models and normalization stats with workers
        sync_ensemble(workers)
        sync_stats(workers)

        # Dropping metrics from the first iteration
        _, _ = collect_episodes(
            workers.local_worker(), workers.remote_workers(), [], timeout_seconds=9999
        )

        # Metrics Collector.
        metric_collect = CollectMetrics(
            workers,
            min_history=0,
            timeout_seconds=config["metrics_episode_collection_timeout_s"],
        )

        num_inner_steps = config["inner_adaptation_steps"]

        def inner_adaptation_steps(itr):
            buf = []
            split = []
            metrics = {}
            for samples in itr:
                print("Collecting Samples, Inner Adaptation {}".format(len(split)))
                # Processing Samples (Standardize Advantages)
                samples, split_lst = post_process_samples(samples, config)

                buf.extend(samples)
                split.append(split_lst)

                adapt_iter = len(split) - 1
                prefix = "DynaTrajInner_" + str(adapt_iter)
                metrics = post_process_metrics(prefix, workers, metrics)

                if len(split) > num_inner_steps:
                    out = concat_samples(buf)
                    out["split"] = np.array(split)
                    buf = []
                    split = []

                    yield out, metrics
                    metrics = {}
                else:
                    inner_adaptation(workers, samples)

        # Iterator for Inner Adaptation Data gathering (from pre->post
        # adaptation).
        rollouts = from_actors(workers.remote_workers())
        rollouts = rollouts.batch_across_shards()
        rollouts = rollouts.transform(inner_adaptation_steps)

        # Meta update step with outer combine loop for multiple MAML
        # iterations.
        train_op = rollouts.combine(
            MetaUpdate(
                workers,
                config["num_maml_steps"],
                config["maml_optimizer_steps"],
                metric_collect,
            )
        )
        return train_op

    @staticmethod
    @override(Algorithm)
    def validate_env(env: EnvType, env_context: EnvContext) -> None:
        """Validates the local_worker's env object (after creation).

        Args:
            env: The env object to check (for worker=0 only).
            env_context: The env context used for the instantiation of
                the local worker's env (worker=0).

        Raises:
            ValueError: In case something is wrong with the config.
        """
        if not hasattr(env, "reward") or not callable(env.reward):
            raise ValueError(
                f"Env {env} doest not have a `reward()` method, needed for "
                "MB-MPO! This `reward()` method should return "
            )


# Deprecated: Use ray.rllib.algorithms.mbmpo.MBMPOConfig instead!
class _deprecated_default_config(dict):
    def __init__(self):
        super().__init__(MBMPOConfig().to_dict())

    @Deprecated(
        old="ray.rllib.algorithms.mbmpo.mbmpo.DEFAULT_CONFIG",
        new="ray.rllib.algorithms.mbmpo.mbmpo.MBMPOConfig(...)",
        error=True,
    )
    def __getitem__(self, item):
        return super().__getitem__(item)


DEFAULT_CONFIG = _deprecated_default_config()
