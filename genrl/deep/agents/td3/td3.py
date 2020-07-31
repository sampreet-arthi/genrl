from copy import deepcopy
from typing import Any, Dict, Optional, Tuple, Union

import gym
import numpy as np
import torch
import torch.nn as nn

from genrl.deep.common import (
    ReplayBuffer,
    get_env_properties,
    get_model,
    safe_mean,
    set_seeds,
)
from genrl.environments import VecEnv


class TD3:
    """
    Twin Delayed DDPG

    Paper: https://arxiv.org/abs/1509.02971
    """

    def __init__(
        self,
        *args,
        polyak: float = 0.995,
        noise: Optional[Any] = None,
        noise_std: float = 0.1,
        **kwargs,
    ):
        super(TD3, self).__init__(*args, **kwargs)
        self.polyak = polyak
        self.noise = noise
        self.noise_std = noise_std

        self.empty_logs()
        if self.create_model:
            self._create_model()

    def _create_model(self) -> None:
        state_dim, action_dim, discrete, _ = get_env_properties(self.env)
        if discrete:
            raise Exception(
                "Discrete Environments not supported for {}.".format(__class__.__name__)
            )
        if self.noise is not None:
            self.noise = self.noise(
                np.zeros_like(action_dim), self.noise_std * np.ones_like(action_dim)
            )

        self.ac = get_model("ac", self.network_type)(
            state_dim, action_dim, self.layers, "Qsa", False
        ).to(self.device)

        self.ac.qf1 = self.ac.critic.to(self.device)
        self.ac.qf2 = get_model("v", self.network_type)(
            state_dim, action_dim, hidden=self.layers, val_type="Qsa"
        ).to(self.device)

        self.ac_target = deepcopy(self.ac).to(self.device)

        # freeze target network params
        for param in self.ac_target.parameters():
            param.requires_grad = False

        self.replay_buffer = ReplayBuffer(self.replay_size, self.env)
        self.q_params = list(self.ac.qf1.parameters()) + list(self.ac.qf2.parameters())
        self.optimizer_q = torch.optim.Adam(self.q_params, lr=self.lr_q)

        self.optimizer_policy = torch.optim.Adam(
            self.ac.actor.parameters(), lr=self.lr_p
        )

    def update_params_before_select_action(self, timestep: int) -> None:
        """
        Update any parameters before selecting action like epsilon for decaying epsilon greedy

        :param timestep: Timestep in the training process
        :type timestep: int
        """
        pass

    def select_action(
        self, state: np.ndarray, deterministic: bool = False
    ) -> np.ndarray:
        with torch.no_grad():
            action = self.ac_target.get_action(
                torch.as_tensor(state, dtype=torch.float32, device=self.device),
                deterministic=deterministic,
            )[0].numpy()

        # add noise to output from policy network
        if self.noise is not None:
            action += self.noise()

        return np.clip(
            action, -self.env.action_space.high[0], self.env.action_space.high[0]
        )

    def get_q_loss(
        self,
        state: np.ndarray,
        action: np.ndarray,
        reward: np.ndarray,
        next_state: np.ndarray,
        done: np.ndarray,
    ) -> torch.Tensor:
        q1 = self.ac.qf1.get_value(torch.cat([state, action], dim=-1))
        q2 = self.ac.qf2.get_value(torch.cat([state, action], dim=-1))

        with torch.no_grad():
            target_q1 = self.ac_target.qf1.get_value(
                torch.cat(
                    [
                        next_state,
                        self.ac_target.get_action(next_state, deterministic=True)[0],
                    ],
                    dim=-1,
                )
            )
            target_q2 = self.ac_target.qf2.get_value(
                torch.cat(
                    [
                        next_state,
                        self.ac_target.get_action(next_state, deterministic=True)[0],
                    ],
                    dim=-1,
                )
            )
            target_q = torch.min(target_q1, target_q2).unsqueeze(1)

            target = reward.squeeze(1) + self.gamma * (1 - done) * target_q.squeeze(1)

        l1 = nn.MSELoss()(q1, target)
        l2 = nn.MSELoss()(q2, target)

        return l1 + l2

    def get_p_loss(self, state: np.array) -> torch.Tensor:
        q_pi = self.ac.get_value(
            torch.cat([state, self.ac.get_action(state, deterministic=True)[0]], dim=-1)
        )
        return -torch.mean(q_pi)

    def update_params(self, update_interval: int) -> None:
        for timestep in range(update_interval):
            batch = self.replay_buffer.sample(self.batch_size)
            state, action, reward, next_state, done = (x.to(self.device) for x in batch)
            self.optimizer_q.zero_grad()
            # print(state.shape, action.shape, reward.shape, next_state.shape, done.shape)
            loss_q = self.get_q_loss(state, action, reward, next_state, done)
            loss_q.backward()
            self.optimizer_q.step()

            # Delayed Update
            if timestep % self.policy_frequency == 0:
                # freeze critic params for policy update
                for param in self.q_params:
                    param.requires_grad = False

                self.optimizer_policy.zero_grad()
                loss_p = self.get_p_loss(state)
                loss_p.backward()
                self.optimizer_policy.step()

                # unfreeze critic params
                for param in self.ac.critic.parameters():
                    param.requires_grad = True

                # update target network
                with torch.no_grad():
                    for param, param_target in zip(
                        self.ac.parameters(), self.ac_target.parameters()
                    ):
                        param_target.data.mul_(self.polyak)
                        param_target.data.add_((1 - self.polyak) * param.data)

                self.logs["policy_loss"].append(loss_p.item())
                self.logs["value_loss"].append(loss_q.item())

    def learn(self) -> None:  # pragma: no cover
        state, episode_reward, episode_len, episode = (
            self.env.reset(),
            np.zeros(self.env.n_envs),
            np.zeros(self.env.n_envs),
            np.zeros(self.env.n_envs),
        )
        total_steps = self.steps_per_epoch * self.epochs * self.env.n_envs

        if self.noise is not None:
            self.noise.reset()

        for timestep in range(0, total_steps, self.env.n_envs):
            # execute single transition
            if timestep > self.start_steps:
                action = self.select_action(state)
            else:
                action = self.env.sample()

            next_state, reward, done, _ = self.env.step(action)
            if self.render:
                self.env.render()
            episode_reward += reward
            episode_len += 1

            # dont set d to True if max_ep_len reached
            # done = self.env.n_envs*[False] if np.any(episode_len == self.max_ep_len) else done
            done = np.array(
                [
                    False if episode_len[i] == self.max_ep_len else done[i]
                    for i, ep_len in enumerate(episode_len)
                ]
            )

            self.replay_buffer.extend(zip(state, action, reward, next_state, done))

            state = next_state

            if np.any(done) or np.any(episode_len == self.max_ep_len):

                if sum(episode) % 20 == 0:
                    print(
                        "Ep: {}, reward: {}, t: {}".format(
                            sum(episode), np.mean(episode_reward), timestep
                        )
                    )

                for i, di in enumerate(done):
                    # print(d)
                    if di or episode_len[i] == self.max_ep_len:
                        episode_reward[i] = 0
                        episode_len[i] = 0
                        episode += 1

                if self.noise is not None:
                    self.noise.reset()

                state, episode_reward, episode_len = (
                    self.env.reset(),
                    np.zeros(self.env.n_envs),
                    np.zeros(self.env.n_envs),
                )
                episode += 1

            # update params
            if timestep >= self.start_update and timestep % self.update_interval == 0:
                self.update_params(self.update_interval)

        self.env.close()

    def get_hyperparams(self) -> Dict[str, Any]:
        hyperparams = {
            "network_type": self.network_type,
            "gamma": self.gamma,
            "lr_p": self.lr_p,
            "lr_q": self.lr_q,
            "polyak": self.polyak,
            "policy_frequency": self.policy_frequency,
            "noise_std": self.noise_std,
            "q1_weights": self.ac.qf1.state_dict(),
            "q2_weights": self.ac.qf2.state_dict(),
            "actor_weights": self.ac.actor.state_dict(),
        }

        return hyperparams

    def load_weights(self, weights) -> None:
        """
        Load weights for the agent from pretrained model
        """
        self.ac.actor.load_state_dict(weights["actor_weights"])
        self.ac.qf1.load_state_dict(weights["q1_weights"])
        self.ac.qf2.load_state_dict(weights["q2_weights"])

    def get_logging_params(self) -> Dict[str, Any]:
        """
        :returns: Logging parameters for monitoring training
        :rtype: dict
        """
        logs = {
            "policy_loss": safe_mean(self.logs["policy_loss"]),
            "value_loss": safe_mean(self.logs["value_loss"]),
        }

        self.empty_logs()
        return logs

    def empty_logs(self):
        """
        Empties logs
        """
        self.logs = {}
        self.logs["policy_loss"] = []
        self.logs["value_loss"] = []
