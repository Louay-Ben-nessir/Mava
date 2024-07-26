# Copyright 2022 InstaDeep Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import threading
from queue import Queue
from typing import Any, Dict, List, Sequence, Tuple

import chex
import flax
import hydra
import jax
import jax.debug
import jax.numpy as jnp
import numpy as np
import optax
from colorama import Fore, Style
from flax.core.frozen_dict import FrozenDict
from omegaconf import DictConfig, OmegaConf
from optax._src.base import OptState
from rich.pretty import pprint

from mava.evaluator import get_sebulba_eval_fn as get_eval_fn
from mava.evaluator import make_ff_eval_act_fn
from mava.networks import FeedForwardActor as Actor
from mava.networks import FeedForwardValueNet as Critic
from mava.systems.ppo.types import LearnerState, OptStates, Params, PPOTransition
from mava.types import (
    ActorApply,
    CriticApply,
    ExperimentOutput,
    Observation,
    SebulbaLearnerFn,
)
from mava.utils import make_env as environments
from mava.utils.checkpointing import Checkpointer
from mava.utils.jax_utils import merge_leading_dims
from mava.utils.logger import LogEvent, MavaLogger
from mava.utils.sebulba_utils import ParamsSource, Pipeline, RecordTimeTo, ThreadLifetime
from mava.utils.total_timestep_checker import sebulba_check_total_timesteps
from mava.utils.training import make_learning_rate
from mava.wrappers.episode_metrics import get_final_step_metrics


def rollout(
    key: chex.PRNGKey,
    config: DictConfig,
    rollout_pipeline: Pipeline,
    params_source: ParamsSource,
    apply_fns: Tuple,
    actor_device_id: int,
    seeds: List[int],
    thread_lifetime: ThreadLifetime,
) -> None:
    # setup
    env = environments.make_gym_env(config, config.arch.num_envs)
    current_actor_device = jax.devices()[actor_device_id]
    actor_apply_fn, critic_apply_fn = apply_fns
    num_agents, num_envs = config.system.num_agents, config.arch.num_envs

    # Define the util functions: select action function and prepare data to share it with learner.
    @jax.jit
    def get_action_and_value(
        params: FrozenDict,
        observation: Observation,
        key: chex.PRNGKey,
    ) -> Tuple:
        """Get action and value."""
        key, subkey = jax.random.split(key)

        actor_policy = actor_apply_fn(params.actor_params, observation)
        action = actor_policy.sample(seed=subkey)
        log_prob = actor_policy.log_prob(action)

        value = critic_apply_fn(params.critic_params, observation).squeeze()
        return action, log_prob, value, key

    timestep = env.reset(seed=seeds)

    next_dones = jax.tree_util.tree_map(
        lambda x: jnp.repeat(x, num_agents).reshape(num_envs, -1),
        timestep.last(),
    )

    move_to_device = lambda x: jax.device_put(x, device=current_actor_device)

    # Loop till the learner has finished training
    while not thread_lifetime.should_stop():
        # Rollout
        traj: List = []
        time_dict: Dict[str, List[float]] = {"single_rollout": [], "env_step_time": []}

        # Loop over the rollout length
        with RecordTimeTo(time_dict["single_rollout"]):
            for _ in range(config.system.rollout_length):
                # Get the latest parameters from the learner
                params = params_source.get()

                cached_next_obs = jax.tree.map(move_to_device, timestep.observation)
                cached_next_dones = move_to_device(next_dones)

                # Get action and value
                (
                    action,
                    log_prob,
                    value,
                    key,
                ) = get_action_and_value(params, cached_next_obs, key)

                # Step the environment
                cpu_action = jax.device_get(action)

                with RecordTimeTo(time_dict["env_step_time"]):
                    timestep = env.step(
                        cpu_action.swapaxes(0, 1)
                    )  # (num_env, num_agents) --> (num_agents, num_env)

                next_dones = jax.tree_util.tree_map(
                    lambda x: jnp.repeat(x, num_agents).reshape(num_envs, -1),
                    timestep.last(),
                )

                # Append data to storage
                traj.append(
                    PPOTransition(
                        done=cached_next_dones,
                        action=action,
                        value=value,
                        reward=timestep.reward,
                        log_prob=log_prob,
                        obs=cached_next_obs,
                        info=timestep.extras,
                    )
                )

        rollout_pipeline.put(traj, timestep.observation, next_dones, time_dict)


