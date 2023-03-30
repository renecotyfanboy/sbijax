from collections import namedtuple
from functools import partial
from typing import Iterable

import chex
import haiku as hk
import jax
import numpy as np
import optax
from absl import logging
from flax.training.early_stopping import EarlyStopping
from jax import numpy as jnp
from jax import random

from sbijax import generator
from sbijax._sbi_base import SBI
from sbijax.generator import named_dataset
from sbijax.mcmc import sample_with_nuts
from sbijax.mcmc.sample import mcmc_diagnostics

# pylint: disable=too-many-arguments
from sbijax.mcmc.slice import sample_with_slice


class SNL(SBI):
    """
    Sequential neural likelihood

    From the Papamakarios paper
    """

    def __init__(self, model_fns, density_estimator):
        super().__init__(model_fns)
        self.model = density_estimator
        self._len_theta = len(self.prior_sampler_fn(seed=random.PRNGKey(0)))

        self.observed: chex.Array
        self._rng_seq: hk.PRNGSequence
        self._train_iter: Iterable
        self._val_iter: Iterable

    # pylint: disable=arguments-differ,too-many-locals
    def fit(
        self,
        rng_key,
        observed,
        optimizer,
        n_rounds=10,
        n_simulations_per_round=1000,
        max_n_iter=1000,
        batch_size=128,
        percentage_data_as_validation_set=0.05,
        n_samples=10000,
        n_warmup=5000,
        n_chains=4,
        n_early_stopping_patience=10,
        **kwargs,
    ):
        """
        Fit a SNL model

        Parameters
        ----------
        rng_seq: hk.PRNGSequence
            a hk.PRNGSequence
        observed: chex.Array
            (n \times p)-dimensional array of observations, where `n` is the n
            number of samples
        optimizer: optax.Optimizer
            an optax optimizer object
        n_rounds: int
            number of rounds to optimize
        n_simulations_per_round: int
            number of data simulations per round
        max_n_iter:
            maximal number of training iterations per round
        batch_size: int
            batch size used for training the model
        percentage_data_as_validation_set:
            percentage of the simulated data that is used for valitation and
            early stopping
         n_samples: int
            number of samples to draw to approximate the posterior
        n_warmup: int
            number of samples to discard
        n_chains: int
            number of chains to sample
        n_early_stopping_patience: int
            number of iterations of no improvement of training the flow
            before stopping optimisation
        kwargs: keyword arguments with sampler specific parameters. For slice
            sampling the following arguments are possible:
            - n_thin: number of thinning steps
            - n_doubling: number of doubling steps of the interval
            - step_size: step size of the initial interval

        Returns
        -------
        Tuple[pytree, Tuple]
            returns a tuple of parameters and a tuple of the training
            information
        """

        super().fit(rng_key, observed)

        simulator_fn = partial(
            self._simulate_new_data_and_append,
            n_simulations_per_round=n_simulations_per_round,
            n_chains=n_chains,
            n_samples=n_samples,
            n_warmup=n_warmup,
        )
        D, params, all_diagnostics, all_losses, all_params = (
            None,
            None,
            [],
            [],
            [],
        )
        for _ in range(n_rounds):
            D, diagnostics = simulator_fn(params, D, **kwargs)
            self._train_iter, self._val_iter = generator.as_batch_iterators(
                next(self._rng_seq),
                D,
                batch_size,
                1.0 - percentage_data_as_validation_set,
                True,
            )
            params, losses = self._fit_model_single_round(
                optimizer=optimizer,
                max_n_iter=max_n_iter,
                n_early_stopping_patience=n_early_stopping_patience,
            )
            all_params.append(params.copy())
            all_losses.append(losses)
            all_diagnostics.append(diagnostics)

        snl_info = namedtuple("snl_info", "params losses diagnostics")
        return params, snl_info(all_params, all_losses, all_diagnostics)

    # pylint: disable=arguments-differ
    def sample_posterior(self, params, n_chains, n_samples, n_warmup):
        """
        Sample from the approximate posterior

        Parameters
        ----------
        params: pytree
            a pytree of parameter for the model
        n_chains: int
        number of chains to sample
        n_samples: int
            number of samples per chain
        n_warmup: int
            number of samples to discard

        Returns
        -------
        chex.Array
            an array of samples from the posterior distribution of dimension
            (n_samples \times p)
        """

        return self._simulate_from_amortized_posterior(
            params, n_chains, n_samples, n_warmup
        )

    def _fit_model_single_round(
        self, optimizer, max_n_iter, n_early_stopping_patience
    ):
        params = self._init_params(next(self._rng_seq), self._train_iter(0))
        state = optimizer.init(params)

        @jax.jit
        def step(params, state, **batch):
            def loss_fn(params):
                lp = self.model.apply(params, method="log_prob", **batch)
                return -jnp.sum(lp)

            loss, grads = jax.value_and_grad(loss_fn)(params)
            updates, new_state = optimizer.update(grads, state, params)
            new_params = optax.apply_updates(params, updates)
            return loss, new_params, new_state

        losses = np.zeros([max_n_iter, 2])
        early_stop = EarlyStopping(1e-3, n_early_stopping_patience)
        logging.info("training model")
        for i in range(max_n_iter):
            train_loss = 0.0
            for j in range(self._train_iter.num_batches):
                batch = self._train_iter(j)
                batch_loss, params, state = step(params, state, **batch)
                train_loss += batch_loss
            validation_loss = self._validation_loss(params)
            losses[i] = jnp.array([train_loss, validation_loss])

            _, early_stop = early_stop.update(validation_loss)
            if early_stop.should_stop:
                logging.info("early stopping criterion found")
                break

        losses = jnp.vstack(losses)[:i, :]
        return params, losses

    def _validation_loss(self, params):
        def _loss_fn(**batch):
            lp = self.model.apply(params, method="log_prob", **batch)
            return -jnp.sum(lp)

        losses = jnp.array(
            [
                _loss_fn(**self._val_iter(j))
                for j in range(self._val_iter.num_batches)
            ]
        )
        return jnp.sum(losses)

    def _init_params(self, rng_key, init_data):
        params = self.model.init(rng_key, method="log_prob", **init_data)
        return params

    def _simulate_new_data_and_append(
        self,
        params,
        D,
        n_simulations_per_round,
        n_chains,
        n_samples,
        n_warmup,
        **kwargs,
    ):
        if D is None:
            diagnostics = None
            new_thetas = self.prior_sampler_fn(
                seed=next(self._rng_seq),
                sample_shape=(n_simulations_per_round,),
            )
        else:
            new_thetas, diagnostics = self._simulate_from_amortized_posterior(
                params, n_chains, n_samples, n_warmup, **kwargs
            )
            new_thetas = new_thetas.reshape(-1, self._len_theta)
            new_thetas = random.permutation(next(self._rng_seq), new_thetas)
            new_thetas = new_thetas[:n_simulations_per_round, :]

        new_obs = self.simulator_fn(seed=next(self._rng_seq), theta=new_thetas)
        new_data = named_dataset(new_obs, new_thetas)
        if D is None:
            d_new = new_data
        else:
            d_new = named_dataset(
                *[jnp.vstack([a, b]) for a, b in zip(D, new_data)]
            )
        return d_new, diagnostics

    def _simulate_from_amortized_posterior(
        self, params, n_chains, n_samples, n_warmup, **kwargs
    ):
        part = partial(
            self.model.apply, params=params, method="log_prob", y=self.observed
        )

        def _log_likelihood_fn(theta):
            theta = jnp.tile(theta, [self.observed.shape[0], 1])
            return part(x=theta)

        def _joint_logdensity_fn(theta):
            lp_prior = self.prior_log_density_fn(theta)
            lp = _log_likelihood_fn(theta)
            return jnp.sum(lp) + jnp.sum(lp_prior)

        if "sampler" in kwargs and kwargs["sampler"] == "slice":

            def lp__(theta):
                return jax.vmap(_joint_logdensity_fn)(theta)

            kwargs.pop("sampler", None)
            samples = sample_with_slice(
                self._rng_seq,
                lp__,
                n_chains,
                n_samples,
                n_warmup,
                self.prior_sampler_fn,
                **kwargs,
            )
        else:

            def lp__(theta):
                return _joint_logdensity_fn(**theta)

            samples = sample_with_nuts(
                self._rng_seq,
                lp__,
                self._len_theta,
                n_chains,
                n_samples,
                n_warmup,
            )
        diagnostics = mcmc_diagnostics(samples)
        return samples, diagnostics
