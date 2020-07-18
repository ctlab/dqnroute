import numpy as np
import torch
from typing import *
from abc import ABC, abstractmethod

from ml_util import Util


class Adversary(ABC):
    """
    Base class for adversaries. Adversaries can perturb vectors given the gradient pointing to the direction
    of making the prediction worse.
    """
    
    @abstractmethod
    def perturb(self, initial_vector: torch.tensor,
                get_gradient: Callable[[torch.tensor], Tuple[torch.tensor, float]]) -> torch.tensor:
        """
        Perturb the given vector. 
        :param initial_vector: initial vector. If this is the original image representation, it must be flattened
            prior to the call (more precisely, it must be of size [1, the_rest]).
        :param get_gradient: a get_gradient function. It accepts the current vector and returns a tuple
            (gradient pointing to the direction of the adversarial attack, the corresponding loss function value).
        :return: the pertured vector of the same size as initial_vector.
        """
        pass

    
class NopAdversary(Adversary):
    """
    Dummy adversary that acts like an identity function.
    """
    
    def perturb(self, initial_vector: torch.tensor,
                get_gradient: Callable[[torch.tensor], Tuple[torch.tensor, float]]) -> torch.tensor:
        return initial_vector
    

class FGSMAdversary(Adversary):
    """
    Implements Fast Gradient Sign Method (FGSM).
    """
    
    def __init__(self, eps: float):
        """
        Constructs FGSMAdversary.
        :param eps: positive number used to scale the sign of the gradient.
        """
        super().__init__()
        self.eps = eps
        
    def perturb(self, initial_vector: torch.tensor,
                get_gradient: Callable[[torch.tensor], Tuple[torch.tensor, float]]) -> torch.tensor:
        return initial_vector + self.eps * torch.sign(get_gradient(initial_vector)[0])


