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

from typing import Tuple, Union

import chex
import jax.numpy as jnp
from jax import tree_util
from jumanji import specs
from jumanji.env import Environment
from jumanji.environments.routing.connector import MaConnector
from jumanji.environments.routing.connector.constants import (
    EMPTY,
    PATH,
    POSITION,
    TARGET,
)
from jumanji.environments.routing.lbf import LevelBasedForaging
from jumanji.environments.routing.multi_cvrp import MultiCVRP
from jumanji.environments.routing.multi_cvrp.types import (
    Observation as MultiCvrpObservation,
)
from jumanji.environments.routing.robot_warehouse import RobotWarehouse
from jumanji.types import TimeStep
from jumanji.wrappers import Wrapper

from mava.types import Observation, ObservationGlobalState, State


class MultiAgentWrapper(Wrapper):
    def __init__(self, env: Environment):
        super().__init__(env)
        self._num_agents = self._env.num_agents
        self.time_limit = self._env.time_limit

    def modify_timestep(self, timestep: TimeStep) -> TimeStep[Observation]:
        """Modify the timestep for `step` and `reset`."""
        pass

    def reset(self, key: chex.PRNGKey) -> Tuple[State, TimeStep]:
        """Reset the environment."""
        state, timestep = self._env.reset(key)
        return state, self.modify_timestep(timestep)

    def step(self, state: State, action: chex.Array) -> Tuple[State, TimeStep]:
        """Step the environment."""
        state, timestep = self._env.step(state, action)
        return state, self.modify_timestep(timestep)

    def observation_spec(self) -> specs.Spec[Observation]:
        """Specification of the observation of the environment."""
        step_count = specs.BoundedArray(
            (self._num_agents,),
            int,
            jnp.zeros(self._num_agents, dtype=int),
            jnp.repeat(self.time_limit, self._num_agents),
            "step_count",
        )
        return self._env.observation_spec().replace(step_count=step_count)


class RwareWrapper(MultiAgentWrapper):
    """Multi-agent wrapper for the Robotic Warehouse environment."""

    def __init__(self, env: RobotWarehouse):
        super().__init__(env)

    def modify_timestep(self, timestep: TimeStep) -> TimeStep[Observation]:
        """Modify the timestep for the Robotic Warehouse environment."""
        observation = Observation(
            agents_view=timestep.observation.agents_view,
            action_mask=timestep.observation.action_mask,
            step_count=jnp.repeat(timestep.observation.step_count, self._num_agents),
        )
        reward = jnp.repeat(timestep.reward, self._num_agents)
        discount = jnp.repeat(timestep.discount, self._num_agents)
        return timestep.replace(observation=observation, reward=reward, discount=discount)


class LbfWrapper(MultiAgentWrapper):
    """
     Multi-agent wrapper for the Level-Based Foraging environment.

    Args:
        env (Environment): The base environment.
        use_individual_rewards (bool): If true each agent gets a separate reward,
        sum reward otherwise.
    """

    def __init__(self, env: LevelBasedForaging, use_individual_rewards: bool = False):
        super().__init__(env)
        self._env: LevelBasedForaging
        self._use_individual_rewards = use_individual_rewards

    def aggregate_rewards(
        self, timestep: TimeStep, observation: Observation
    ) -> TimeStep[Observation]:
        """Aggregate individual rewards across agents."""
        team_reward = jnp.sum(timestep.reward)

        # Repeat the aggregated reward for each agent.
        reward = jnp.repeat(team_reward, self._num_agents)
        return timestep.replace(observation=observation, reward=reward)

    def modify_timestep(self, timestep: TimeStep) -> TimeStep[Observation]:
        """Modify the timestep for Level-Based Foraging environment and update
        the reward based on the specified reward handling strategy."""

        # Create a new observation with adjusted step count
        modified_observation = Observation(
            agents_view=timestep.observation.agents_view,
            action_mask=timestep.observation.action_mask,
            step_count=jnp.repeat(timestep.observation.step_count, self._num_agents),
        )
        if self._use_individual_rewards:
            # The environment returns a list of individual rewards and these are used as is.
            return timestep.replace(observation=modified_observation)

        # Aggregate the list of individual rewards and use a single team_reward.
        return self.aggregate_rewards(timestep, modified_observation)


