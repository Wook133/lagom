import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from gym.spaces import Discrete
from gym.spaces import Box
from gym.spaces import flatdim

from lagom import BaseAgent
from lagom.utils import pickle_dump
from lagom.utils import tensorify
from lagom.utils import numpify
from lagom.envs.wrappers import get_wrapper
from lagom.networks import Module
from lagom.networks import make_fc
from lagom.networks import ortho_init
from lagom.networks import CategoricalHead
from lagom.networks import DiagGaussianHead
from lagom.networks import linear_lr_scheduler
from lagom.metric import vtrace
from lagom.transform import explained_variance as ev
from lagom.transform import describe


class MLP(Module):
    def __init__(self, config, env, device, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.env = env
        self.device = device
        
        self.feature_layers = make_fc(flatdim(env.observation_space), config['nn.sizes'])
        for layer in self.feature_layers:
            ortho_init(layer, nonlinearity='relu', constant_bias=0.0)
        self.layer_norms = nn.ModuleList([nn.LayerNorm(hidden_size) for hidden_size in config['nn.sizes']])
        
        self.to(self.device)
        
    def forward(self, x):
        for layer, layer_norm in zip(self.feature_layers, self.layer_norms):
            x = layer_norm(F.relu(layer(x)))
        return x


class Agent(BaseAgent):
    def __init__(self, config, env, device, **kwargs):
        super().__init__(config, env, device, **kwargs)
        
        feature_dim = config['nn.sizes'][-1]
        self.feature_network = MLP(config, env, device, **kwargs)
        if isinstance(env.action_space, Discrete):
            self.action_head = CategoricalHead(feature_dim, env.action_space.n, device, **kwargs)
        elif isinstance(env.action_space, Box):
            self.action_head = DiagGaussianHead(feature_dim, flatdim(env.action_space), device, config['agent.std0'], **kwargs)
        self.V_head = nn.Linear(feature_dim, 1)
        ortho_init(self.V_head, weight_scale=1.0, constant_bias=0.0)
        self.V_head = self.V_head.to(device)  # reproducible between CPU/GPU, ortho_init behaves differently
        
        self.register_buffer('total_timestep', torch.tensor(0))
        #self.total_timestep = 0
        
        self.optimizer = optim.Adam(self.parameters(), lr=config['agent.lr'])
        if config['agent.use_lr_scheduler']:
            self.lr_scheduler = linear_lr_scheduler(self.optimizer, config['train.timestep'], min_lr=1e-8)
        self.gamma = config['agent.gamma']
        self.clip_rho = config['agent.clip_rho']
        self.clip_pg_rho = config['agent.clip_pg_rho']
        
    def choose_action(self, obs, **kwargs):
        obs = tensorify(obs, self.device)
        out = {}
        features = self.feature_network(obs)
        
        action_dist = self.action_head(features)
        out['action_dist'] = action_dist
        out['entropy'] = action_dist.entropy()
        
        action = action_dist.sample()
        out['action'] = action
        out['raw_action'] = numpify(action, self.env.action_space.dtype)
        out['action_logprob'] = action_dist.log_prob(action.detach())
        
        V = self.V_head(features)
        out['V'] = V
        return out
    
    def learn(self, D, **kwargs):
        # Compute all metrics, D: list of Trajectory
        Ts = [len(traj) for traj in D]
        behavior_logprobs = [torch.cat(traj.get_all_info('action_logprob')) for traj in D]
        out_agent = self.choose_action(np.concatenate([traj.numpy_observations[:-1] for traj in D], 0))
        logprobs = out_agent['action_logprob'].squeeze()
        entropies = out_agent['entropy'].squeeze()
        Vs = out_agent['V'].squeeze()
        with torch.no_grad():
            last_observations = tensorify(np.concatenate([traj.last_observation for traj in D], 0), self.device)
            last_Vs = self.V_head(self.feature_network(last_observations)).squeeze(-1)

        vs, As = [], []
        for traj, behavior_logprob, logprob, V, last_V in zip(D, behavior_logprobs, logprobs.detach().cpu().split(Ts), 
                                                              Vs.detach().cpu().split(Ts), last_Vs):
            v, A = vtrace(behavior_logprob, logprob, self.gamma, traj.rewards, V, last_V, 
                          traj.reach_terminal, self.clip_rho, self.clip_pg_rho)
            vs.append(v)
            As.append(A)
    
        # Metrics -> Tensor, device
        vs, As = map(lambda x: tensorify(np.concatenate(x).copy(), self.device), [vs, As])
        if self.config['agent.standardize_adv']:
            As = (As - As.mean())/(As.std() + 1e-8)
        
        assert all([x.ndimension() == 1 for x in [logprobs, entropies, Vs, vs, As]])
        
        # Loss
        policy_loss = -logprobs*As
        entropy_loss = -entropies
        value_loss = F.mse_loss(Vs, vs, reduction='none')
        
        loss = policy_loss + self.config['agent.value_coef']*value_loss + self.config['agent.entropy_coef']*entropy_loss
        loss = loss.mean()
        
        self.optimizer.zero_grad()
        loss.backward()
        grad_norm = nn.utils.clip_grad_norm_(self.parameters(), self.config['agent.max_grad_norm'])
        self.optimizer.step()
        if self.config['agent.use_lr_scheduler']:
            self.lr_scheduler.step(self.total_timestep)
        
        self.total_timestep += sum([len(traj) for traj in D])
        out = {}
        if self.config['agent.use_lr_scheduler']:
            out['current_lr'] = self.lr_scheduler.get_lr()
        out['loss'] = loss.item()
        out['grad_norm'] = grad_norm
        out['policy_loss'] = policy_loss.mean().item()
        out['entropy_loss'] = entropy_loss.mean().item()
        out['policy_entropy'] = -entropy_loss.mean().item()
        out['value_loss'] = value_loss.mean().item()
        out['V'] = describe(numpify(Vs, 'float').squeeze(), axis=-1, repr_indent=1, repr_prefix='\n')
        out['explained_variance'] = ev(y_true=numpify(vs, 'float'), y_pred=numpify(Vs, 'float'))
        return out
    
    def checkpoint(self, logdir, num_iter):
        self.save(logdir/f'agent_{num_iter}.pth')
        obs_env = get_wrapper(self.env, 'VecStandardizeObservation')
        if obs_env is not None:
            pickle_dump(obj=(obs_env.mean, obs_env.var), f=logdir/f'obs_moments_{num_iter}', ext='.pth')