class PGDAdversary(Adversary):
    """
    Performes Projected Gradient Descent (PGD), or, more precisely, PG ascent according to the provided gradient.
    """
    
    def __init__(self, rho: float = 0.1, steps: int = 25, step_size: float = 0.1, random_start: bool = True,
                 stop_loss: float = 0, verbose: int = 1, norm: str = "scaled_l_2",
                 n_repeat: int = 1, repeat_mode: str = None, unit_sphere_normalization: bool = False):
        """
        Constructs PGDAdversary. 
        :param rho > 0: bound on perturbation norm.
        :param steps: number of steps to perform in each run. Less steps can be done if stop_loss is reached.
        :param step_size: step size. Each step will be of magnitude rho * step_size.
        :param random_start: if True, start search in a vector with a uniformly random radius within the rho-ball.
            Otherwise, start in the center of the rho-ball.
        :param stop_loss: the search will stop when this value of the "loss" function is exceeded.
        :param verbose: 0 (silent), 1 (regular), 2 (verbose).
        :param norm: one of 'scaled_l_2' (default), 'l_2' or 'l_inf'.
        :param n_repeat: number of times to run PGD.
        :param repeat_mode: 'any' or 'min': In mode 'any', n_repeat runs are identical and any run that reaches
            stop_loss will prevent subsequent runs. In mode 'min', all runs will be performed, and if a run
            finds a smaller perturbation according to norm, it will tighten rho on the next run.
        :param unit_sphere_normalization: search perturbations on the unit sphere (according to the scaled L2 norm)
            instead of the entire latent space.
        """
        super().__init__()
        self.rho = rho
        self.steps = steps
        self.step_size = step_size
        self.random_start = random_start
        self.stop_loss = stop_loss
        self.verbose = verbose
        # checks on norms
        assert norm in ["scaled_l_2", "l_2", "l_inf"], "norm must be either 'scaled_l_2', 'l_2' or 'l_inf'"
        self.scale_norm = norm == "scaled_l_2"
        self.inf_norm = norm == "l_inf"
        # checks on repeated runs
        assert n_repeat >= 1, "n_repeat must be positive"
        assert not(n_repeat > 1 and repeat_mode is None), "if n_repeat > 1, repeat_mode must be set" 
        assert repeat_mode in [None, "any", "min"], "if repeat_mode is set, it must be either 'any' or 'min'"
        self.n_repeat = n_repeat
        self.shrinking_repeats = repeat_mode == "min"
        # optional unit sphere normalization
        self.unit_sphere_normalization = unit_sphere_normalization
        assert not unit_sphere_normalization or norm == "scaled_l_2",\
            "unit_sphere_normalization is only compatible with scaled_l_2 norm"
    
    def norm_(self, x: torch.tensor) -> float:
        """
        (Possibly scaled) norm of x.
        """
        return x.norm(np.infty if self.inf_norm else 2).item() / (np.sqrt(x.numel()) if self.scale_norm else 1)
    
    def normalize_gradient_(self, x: torch.tensor) -> torch.tensor:
        """
        Normalizes the vector of gradients.
        In the L2 space, this is done by dividing the vector by its norm.
        In the L-inf space, this is done by taking the sign of the gradient.
        """
        return x.sign() if self.inf_norm else (x / self.norm_(x))
    
    def project_(self, x: torch.tensor, rho: float) -> torch.tensor:
        """
        Projects the vector onto the rho-ball.
        In the L2 space, this is done by scaling the vector.
        In the L-inf space, this is done by clamping all components independently.
        """
        return x.clamp(-rho, rho) if self.inf_norm else (x / self.norm_(x) * rho)
    
    def perturb(self, initial_vector: torch.tensor,
                get_gradient: Callable[[torch.tensor], Tuple[torch.tensor, float]]) -> torch.tensor:
        best_perturbation = None
        best_perturbation_norm = np.infty
        # rho may potentially shrink with repeat_mode == "min":
        rho = self.rho
        random_start = self.random_start
        for run_n in range(self.n_repeat):
            x1 = initial_vector * 1
            perturbation = x1 * 0

            if random_start:
                # random vector within the rho-ball
                if self.inf_norm:
                    # uniform
                    perturbation = (torch.rand(1, x1.numel()) - 0.5) * 2 * rho
                    # possibly reduce radius to encourage search of vectors with smaller norms
                    perturbation *= np.random.rand()
                else:
                    # uniform radius, random direction
                    # note that this distribution is not uniform in terms of R^n!
                    perturbation = torch.randn(1, x1.numel())
                    perturbation /= self.norm_(perturbation) / rho
                    perturbation *= np.random.rand()
                perturbation = Util.conditional_to_cuda(perturbation)

            if self.verbose > 0:
                print(f">> #run = {run_n}, ║x1║ = {self.norm_(x1):.5f}, ρ = {rho:.5f}")

            found = False
            for i in range(self.steps):
                perturbed_vector = x1 + perturbation
                perturbed_vector, perturbation = self.recompute_with_unit_sphere_normalization_(perturbed_vector, perturbation)
                classification_gradient, classification_loss = get_gradient(perturbed_vector)
                if self.verbose > 0:
                    if classification_loss > self.stop_loss or i == self.steps - 1 or i % 5 == 0 and self.verbose > 1:
                        print(f"step {i:3d}: objective = {-classification_loss:7f}, "
                              f"║Δx║ = {self.norm_(perturbation):.5f}, ║x║ = {self.norm_(perturbed_vector):.5f}")
                if classification_loss > self.stop_loss:
                    found = True
                    break
                # learning step
                perturbation_step = rho * self.step_size * self.normalize_gradient_(classification_gradient)
                perturbation_step = self.adjust_step_for_unit_sphere_(perturbation_step, x1 + perturbation)
                perturbation += perturbation_step
                # projecting on rho-ball around x1
                if self.norm_(perturbation) > rho:
                    perturbation = self.project_(perturbation, rho)
            
            # end of run
            if found:
                if self.shrinking_repeats:
                    if self.norm_(perturbation) < best_perturbation_norm:
                        best_perturbation_norm = self.norm_(perturbation)
                        best_perturbation = perturbation
                        rho = best_perturbation_norm
                else: # regular repeats
                    # return immediately
                    return self.optional_normalize_(x1 + perturbation)
            if best_perturbation is None:
                best_perturbation = perturbation
            if self.shrinking_repeats and run_n == self.n_repeat - 1:
                # heuristic: the last run is always from the center
                random_start = False
        return self.optional_normalize_(x1 + best_perturbation)
    
    ### projections on the unit sphere: ###
    
    def optional_normalize_(self, x: torch.tensor):
        """
        Optional unit sphere normalization.
        :param x: vector of shape 1*dim to normalize.
        :return optionally normalized x.
        """
        return Util.normalize_latent(x) if self.unit_sphere_normalization else x
    
    def recompute_with_unit_sphere_normalization_(self, perturbed_vector: torch.tensor, perturbation: torch.tensor):
        """
        If unit sphere normalization is enabled, the perturbed vector is projected on the unit sphere,
        and the perturbation vector is recomputed accordingly. Otherwise, returns the inputs unmodified.
        :param perturbed_vector: perturbed vector (initial vector + perturbation).
        :param perturbation: perturbation vector.
        :return possibly recomputed (perturbed_vector, perturbation).
        """
        effective_perturbed_vector = self.optional_normalize_(perturbed_vector)
        return effective_perturbed_vector, perturbation + effective_perturbed_vector - perturbed_vector
    
    def adjust_step_for_unit_sphere_(self, perturbation_step: torch.tensor, previous_perturbed_vector: torch.tensor):
        """
        If unit sphere normalization is enabled, multiplies perturbation_step by a coefficient that approximately
        compensates for the reduction of the learning step due to projection of a unit sphere.
        :param perturbation_step: unmodified pertubation step.
        :param previous_perturbed_vector: previous perturbed vector.
        :return altered perturbation_step.
        """
        new_perturbed_vector = self.optional_normalize_(previous_perturbed_vector + perturbation_step)
        effective_perturbation_step = new_perturbed_vector - previous_perturbed_vector
        coef = perturbation_step.norm() / effective_perturbation_step.norm()
        return perturbation_step * coef