def get_learner_fn(
    apply_fns: Tuple[ActorApply, CriticApply],
    update_fns: Tuple[optax.TransformUpdateFn, optax.TransformUpdateFn],
    config: DictConfig,
) -> SebulbaLearnerFn[LearnerState, PPOTransition]:
    """Get the learner function."""

    # Get apply and update functions for actor and critic networks.
    actor_apply_fn, critic_apply_fn = apply_fns
    actor_update_fn, critic_update_fn = update_fns

    def _update_step(
        learner_state: LearnerState,
        traj_batch: PPOTransition,
        last_obs: Observation,
        last_dones: chex.Array,
    ) -> Tuple[LearnerState, Tuple]:
        """A single update of the network.

        This function steps the environment and records the trajectory batch for
        training. It then calculates advantages and targets based on the recorded
        trajectory and updates the actor and critic networks based on the calculated
        losses.

        Args:
            learner_state (NamedTuple):
                - params (Params): The current model parameters.
                - opt_states (OptStates): The current optimizer states.
                - key (PRNGKey): The random number generator state.
                - env_state (State): The environment state.
                - last_timestep (TimeStep): The last timestep in the current trajectory.
            _ (Any): The current metrics info.
        """

        def _calculate_gae(
            traj_batch: PPOTransition, last_val: chex.Array, last_done: chex.Array
        ) -> Tuple[chex.Array, chex.Array]:
            def _get_advantages(
                carry: Tuple[chex.Array, chex.Array, chex.Array], transition: PPOTransition
            ) -> Tuple[Tuple[chex.Array, chex.Array, chex.Array], chex.Array]:
                gae, next_value, next_done = carry
                done, value, reward = transition.done, transition.value, transition.reward
                gamma = config.system.gamma
                delta = reward + gamma * next_value * (1 - next_done) - value
                gae = delta + gamma * config.system.gae_lambda * (1 - next_done) * gae
                return (gae, value, done), gae

            _, advantages = jax.lax.scan(
                _get_advantages,
                (jnp.zeros_like(last_val), last_val, last_done),
                traj_batch,
                reverse=True,
                unroll=16,
            )
            return advantages, advantages + traj_batch.value

        # CALCULATE ADVANTAGE
        params, opt_states, key, _, _ = learner_state
        last_val = critic_apply_fn(params.critic_params, last_obs)
        advantages, targets = _calculate_gae(traj_batch, last_val, last_dones)

        def _update_epoch(update_state: Tuple, _: Any) -> Tuple:
            """Update the network for a single epoch."""

            def _update_minibatch(train_state: Tuple, batch_info: Tuple) -> Tuple:
                """Update the network for a single minibatch."""

                # UNPACK TRAIN STATE AND BATCH INFO
                params, opt_states, key = train_state
                traj_batch, advantages, targets = batch_info

                def _actor_loss_fn(
                    actor_params: FrozenDict,
                    actor_opt_state: OptState,
                    traj_batch: PPOTransition,
                    gae: chex.Array,
                    key: chex.PRNGKey,
                ) -> Tuple:
                    """Calculate the actor loss."""
                    # RERUN NETWORK
                    actor_policy = actor_apply_fn(actor_params, traj_batch.obs)
                    log_prob = actor_policy.log_prob(traj_batch.action)

                    # CALCULATE ACTOR LOSS
                    ratio = jnp.exp(log_prob - traj_batch.log_prob)
                    gae = (gae - gae.mean()) / (gae.std() + 1e-8)
                    loss_actor1 = ratio * gae
                    loss_actor2 = (
                        jnp.clip(
                            ratio,
                            1.0 - config.system.clip_eps,
                            1.0 + config.system.clip_eps,
                        )
                        * gae
                    )
                    loss_actor = -jnp.minimum(loss_actor1, loss_actor2)
                    loss_actor = loss_actor.mean()
                    # The seed will be used in the TanhTransformedDistribution:
                    entropy = actor_policy.entropy(seed=key).mean()

                    total_loss_actor = loss_actor - config.system.ent_coef * entropy
                    return total_loss_actor, (loss_actor, entropy)

                def _critic_loss_fn(
                    critic_params: FrozenDict,
                    critic_opt_state: OptState,
                    traj_batch: PPOTransition,
                    targets: chex.Array,
                ) -> Tuple:
                    """Calculate the critic loss."""
                    # RERUN NETWORK
                    value = critic_apply_fn(critic_params, traj_batch.obs)

                    # CALCULATE VALUE LOSS
                    value_pred_clipped = traj_batch.value + (value - traj_batch.value).clip(
                        -config.system.clip_eps, config.system.clip_eps
                    )
                    value_losses = jnp.square(value - targets)
                    value_losses_clipped = jnp.square(value_pred_clipped - targets)
                    value_loss = 0.5 * jnp.maximum(value_losses, value_losses_clipped).mean()

                    critic_total_loss = config.system.vf_coef * value_loss
                    return critic_total_loss, (value_loss)

                # CALCULATE ACTOR LOSS
                key, entropy_key = jax.random.split(key)
                actor_grad_fn = jax.value_and_grad(_actor_loss_fn, has_aux=True)
                actor_loss_info, actor_grads = actor_grad_fn(
                    params.actor_params,
                    opt_states.actor_opt_state,
                    traj_batch,
                    advantages,
                    entropy_key,
                )

                # CALCULATE CRITIC LOSS
                critic_grad_fn = jax.value_and_grad(_critic_loss_fn, has_aux=True)
                critic_loss_info, critic_grads = critic_grad_fn(
                    params.critic_params, opt_states.critic_opt_state, traj_batch, targets
                )

                # Compute the parallel mean (pmean) over the batch.
                # This calculation is inspired by the Anakin architecture demo notebook.
                # available at https://tinyurl.com/26tdzs5x
                # pmean over devices.
                actor_grads, actor_loss_info = jax.lax.pmean(
                    (actor_grads, actor_loss_info),
                    axis_name="device",
                )

                # pmean over devices.
                critic_grads, critic_loss_info = jax.lax.pmean(
                    (critic_grads, critic_loss_info), axis_name="device"
                )

                # UPDATE ACTOR PARAMS AND OPTIMISER STATE
                actor_updates, actor_new_opt_state = actor_update_fn(
                    actor_grads, opt_states.actor_opt_state
                )
                actor_new_params = optax.apply_updates(params.actor_params, actor_updates)

                # UPDATE CRITIC PARAMS AND OPTIMISER STATE
                critic_updates, critic_new_opt_state = critic_update_fn(
                    critic_grads, opt_states.critic_opt_state
                )
                critic_new_params = optax.apply_updates(params.critic_params, critic_updates)

                # PACK NEW PARAMS AND OPTIMISER STATE
                new_params = Params(actor_new_params, critic_new_params)
                new_opt_state = OptStates(actor_new_opt_state, critic_new_opt_state)
                # PACK LOSS INFO
                total_loss = actor_loss_info[0] + critic_loss_info[0]
                value_loss = critic_loss_info[1]
                actor_loss = actor_loss_info[1][0]
                entropy = actor_loss_info[1][1]
                loss_info = {
                    "total_loss": total_loss,
                    "value_loss": value_loss,
                    "actor_loss": actor_loss,
                    "entropy": entropy,
                }
                return (new_params, new_opt_state, entropy_key), loss_info

            params, opt_states, traj_batch, advantages, targets, key = update_state
            key, shuffle_key, entropy_key = jax.random.split(key, 3)
            # SHUFFLE MINIBATCHES
            batch_size = config.system.rollout_length * (
                config.arch.num_envs // len(config.arch.learner_device_ids)
            )
            permutation = jax.random.permutation(shuffle_key, batch_size)
            batch = (traj_batch, advantages, targets)
            batch = jax.tree_util.tree_map(lambda x: merge_leading_dims(x, 2), batch)
            shuffled_batch = jax.tree_util.tree_map(
                lambda x: jnp.take(x, permutation, axis=0), batch
            )
            minibatches = jax.tree_util.tree_map(
                lambda x: jnp.reshape(x, (config.system.num_minibatches, -1, *x.shape[1:])),
                shuffled_batch,
            )
            # UPDATE MINIBATCHES
            (params, opt_states, entropy_key), loss_info = jax.lax.scan(
                _update_minibatch, (params, opt_states, entropy_key), minibatches
            )

            update_state = (params, opt_states, traj_batch, advantages, targets, key)
            return update_state, loss_info

        update_state = (params, opt_states, traj_batch, advantages, targets, key)
        # UPDATE EPOCHS
        update_state, loss_info = jax.lax.scan(
            _update_epoch, update_state, None, config.system.ppo_epochs
        )

        params, opt_states, traj_batch, advantages, targets, key = update_state
        learner_state = LearnerState(params, opt_states, key, None, None)
        metric = traj_batch.info
        return learner_state, (metric, loss_info)

    def learner_fn(
        learner_state: LearnerState,
        traj_batch: PPOTransition,
        last_obs: Observation,
        last_dones: chex.Array,
    ) -> ExperimentOutput[LearnerState]:
        """Learner function.

        This function represents the learner, it updates the network parameters
        by iteratively applying the `_update_step` function for a fixed number of
        updates. The `_update_step` function is vectorized over a batch of inputs.

        Args:
            learner_state (NamedTuple):
                - params (Params): The initial model parameters.
                - opt_states (OptStates): The initial optimizer state.
                - key (chex.PRNGKey): The random number generator state.
                - env_state (LogEnvState): The environment state.
                - timesteps (TimeStep): The initial timestep in the initial trajectory.
        """
        learner_state, (episode_info, loss_info) = _update_step(
            learner_state, traj_batch, last_obs, last_dones
        )

        return ExperimentOutput(
            learner_state=learner_state,
            episode_metrics=episode_info,
            train_metrics=loss_info,
        )

    return learner_fn