class ConnectorWrapper(MultiAgentWrapper):
    """Multi-agent wrapper for the MA Connector environment.

    Do not use the AgentID wrapper with this env, it has implicit agent IDs.
    """

    def __init__(self, env: MaConnector, has_global_state: bool = False):
        super().__init__(env)
        self.has_global_state = has_global_state

    def modify_timestep(
        self, timestep: TimeStep
    ) -> TimeStep[Union[Observation, ObservationGlobalState]]:
        """Modify the timestep for the Connector environment."""

        # TARGET = 3 = The number of different types of items on the grid.
        def create_agents_view(grid: chex.Array) -> chex.Array:
            positions = jnp.where(grid % TARGET == POSITION, True, False)
            targets = jnp.where((grid % TARGET == 0) & (grid != EMPTY), True, False)
            paths = jnp.where(grid % TARGET == PATH, True, False)
            position_per_agent = jnp.where(grid == POSITION, True, False)
            target_per_agent = jnp.where(grid == TARGET, True, False)
            agents_view = jnp.stack(
                (positions, targets, paths, position_per_agent, target_per_agent), -1
            )
            return agents_view

        def create_global_state(grid: chex.Array) -> chex.Array:
            positions = jnp.where(grid % TARGET == POSITION, True, False)
            targets = jnp.where((grid % TARGET == 0) & (grid != EMPTY), True, False)
            paths = jnp.where(grid % TARGET == PATH, True, False)
            global_state = jnp.stack((positions, targets, paths), -1)
            return global_state

        obs_data = {
            "agents_view": create_agents_view(timestep.observation.grid),
            "action_mask": timestep.observation.action_mask,
            "step_count": jnp.repeat(timestep.observation.step_count, self._num_agents),
        }

        if self.has_global_state:
            obs_data["global_state"] = create_global_state(timestep.observation.grid)
            return timestep.replace(observation=ObservationGlobalState(**obs_data))
        else:
            return timestep.replace(observation=Observation(**obs_data))

    def observation_spec(self) -> specs.Spec[Union[Observation, ObservationGlobalState]]:
        """Specification of the observation of the environment."""
        step_count = specs.BoundedArray(
            (self._num_agents,),
            int,
            jnp.zeros(self._num_agents, dtype=int),
            jnp.repeat(self.time_limit, self._num_agents),
            "step_count",
        )

        agents_view = specs.BoundedArray(
            shape=(self._env.num_agents, self._env.grid_size, self._env.grid_size, 5),
            dtype=bool,
            name="agents_view",
            minimum=False,
            maximum=True,
        )

        if self.has_global_state:
            global_state = specs.BoundedArray(
                shape=(self._env.num_agents, self._env.grid_size, self._env.grid_size, 3),
                dtype=bool,
                name="global_state",
                minimum=False,
                maximum=True,
            )
            spec = specs.Spec(
                ObservationGlobalState,
                "ObservationSpec",
                agents_view=agents_view,
                action_mask=self._env.observation_spec().action_mask,
                global_state=global_state,
                step_count=step_count,
            )

        else:
            spec = specs.Spec(
                Observation,
                "ObservationSpec",
                agents_view=agents_view,
                action_mask=self._env.observation_spec().action_mask,
                step_count=step_count,
            )

        return spec


