
import copy
import time
from typing import Any, Dict, Tuple, List
import threading
import chex
import flax
import hydra
import jax
import jax.numpy as jnp
import numpy as np
import optax
import queue
from collections import deque
from colorama import Fore, Style
from flax.core.frozen_dict import FrozenDict
from omegaconf import DictConfig, OmegaConf
from optax._src.base import OptState
from rich.pretty import pprint

from mava.evaluator import make_eval_fns
from mava.networks import FeedForwardActor as Actor
from mava.networks import FeedForwardValueNet as Critic
from mava.systems.anakin.ppo.types import LearnerState, OptStates, Params, PPOTransition #todo: change this
from mava.types import ActorApply, CriticApply, ExperimentOutput, LearnerFn, Observation
from mava.utils import make_env as environments
from mava.utils.checkpointing import Checkpointer
from mava.utils.jax_utils import (
    merge_leading_dims,
    unreplicate_batch_dim,
    unreplicate_n_dims,
)
from mava.utils.logger import LogEvent, MavaLogger
from mava.utils.total_timestep_checker import anakin_check_total_timesteps
from mava.utils.training import make_learning_rate
from mava.wrappers.episode_metrics import get_final_step_metrics
from flax import linen as nn
import gym
from mava.wrappers import GymRwareWrapper
@hydra.main(config_path="../../../configs", config_name="default_ff_ippo_seb.yaml", version_base="1.2")
def hydra_entry_point(cfg: DictConfig) -> float:
    """Experiment entry point."""
    # Allow dynamic attributes.
    OmegaConf.set_struct(cfg, False)


    base = gym.make(cfg.env.scenario)
    base = GymRwareWrapper(base, cfg.env.use_individual_rewards, False, True)
    base.reset()
    ree = base.step([0,0])
    print(ree)
    env = environments.make_gym_env(cfg)
    a = env.reset()
    print(a)
    b = env.step([[0,0], [0,0], [0,0], [0,0]])
    #print(b)
    #r = 1+1
    # Create a sample input
    #env = gym.make(cfg.env.scenario)
    #env.reset()
    #a = env.step(jnp.ones((4)))

hydra_entry_point()