def learner_setup(
    keys: chex.Array, config: DictConfig, learner_devices: List
) -> Tuple[
    SebulbaLearnerFn[LearnerState, PPOTransition], Tuple[ActorApply, CriticApply], LearnerState
]:
    """Initialise learner_fn, network, optimiser, environment and states."""

    # create temporory envoirnments.
    env = environments.make_gym_env(config, config.arch.num_envs)
    # Get number of agents and actions.
    action_space = env.single_action_space
    config.system.num_agents = len(action_space)
    config.system.num_actions = int(action_space[0].n)

    # PRNG keys.
    key, actor_net_key, critic_net_key = keys

    # Define network and optimiser.
    actor_torso = hydra.utils.instantiate(config.network.actor_network.pre_torso)
    actor_action_head = hydra.utils.instantiate(
        config.network.action_head, action_dim=config.system.num_actions
    )
    critic_torso = hydra.utils.instantiate(config.network.critic_network.pre_torso)

    actor_network = Actor(torso=actor_torso, action_head=actor_action_head)
    critic_network = Critic(torso=critic_torso)

    actor_lr = make_learning_rate(config.system.actor_lr, config)
    critic_lr = make_learning_rate(config.system.critic_lr, config)

    actor_optim = optax.chain(
        optax.clip_by_global_norm(config.system.max_grad_norm),
        optax.adam(actor_lr, eps=1e-5),
    )
    critic_optim = optax.chain(
        optax.clip_by_global_norm(config.system.max_grad_norm),
        optax.adam(critic_lr, eps=1e-5),
    )

    # Initialise observation: Select only obs for a single agent.
    init_obs = jnp.array([env.single_observation_space.sample()])
    init_action_mask = jnp.ones((config.system.num_agents, config.system.num_actions))
    init_x = Observation(init_obs, init_action_mask)

    # Initialise actor params and optimiser state.
    actor_params = actor_network.init(actor_net_key, init_x)
    actor_opt_state = actor_optim.init(actor_params)

    # Initialise critic params and optimiser state.
    critic_params = critic_network.init(critic_net_key, init_x)
    critic_opt_state = critic_optim.init(critic_params)

    # Pack params.
    params = Params(actor_params, critic_params)

    # Pack apply and update functions.
    apply_fns = (actor_network.apply, critic_network.apply)
    update_fns = (actor_optim.update, critic_optim.update)

    # Get batched iterated update and replicate it to pmap it over learner cores.
    learn = get_learner_fn(apply_fns, update_fns, config)
    learn = jax.pmap(learn, axis_name="device", devices=learner_devices)

    # Load model from checkpoint if specified.
    if config.logger.checkpointing.load_model:
        loaded_checkpoint = Checkpointer(
            model_name=config.logger.system_name,
            **config.logger.checkpointing.load_args,  # Other checkpoint args
        )
        # Restore the learner state from the checkpoint
        restored_params, _ = loaded_checkpoint.restore_params(input_params=params)
        # Update the params
        params = restored_params

    # Define params to be replicated across devices and batches.
    key, step_keys = jax.random.split(key)
    opt_states = OptStates(actor_opt_state, critic_opt_state)
    replicate_learner = (params, opt_states, step_keys)

    # Duplicate learner across Learner devices.
    replicate_learner = flax.jax_utils.replicate(replicate_learner, devices=learner_devices)

    # Initialise learner state.
    params, opt_states, step_keys = replicate_learner
    init_learner_state = LearnerState(params, opt_states, step_keys, None, None)
    env.close()

    return learn, apply_fns, init_learner_state