class MultiCVRPWrapper(Wrapper):
    """Wrapper for MultiCVRP environment."""

    def __init__(self, env: MultiCVRP, has_global_state: bool = False):
        super().__init__(env)
        self.num_agents = env._num_vehicles
        self._env = env
        self.has_global_state = has_global_state

    def reset(self, key: chex.PRNGKey) -> Tuple[State, TimeStep]:
        state, timestep = self._env.reset(key)
        timestep = self.modify_timestep(timestep, state.step_count)
        return state, timestep

    def step(self, state: State, action: chex.Array) -> Tuple[State, TimeStep]:
        state, timestep = self._env.step(state, action)
        timestep = self.modify_timestep(timestep, state.step_count)
        return state, timestep

    def modify_timestep(self, timestep: TimeStep, step_count: chex.Array) -> TimeStep[Observation]:
        # avoided the MultiAgentWrapper wrapper to use the step_count provided by the environment
        observation, global_observation = self._flatten_observation(timestep.observation)
        obs_data = {
            "agents_view": observation,
            "action_mask": timestep.observation.action_mask,
            "step_count": jnp.repeat(step_count, (self.num_agents)),
        }
        if self.has_global_state:
            obs_data["global_state"] = global_observation
            observation = ObservationGlobalState(**obs_data)
        else:
            observation = Observation(**obs_data)

        reward = jnp.repeat(timestep.reward, (self.num_agents))
        discount = jnp.repeat(timestep.discount, (self.num_agents))
        timestep = timestep.replace(observation=observation, reward=reward, discount=discount)
        return timestep

    def _flatten_observation(
        self, observation: MultiCvrpObservation
    ) -> Tuple[chex.Array, Union[None, chex.Array]]:
        """
        Concatenates all observation fields into a single array.

        Args:
            observation (MultiCvrpObservation): The raw observation NamedTuple provided by jumanji.

        Returns:
            observations (chex.Array): Concatenated individual observations for each agent,
                shaped (num_agents, vehicle_info + customer_info).
            global_observation (Union[None, chex.Array]): Concatenated global observation
                shaped (num_agents, global_info) if has_global_state = True, None otherwise.
        """
        global_observation = None
        # N: number of nodes, same as _num_customers + 1
        # V: number of vehicles, same as num_agents
        # nodes are composed of (x, y, demands)
        # Windows are composed of (start_time, end_time)
        # Coeffs are composed of (early, late)
        # Vehicles have ((x, y), local_time, capacity)

        # Tuple[(N, 3), (N, 2), (N, 2)]
        customers_info, _ = tree_util.tree_flatten(
            (observation.nodes, observation.windows, observation.coeffs)
        )
        # Tuple[(V, 2), (V, 1), (V, 1)]
        vehicles_info, _ = tree_util.tree_flatten(observation.vehicles)

        # (N * 7, )
        customers_info = jnp.column_stack(customers_info).ravel()
        # (V, 4)
        vehicles_info = jnp.column_stack(vehicles_info)

        if self.has_global_state:
            # (V * 4 * N * 7, )
            global_observation = jnp.concat((vehicles_info.ravel(), customers_info))
            # (V, N * 7 * V * 4)
            global_observation = jnp.tile(global_observation, (self.num_agents, 1))

        # (V, N * 7)
        customers_info = jnp.tile(customers_info, (self.num_agents, 1))
        # (V, 4 + N * 7)
        observations = jnp.column_stack((vehicles_info, customers_info))
        return observations, global_observation

    def observation_spec(self) -> specs.Spec[Observation]:
        step_count = specs.BoundedArray(
            (self.num_agents,), jnp.int32, 0, self._env._num_customers + 1, "step_count"
        )
        action_mask = specs.BoundedArray(
            (self.num_agents, self._env._num_customers + 1), bool, False, True, "action_mask"
        )

        agents_view = specs.BoundedArray(
            (self.num_agents, (self._env._num_customers + 1) * 7 + 4),
            jnp.float32,
            -jnp.inf,
            jnp.inf,
            "agents_view",
        )
        if self.has_global_state:
            global_state = specs.Array(
                (self.num_agents, (self._env._num_customers + 1) * 7 + 4 * self.num_agents),
                jnp.float32,
                "global_state",
            )
            return specs.Spec(
                ObservationGlobalState,
                "ObservationSpec",
                agents_view=agents_view,
                action_mask=action_mask,
                global_state=global_state,
                step_count=step_count,
            )
        return specs.Spec(
            Observation,
            "ObservationSpec",
            agents_view=agents_view,
            action_mask=action_mask,
            step_count=step_count,
        )

    def reward_spec(self) -> specs.Array:
        return specs.Array(shape=(self.num_agents,), dtype=float, name="reward")

    def discount_spec(self) -> specs.BoundedArray:
        return specs.BoundedArray(
            shape=(self.num_agents,), dtype=float, minimum=0.0, maximum=1.0, name="discount"
        )

    def action_spec(self) -> specs.Spec:
        return specs.MultiDiscreteArray(
            num_values=jnp.full(self.num_agents, self._env._num_customers + 1)
        )