def learner(
    learn: SebulbaLearnerFn[LearnerState, PPOTransition],
    learner_state: LearnerState,
    config: DictConfig,
    learner_queue: Queue,
    pipeline: Pipeline,
    params_sources: Sequence[ParamsSource],
) -> None:
    for _eval_step in range(config.arch.num_evaluation):
        metrics: List[Tuple[Dict, Dict]] = []
        rollout_times: List[Dict] = []
        eval_times: Dict[str, List[float]] = {"evaluator_blocked_time": [], "evaluation_time": []}

        for _update in range(config.system.num_updates_per_eval):
            with RecordTimeTo(eval_times["evaluator_blocked_time"]):
                traj_batch, last_obs, last_dones, rollout_time = pipeline.get(block=True)

            with RecordTimeTo(eval_times["evaluation_time"]):
                learner_state, episode_metrics, train_metrics = learn(
                    learner_state, traj_batch, last_obs, last_dones
                )

            metrics.append((episode_metrics, train_metrics))
            rollout_times.append(rollout_time)

            unreplicated_params = flax.jax_utils.unreplicate(learner_state.params)

            for source in params_sources:
                source.update(unreplicated_params)

        # Pass to the evaluator
        episode_metrics, train_metrics = jax.tree.map(lambda *x: np.asarray(x), *metrics)

        rollout_times = jax.tree.map(lambda *x: np.mean(x), *rollout_times)
        times_dict = rollout_times | eval_times
        times_dict = jax.tree.map(np.mean, times_dict, is_leaf=lambda x: isinstance(x, list))

        learner_queue.put((episode_metrics, train_metrics, learner_state, times_dict))


def run_experiment(_config: DictConfig) -> float:
    """Runs experiment."""
    config = copy.deepcopy(_config)

    devices = jax.devices()
    learner_devices = [devices[d_id] for d_id in config.arch.learner_device_ids]

    # PRNG keys.
    key, key_e, actor_net_key, critic_net_key = jax.random.split(
        jax.random.PRNGKey(config.system.seed), num=4
    )

    # Sanity check of config
    assert (
        config.arch.num_envs % len(config.arch.learner_device_ids) == 0
    ), "The number of environments must to be divisible by the number of learners."

    assert (
        int(config.arch.num_envs / len(config.arch.learner_device_ids))
        * config.arch.n_threads_per_executor
        % config.system.num_minibatches
        == 0
    ), "int(local_num_envs / len(learner_device_ids)) must be divisible by num_minibatches."

    # Setup learner.
    learn, apply_fns, learner_state = learner_setup(
        (key, actor_net_key, critic_net_key), config, learner_devices
    )

    # Generate Numpy RNG for reproducibility
    np_rng = np.random.default_rng(config.system.seed)

    # Setup evaluator.
    # One key per device for evaluation.
    eval_act_fn = make_ff_eval_act_fn(apply_fns[0], config)
    evaluator = get_eval_fn(
        environments.make_gym_env, eval_act_fn, config, np_rng, absolute_metric=False
    )

    # Calculate total timesteps.
    config = sebulba_check_total_timesteps(config)
    assert (
        config.system.num_updates > config.arch.num_evaluation
    ), "Number of updates per evaluation must be less than total number of updates."
    # Calculate number of updates per evaluation.
    config.system.num_updates_per_eval = config.system.num_updates // config.arch.num_evaluation

    steps_per_rollout = (
        config.system.rollout_length * config.arch.num_envs * config.system.num_updates_per_eval
    )

    # Logger setup
    logger = MavaLogger(config)
    cfg: Dict = OmegaConf.to_container(config, resolve=True)
    cfg["arch"]["devices"] = jax.devices()
    pprint(cfg)

    # Set up checkpointer
    save_checkpoint = config.logger.checkpointing.save_model
    if save_checkpoint:
        checkpointer = Checkpointer(
            metadata=config,  # Save all config as metadata in the checkpoint
            model_name=config.logger.system_name,
            **config.logger.checkpointing.save_args,  # Checkpoint args
        )

    # Executor setup and launch.
    unreplicated_inital_params = flax.jax_utils.unreplicate(learner_state.params)
    params_sources: List[ParamsSource] = []
    thread_lifetimes: List[ThreadLifetime] = []
    pipeline = Pipeline(config.arh.Pilpeline_queue_size, learner_devices)
    pipeline.start()

    # Create the actor threads
    for d_idx, d_id in enumerate(config.arch.executor_device_ids):
        # Loop through each executor thread
        for thread_id in range(config.arch.n_threads_per_executor):
            seeds = np_rng.integers(np.iinfo(np.int32).max, size=config.arch.num_envs).tolist()

            params_source = ParamsSource(unreplicated_inital_params, devices[d_id])
            params_source.start()
            params_sources.append(params_source)

            lifetime = ThreadLifetime()
            thread_lifetimes.append(lifetime)

            threading.Thread(
                target=rollout,
                args=(
                    jax.device_put(key, devices[d_id]),
                    config,
                    pipeline,
                    params_sources[-1],
                    apply_fns,
                    d_id,
                    seeds,
                    lifetime,
                ),
                name=f"Actor-{thread_id + d_idx * config.arch.n_threads_per_executor}",
            ).start()

    learner_queue: Queue = Queue()
    threading.Thread(
        target=learner,
        name="Learner",
        args=(learn, learner_state, config, learner_queue, pipeline, params_sources),
    ).start()

    max_episode_return = -jnp.inf
    best_params = unreplicated_inital_params.actor_params

    for eval_step in range(config.arch.num_evaluation):
        # Get the next set of params and metrics from the evaluator
        episode_metrics, train_metrics, learner_state, times_dict = learner_queue.get()

        t = int(steps_per_rollout * (eval_step + 1))

        times_dict["timestep"] = t
        logger.log(times_dict, t, eval_step, LogEvent.MISC)

        episode_metrics, ep_completed = get_final_step_metrics(episode_metrics)
        episode_metrics["steps_per_second"] = steps_per_rollout / times_dict["single_rollout"]
        if ep_completed:
            logger.log(episode_metrics, t, eval_step, LogEvent.ACT)

        logger.log(train_metrics, t, eval_step, LogEvent.TRAIN)

        unreplicated_actor_params = flax.jax_utils.unreplicate(learner_state.params.actor_params)
        key, eval_key = jax.random.split(key, 2)
        eval_metrics = evaluator(unreplicated_actor_params, eval_key, {})
        logger.log(eval_metrics, t, eval_step, LogEvent.EVAL)

        episode_return = jnp.mean(eval_metrics["episode_return"])

        if save_checkpoint:
            # Save checkpoint of learner state
            checkpointer.save(
                timestep=steps_per_rollout * (eval_step + 1),
                unreplicated_learner_state=learner_state,
                episode_return=episode_return,
            )

        if config.arch.absolute_metric and max_episode_return <= episode_return:
            best_params = copy.deepcopy(unreplicated_actor_params)
            max_episode_return = episode_return

    for thread_lifetime in thread_lifetimes:
        thread_lifetime.stop()

    eval_performance = float(jnp.mean(eval_metrics[config.env.eval_metric]))

    # Measure absolute metric.
    if config.arch.absolute_metric:
        abs_metric_evaluator = get_eval_fn(
            environments.make_gym_env, eval_act_fn, config, np_rng, absolute_metric=True
        )
        key, eval_key = jax.random.split(key, 2)
        eval_metrics = abs_metric_evaluator(best_params, eval_key, {})

        t = int(steps_per_rollout * (eval_step + 1))
        logger.log(eval_metrics, t, eval_step, LogEvent.ABSOLUTE)

    # Stop the logger.
    logger.stop()

    return eval_performance


@hydra.main(
    config_path="../../../configs", config_name="default_ff_ippo_seb.yaml", version_base="1.2"
)
def hydra_entry_point(cfg: DictConfig) -> float:
    """Experiment entry point."""
    # Allow dynamic attributes.
    OmegaConf.set_struct(cfg, False)

    # Run experiment.
    eval_performance = run_experiment(cfg)
    print(f"{Fore.CYAN}{Style.BRIGHT}IPPO experiment completed{Style.RESET_ALL}")
    return eval_performance


if __name__ == "__main__":
    hydra_entry_point()

# learner_output.episode_metrics.keys()
# dict_keys(['episode_length', 'episode_return'])
