#!/usr/local/bin/env python

# ==============================================================================
# MODULE DOCSTRING
# ==============================================================================

"""
MultiStateAnalyzers
===================

Analysis tools and module for MultiStateSampler simulations. Provides programmatic and automatic
"best practices" integration to determine free energy and other observables.

Fully extensible to support new samplers and observables.


"""

# =============================================================================================
# MODULE IMPORTS
# =============================================================================================

import abc
import copy
import inspect
import logging
import re
from typing import Optional, NamedTuple, Union

import mdtraj
import numpy as np
from simtk import openmm
import simtk.unit as units
from pymbar import MBAR, timeseries
import openmmtools as mmtools

from . import utils

ABC = abc.ABC
logger = logging.getLogger(__name__)

__all__ = [
    'PhaseAnalyzer',
    'MultiStateSamplerAnalyzer',
    'MultiPhaseAnalyzer',
    'ObservablesRegistry',
    'default_observables_registry'
]

# =============================================================================================
# PARAMETERS
# =============================================================================================

kB = units.BOLTZMANN_CONSTANT_kB * units.AVOGADRO_CONSTANT_NA


# =============================================================================================
# UTILITY FUNCTIONS
# =============================================================================================

def compute_centroid_distance(positions_group1, positions_group2, weights_group1, weights_group2):
    """Compute the distance between the centers of mass of the two groups.

    The two positions given must have the same units.

    Parameters
    ----------
    positions_group1 : numpy.array
        The positions of the particles in the first CustomCentroidBondForce group.
    positions_group2 : numpy.array
        The positions of the particles in the second CustomCentroidBondForce group.
    weights_group1 : list of float
        The mass of the particle in the first CustomCentroidBondForce group.
    weights_group2 : list of float
        The mass of the particles in the second CustomCentroidBondForce group.

    """
    assert len(positions_group1) == len(weights_group1)
    assert len(positions_group2) == len(weights_group2)
    # Compute center of mass for each group.
    com_group1 = np.average(positions_group1, axis=0, weights=weights_group1)
    com_group2 = np.average(positions_group2, axis=0, weights=weights_group2)
    # Compute distance between centers of mass.
    distance = np.linalg.norm(com_group1 - com_group2)
    return distance


# =============================================================================================
# MODULE CLASSES
# =============================================================================================

class ObservablesRegistry(object):
    """
    Registry of computable observables.

    This is a class accessed by the :class:`PhaseAnalyzer` objects to check
    which observables can be computed, and then provide a regular categorization of them.

    This registry is a required linked component of any PhaseAnalyzer and especially of the MultiPhaseAnalyzer.
    This is not an internal class to the PhaseAnalyzer however because it can be instanced, extended, and customized
    as part of the API for this module.

    To define your own methods:
    1) Choose a unique observable name.
    2) Categorize the observable in one of the following ways by adding to the list in the "observables_X" method:

        2a) "defined_by_phase":
            Depends on the Phase as a whole (state independent)

        2b) "defined_by_single_state":
            Computed entirely from one state, e.g. Radius of Gyration

        2c) "defined_by_two_states":
            Property is relative to some reference state, such as Free Energy Difference

    3) Optionally categorize the error category calculation in the "observables_with_error_adding_Y" methods
       If not placed in an error category, the observable will be assumed not to carry error
       Examples: A, B, C are the observable in 3 phases, eA, eB, eC are the error of the observable in each phase

        3a) "linear": Error between phases adds linearly.
            If C = A + B, eC = eA + eB

        3b) "quadrature": Error between phases adds in the square.
            If C = A + B, eC = sqrt(eA^2 + eB^2)

    4) Finally, to add this observable to the phase, implement a "get_{method name}" method to the subclass of
       :class:`YankPhaseAnalyzer`. Any :class:`MultiPhaseAnalyzer` composed of this phase will automatically have the
       "get_{method name}" if all other phases in the :class:`MultiPhaseAnalyzer` have the same method.
    """

    def __init__(self):
        """Register Defaults"""
        # Create empty registry
        self._observables = {'two_state': set(),
                             'one_state': set(),
                             'phase': set()}
        self._errors = {'quad': set(),
                        'linear': set(),
                        None: set()}

    def register_two_state_observable(self, name: str,
                                      error_class: Optional[str]=None,
                                      re_register: bool=False):
        """
        Register a new two state observable, or re-register an existing one.

        Parameters
        ----------
        name: str
            Name of the observable, will be cast to all lower case and spaces replaced with underscores
        error_class: "quad", "linear", or None
            How the error of the observable is computed when added with other errors from the same observable.

            * "quad": Adds in the quadrature, Observable C = A + B, Error eC = sqrt(eA**2 + eB**2)

            * "linear": Adds linearly,  Observable C = A + B, Error eC = eA + eB

            * None: Does not carry error

        re_register: bool, optional, Default: False
            Re-register an existing observable
        """

        self._register_observable(name, "two_state", error_class, re_register=re_register)

    def register_one_state_observable(self, name: str,
                                      error_class: Optional[str]=None,
                                      re_register: bool=False):
        """
        Register a new one state observable, or re-register an existing one.

        Parameters
        ----------
        name: str
            Name of the observable, will be cast to all lower case and spaces replaced with underscores
        error_class: "quad", "linear", or None
            How the error of the observable is computed when added with other errors from the same observable.

            * "quad": Adds in the quadrature, Observable C = A + B, Error eC = sqrt(eA**2 + eB**2)

            * "linear": Adds linearly,  Observable C = A + B, Error eC = eA + eB

            * None: Does not carry error

        re_register: bool, optional, Default: False
            Re-register an existing observable
        """

        self._register_observable(name, "one_state", error_class, re_register=re_register)

    def register_phase_observable(self, name: str,
                                  error_class: Optional[str]=None,
                                  re_register: bool=False):
        """
        Register a new observable defined by phaee, or re-register an existing one.

        Parameters
        ----------
        name: str
            Name of the observable, will be cast to all lower case and spaces replaced with underscores
        error_class: 'quad', 'linear', or None
            How the error of the observable is computed when added with other errors from the same observable.

            * 'quad': Adds in the quadrature, Observable C = A + B, Error eC = sqrt(eA**2 + eB**2)

            * 'linear': Adds linearly,  Observable C = A + B, Error eC = eA + eB

            * None: Does not carry error

        re_register: bool, optional, Default: False
            Re-register an existing observable

        """

        self._register_observable(name, "phase", error_class, re_register=re_register)

    ########################
    # Define the observables
    ########################
    @property
    def observables(self):
        """
        Set of observables which are derived from the subsets below
        """
        observables = set()
        for subset_key in self._observables:
            observables |= self._observables[subset_key]
        return tuple(observables)

    # ------------------------------------------------
    # Exclusive Observable categories
    # The intersection of these should be the null set
    # ------------------------------------------------

    @property
    def observables_defined_by_two_states(self):
        """
        Observables that require an i and a j state to define the observable accurately between phases
        """
        return self._get_observables('two_state')

    @property
    def observables_defined_by_single_state(self):
        """
        Defined observables which are fully defined by a single state, and not by multiple states such as differences
        """
        return self._get_observables('one_state')

    @property
    def observables_defined_by_phase(self):
        """
        Observables which are defined by the phase as a whole, and not defined by any 1 or more states
        e.g. Standard State Correction
        """
        return self._get_observables('phase')

    ##########################################
    # Define the observables which carry error
    # This should be a subset of observables
    ##########################################

    @property
    def observables_with_error(self):
        """Determine which observables have error by inspecting the the error subsets"""
        observables = set()
        for subset_key in self._errors:
            if subset_key is not None:
                observables |= self._errors[subset_key]
        return tuple(observables)

    # ------------------------------------------------
    # Exclusive Error categories
    # The intersection of these should be the null set
    # ------------------------------------------------

    @property
    def observables_with_error_adding_quadrature(self):
        """Observable C = A + B, Error eC = sqrt(eA**2 + eB**2)"""
        return self._get_errors('quad')

    @property
    def observables_with_error_adding_linear(self):
        """Observable C = A + B, Error eC = eA + eB"""
        return self._get_errors('linear')

    @property
    def observables_without_error(self):
        return self._get_errors(None)

    # ------------------
    # Internal functions
    # ------------------

    def _get_observables(self, key):
        return tuple(self._observables[key])

    def _get_errors(self, key):
        return tuple(self._errors[key])

    @staticmethod
    def _cast_observable_name(name) -> str:
        return re.sub(" +", "_", name.lower())

    def _register_observable(self, obs_name: str,
                             obs_calc_class: str,
                             obs_error_class: Union[None, str],
                             re_register: bool=False):
        obs_name = self._cast_observable_name(obs_name)
        if not re_register and obs_name in self.observables:
            raise ValueError("{} is already a registered observable! "
                             "Consider setting re_register key!".format(obs_name))
        self._check_obs_class(obs_calc_class)
        self._check_obs_error_class(obs_error_class)
        obs_name_set = {obs_name}  # set(single_object) throws an error, set(string) splits each char
        # Throw out existing observable if present (set difference)
        for obs_key in self._observables:
            self._observables[obs_key] -= obs_name_set
        for obs_err_key in self._errors:
            self._errors[obs_err_key] -= obs_name_set
        # Add new observable to correct classifiers (set union)
        self._observables[obs_calc_class] |= obs_name_set
        self._errors[obs_error_class] |= obs_name_set

    def _check_obs_class(self, obs_class):
        assert obs_class in self._observables, "{} not a known observable class!".format(obs_class)

    def _check_obs_error_class(self, obs_error):
        assert obs_error is None or obs_error in self._errors, \
            "{} not a known observable error class!".format(obs_error)

# Create a default registry and register some stock values
default_observables_registry = ObservablesRegistry()
default_observables_registry.register_two_state_observable('free_energy', error_class='quad')
default_observables_registry.register_two_state_observable('entropy', error_class='quad')
default_observables_registry.register_two_state_observable('enthalpy', error_class='quad')


# ---------------------------------------------------------------------------------------------
# Phase Analyzers
# ---------------------------------------------------------------------------------------------

class PhaseAnalyzer(ABC):
    """
    Analyzer for a single phase of a MultiState simulation.

    Uses the reporter from the simulation to determine the location
    of all variables.

    To compute a specific observable in an implementation of this class, add it to the ObservableRegistry and then
    implement a ``get_X`` where ``X`` is the name of the observable you want to compute. See the ObservablesRegistry for
    information about formatting the observables.

    Analyzer works in units of kT unless specifically stated otherwise. To convert back to a unit set, just multiply by
    the .kT property.

    A PhaseAnalyzer also needs an ObservablesRegistry to track how to handle each observable given implemented within
    for things like error and cross-phase analysis.

    Parameters
    ----------
    reporter : MultiStateReporter instance
        Reporter from MultiState which ties to the simulation data on disk.
    name : str, Optional
        Unique name you want to assign this phase, this is the name that will appear in :class:`MultiPhaseAnalyzer`'s.
        If not set, it will be given the arbitrary name "phase#" where # is an integer, chosen in order that it is
        assigned to the :class:`MultiPhaseAnalyzer`.
    reference_states : tuple of ints, length 2, Optional, Default: (0,-1)
        Integers ``i`` and ``j`` of the state that is used for reference in observables, "O". These values are only used
        when reporting single numbers or combining observables through :class:`MultiPhaseAnalyzer` (since the number of
        states between phases can be different). Calls to functions such as ``get_free_energy`` in a single Phase
        results in the O being returned for all states.

            For O completely defined by the state itself (i.e. no differences between states, e.g. Temperature),
            only O[i] is used

            For O where differences between states are required (e.g. Free Energy): O[i,j] = O[j] - O[i]

            For O defined by the phase as a whole, the reference states are not needed.

    analysis_kwargs : None or dict, optional
        Dictionary of extra keyword arguments to pass into the analysis tool, typically MBAR.
        For instance, the initial guess of relative free energies to give to MBAR would be something like:
        ``{'initial_f_k':[0,1,2,3]}``

    registry : ObservablesRegistry instance
        Instanced ObservablesRegistry with all observables implemented through a ``get_X`` function classified and
        registered. Any cross-phase analysis must use the same instance of an ObservablesRegistry


    Attributes
    ----------
    name
    observables
    mbar
    reference_states
    kT
    reporter
    registry

    See Also
    --------
    ObservablesRegistry

    """
    def __init__(self, reporter, name=None, reference_states=(0, -1),
                 restraint_energy_cutoff=None, restraint_distance_cutoff=None, max_n_iterations=None,
                 analysis_kwargs=None, registry=default_observables_registry):
        """
        The reporter provides the hook into how to read the data, all other options control where differences are
        measured from and how each phase interfaces with other phases.
        """
        # Check arguments.
        if type(reporter) is str:
            raise ValueError('reporter must be a MultiStateReporter instance')
        if not isinstance(registry, ObservablesRegistry):
            raise ValueError("Registry must be an instanced ObservablesRegistry")
        if analysis_kwargs is None:
            analysis_kwargs = {}
        elif not isinstance(analysis_kwargs, dict):
            raise ValueError('analysis_kwargs must be either None or a dictionary')

        self.registry = registry
        if not reporter.is_open():
            reporter.open(mode='r')
        self._reporter = reporter

        # Initialize cacheed observables so the phase can be retrieved once computed.
        self._computed_observables = {observable: None for observable in self.observables}

        # Internal properties
        self._name = name
        # Start as default sign +, handle all sign conversion at preparation time
        self._sign = '+'
        self._reference_states = None  # Initialize the cache object.
        self.reference_states = reference_states
        self._extra_analysis_kwargs = analysis_kwargs

        # Initialize cached values that are read directly from the Reporter.
        self._n_iterations = None
        self._n_replicas = None
        self._end_thermodynamic_states = None
        self._kT = None

        # Cached values with dependencies.
        self._cache = {}  # This cache should be always set with _update_cache().
        self.max_n_iterations = max_n_iterations

    @property
    def name(self):
        """User-readable string name of the phase"""
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def observables(self):
        """List of observables that the instanced analyzer can compute/fetch."""
        # Auto-determine the computable observables by inspection of non-flagged methods
        # We determine valid observables by negation instead of just having each child
        # implement the method to enforce uniform function naming conventions.
        observables = []
        for observable in self.registry.observables:
            if hasattr(self, "get_" + observable):
                observables.append(observable)
        # Cast observables to an immutable.
        return tuple(observables)

    @property
    def reference_states(self):
        """Tuple of reference states ``i`` and ``j`` for :class:`MultiPhaseAnalyzer` instances"""
        return self._reference_states

    @reference_states.setter
    def reference_states(self, value):
        """Provide a way to re-assign the ``i, j`` states in a protected way"""
        i, j = value[0], value[1]
        if type(i) is not int or type(j) is not int:
            raise ValueError("reference_states must be a length 2 iterable of ints")
        self._reference_states = (i, j)

    @property
    def n_iterations(self):
        """int: The total number of iterations of the phase."""
        if self._n_iterations is None:
            # The + 1 accounts for iteration 0.
            self._n_iterations = self._reporter.read_last_iteration(full_iteration=False)
        return self._n_iterations

    @property
    def n_replicas(self):
        """int: Number of replicas."""
        if self._n_replicas is None:
            replica_state_indices = self._reporter.read_replica_thermodynamic_states(iteration=0)
            self._n_replicas = len(replica_state_indices)
        return self._n_replicas

    @property
    def n_states(self):
        """int: Number of sampled thermodynamic states."""
        return self._reporter.n_states

    def _get_end_thermodynamic_states(self):
        """Read thermodynamic states at the ends of the protocol."""
        # TODO: what if reference_states changes? This should become a CachedProperty.
        # Check cached values.
        if self._end_thermodynamic_states is None:
            sampled_states, unsampled_states = self._reporter.read_thermodynamic_states()
            if len(unsampled_states) == 0:
                self._end_thermodynamic_states = [sampled_states[0], sampled_states[-1]]
            else:
                self._end_thermodynamic_states = unsampled_states
            # Cache other useful informations since we have already read this.
            # TODO should we read temperatures of all the states and let kT property depend on reference_states?
            self._kT = self._end_thermodynamic_states[0].kT
        return self._end_thermodynamic_states

    @property
    def kT(self):
        """
        Quantity of boltzmann constant times temperature of the phase in units of energy per mol

        Allows conversion between dimensionless energy and unit bearing energy
        """
        if self._kT is None:
            self._get_end_thermodynamic_states()
        return self._kT

    @property
    def reporter(self):
        """Sampler Reporter tied to this object."""
        return self._reporter

    @reporter.setter
    def reporter(self, value):
        """Make sure users cannot overwrite the reporter."""
        raise ValueError("You cannot re-assign the reporter for this analyzer!")

    # -------------------------------------------------------------------------
    # Cached properties functions/classes.
    # -------------------------------------------------------------------------

    class CachedProperty(object):
        """Descriptor of a cached value with a dependency graph.

        Automatically takes care of invalidating the values of the cache
        that depend on this property.

        Parameters
        ----------
        name : str
            The name of the parameter in the cache.
        default : object, optional
            The default value in case the cache doesn't contain a value
            for this. If a callable, this function must have the signature
            ``default(instance)``. After the first cache miss, the default
            value is cached. By default, AttributeError is raised on a
            cache miss.
        validator : callable, optional
            A function to call before setting a new value with signature
            ``validator(instance, new_value)``.
        check_changes : bool, optional
            If True, the cache dependencies will be invalidated only if
            the new value differs from the old one (default is False).

        """
        def __init__(self, name, dependencies=(), default=AttributeError,
                     validator=None, check_changes=False):
            # TODO make observables CachedProperties?
            assert name != 'observables'  # Reserved name.
            # TODO use __setname__() when dropping Python 3.5 support.
            self.name = name
            self.dependencies = dependencies
            self._default = default
            self._validator = validator
            self._check_changes = check_changes

        def __get__(self, instance, owner_class=None):
            # If called as a class descriptor, return the descriptor.
            if instance is None:
                return self
            # Check if the value is cached and fall back to default value.
            try:
                value = instance._cache[self.name]
            except KeyError:
                value = self._get_default(instance)
                # Cache default value for next use.
                instance._update_cache(self.name, value, self._check_changes)
            return value

        def __set__(self, instance, new_value):
            if self._validator is not None:
                new_value = self._validator(instance, new_value)
            instance._update_cache(self.name, new_value, self._check_changes)

        def _get_default(self, instance):
            if self._default is AttributeError:
                err_msg = 'Reference before assignment {}.{}'.format(instance, self.name)
                raise AttributeError(err_msg)
            elif callable(self._default):
                value = self._default(instance)
            else:
                value = self._default
            return value

    @classmethod
    def _get_cache_dependency_graph(cls):
        """dict: cached_value -> list of cache values to invalidate."""
        # Retrieve all cached properties.
        cached_properties = {value for name, value in inspect.getmembers(cls)
                             if isinstance(value, cls.CachedProperty)}
        # Build the dependency graph.
        dependency_graph = {}
        for cached_property in cached_properties:
            for dependency in cached_property.dependencies:
                try:
                    dependency_graph[dependency].add(cached_property.name)
                except KeyError:
                    dependency_graph[dependency] = {cached_property.name}
        # Hard-code observable dependency since those are not CachedProperties.
        # TODO make observables CachedProperties?
        dependency_graph['mbar'] = {'observables'}
        return dependency_graph

    def _update_cache(self, key, new_value, check_changes=False):
        """Update the cache entry and invalidate values that depend on it.

        Parameters
        ----------
        key : str
            The name of the value to update.
        value : object
            The new value of the key.
        check_changes : bool, optional
            If True and the new value is equal to the current one,
            the dependent cache values are not invalidated.

        """
        invalidate_cache = True
        try:
            old_value = self._cache[key]
        except KeyError:
            invalidate_cache = False
        else:
            if check_changes and old_value == new_value:
                invalidate_cache = False
        # Update value and invalidate the cache.
        self._cache[key] = new_value
        if invalidate_cache:
            self._invalidate_cache_values(key)

    def _invalidate_cache_values(self, key):
        """Invalidate all the cache dependencies of key.

        Parameters
        ----------
        key : str
            The name of the cached whose dependencies must be invalidated.

        """
        dependency_graph = self._get_cache_dependency_graph()
        for k in dependency_graph[key]:
            # Invalidate observables that are in a separate cache.
            if k == 'observables':
                for observable in self.observables:
                    self._computed_observables[observable] = None
            else:
                # Invalidate dependencies of k.
                self._invalidate_cache_values(k)
                # Remove k.
                self._cache.pop(k, None)

    # -------------------------------------------------------------------------
    # Cached properties.
    # -------------------------------------------------------------------------

    @staticmethod
    def _max_n_iterations_validator(instance, new_value):
        if new_value is None or new_value > instance.n_iterations:
            new_value = instance.n_iterations
        return new_value

    max_n_iterations = CachedProperty(
        name='max_n_iterations',
        validator=_max_n_iterations_validator.__func__,
        check_changes=True
    )

    # -------------------------------------------------------------------------
    # Abstract methods.
    # -------------------------------------------------------------------------

    @abc.abstractmethod
    def get_states_energies(self):
        """
        Extract the deconvoluted energies from a phase.

        Energies from this are NOT decorrelated.

        Returns
        -------
        sampled_energy_matrix : numpy.ndarray of shape K,L,N'
            Deconvoluted energy of sampled states evaluated at other sampled states.

            Has shape (K,L,N') = (number of replica samplers,
                                 number of sampled thermodynamic states,
                                 number of iterations from state k)

            Indexed by [k,l,n] where an energy drawn from replica sampler [k] is evaluated in thermodynamic state [l] at
            iteration [n]
        unsampled_energy_matrix : numpy.ndarray of shape K,L,N
            Has shape (K, L, N) = (number of replica samplers,
                                   number of UN-sampled thermodynamic states,
                                   number of iterations)

            Indexed by [k,l,n]
            where an energy drawn from replica state [k] is evaluated in un-sampled state [l] at iteration [n]
        """
        raise NotImplementedError()

    @staticmethod
    @abc.abstractmethod
    def get_timeseries(passed_timeseries):
        """
        Generate the timeseries that is generated for this phase

        Returns
        -------
        generated_timeseries : 1-D iterable
            timeseries which can be fed into get_decorrelation_time to get the decorrelation
        """

        raise NotImplementedError("This class has not implemented this function")

    # -------------------------------------------------------------------------
    # MBAR routines.
    # -------------------------------------------------------------------------

    @staticmethod
    def reformat_energies_for_mbar(u_kln: np.ndarray, n_k: Optional[np.ndarray]=None):
        """
        Convert u_kln formatted energies into u_ln formatted energies.

        This method assumes that the first dimension are all samplers,
        the second dimension are all the thermodynamic states energies were evaluated at
        and an equal number of samples were drawn from each k'th sampler, UNLESS n_k is specified.

        Parameters
        ----------
        u_kln : np.ndarray of shape (K,L,N')
            K = number of replica samplers
            L = number of thermodynamic states,
            N' = number of iterations from state k
        n_k : np.ndarray of shape K or None
            Number of samples each _SAMPLER_ (k) has drawn
            This allows you to have trailing entries on a given kth row in the n'th (n prime) index
            which do not contribute to the conversion.

            If this is None, assumes ALL samplers have the same number of samples
            such that N_k = N' for all k

            **WARNING**: N_k is number of samples the SAMPLER drew,
            NOT how many samples were drawn from each thermodynamic state L.
            This method knows nothing of how many samples were drawn from each state.

        Returns
        -------
        u_ln : np.ndarray of shape (L, N)
            Reduced, non-sparse data format
            L = number of thermodynamic states
            N = \sum_k N_k. note this is not N'
        """
        k, l, n = u_kln.shape
        if n_k is None:
            n_k = np.ones(k, dtype=np.int32)*n
        u_ln = np.zeros([l, n_k.sum()])
        n_counter = 0
        for k_index in range(k):
            u_ln[:, n_counter:n_counter + n_k[k_index]] = u_kln[k_index, :, :n_k[k_index]]
            n_counter += n_k[k_index]
        return u_ln

    # Private Class Methods
    def _create_mbar(self, energy_matrix, samples_per_state):
        """
        Initialize MBAR for Free Energy and Enthalpy estimates, this may take a while.
        This function is helpful for those who want to create a slightly different mbar object with different
        parameters.

        This function is hidden from the user unless they really, really need to create their own mbar object

        Parameters
        ----------
        energy_matrix : array of numpy.float64, optional, default=None
           Reduced potential energies of the replicas; if None, will be extracted from the ncfile
        samples_per_state : array of ints, optional, default=None
           Number of samples drawn from each kth state; if None, will be extracted from the ncfile

        """
        # Initialize MBAR (computing free energy estimates, which may take a while)
        logger.info("Computing free energy differences...")
        self.mbar = MBAR(energy_matrix, samples_per_state, **self._extra_analysis_kwargs)
        return self.mbar

    # -------------------------------------------------------------------------
    # Analysis combination.
    # -------------------------------------------------------------------------

    def _combine_phases(self, other, operator='+'):
        """
        Workhorse function when creating a :class:`MultiPhaseAnalyzer` object by combining single
        :class:`PhaseAnalyzer`s
        """
        phases = [self]
        names = []
        signs = [self._sign]
        # Reset self._sign
        self._sign = '+'
        if self.name is None:
            names.append(utils.generate_phase_name(self.name, []))
        else:
            names.append(self.name)
        if isinstance(other, MultiPhaseAnalyzer):
            new_phases = other.phases
            new_signs = other.signs
            new_names = other.names
            final_new_names = []
            for name in new_names:
                other_names = [n for n in new_names if n != name]
                final_new_names.append(utils.generate_phase_name(name, other_names + names))
            names.extend(final_new_names)
            for new_sign in new_signs:
                if operator != '+' and new_sign == '+':
                    signs.append('-')
                else:
                    signs.append('+')
            phases.extend(new_phases)
        elif isinstance(other, PhaseAnalyzer):
            names.append(utils.generate_phase_name(other.name, names))
            if operator != '+' and other._sign == '+':
                signs.append('-')
            else:
                signs.append('+')
            # Reset the other's sign if it got set to negative
            other._sign = '+'
            phases.append(other)
        else:
            base_err = "cannot {} 'PhaseAnalyzer' and '{}' objects"
            if operator == '+':
                err = base_err.format('add', type(other))
            else:
                err = base_err.format('subtract', type(other))
            raise TypeError(err)
        phase_pass = {'phases': phases, 'signs': signs, 'names': names}
        return MultiPhaseAnalyzer(phase_pass)

    def __add__(self, other):
        return self._combine_phases(other, operator='+')

    def __sub__(self, other):
        return self._combine_phases(other, operator='-')

    def __neg__(self):
        """Internally handle the internal sign"""
        if self._sign == '+':
            self._sign = '-'
        else:
            self._sign = '+'
        return self


class MultiStateSamplerAnalyzer(PhaseAnalyzer):

    """
    The MultiStateSamplerAnalyzer is the analyzer for a simulation generated from a MultiStateSampler simulation,
    implemented as an instance of the :class:`PhaseAnalyzer`.

    See Also
    --------
    PhaseAnalyzer

    """

    def __init__(self, *args, unbias_restraint=True, restraint_energy_cutoff=None,
                 restraint_distance_cutoff=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Initialize cached values that are derived directly from the Reporter.
        self._restraint_data = None
        self._restraint_energies_kn = {}
        self._restraint_distances_kn = {}

        # Cached values with dependencies.
        self.unbias_restraint = unbias_restraint
        self.restraint_energy_cutoff = restraint_energy_cutoff
        self.restraint_distance_cutoff = restraint_distance_cutoff

    # TODO use class syntax and add docstring after dropping python 3.5 support.
    _MixingStatistics = NamedTuple('MixingStatistics', [
        ('transition_matrix', np.ndarray),
        ('eigenvalues', np.ndarray),
        ('statistical_inefficiency', np.ndarray)
    ])

    def generate_mixing_statistics(self, number_equilibrated: Union[int, None] = None) -> NamedTuple:
        """
        Compute and return replica mixing statistics.

        Compute the transition state matrix, its eigenvalues sorted from
        greatest to least, and the state index correlation function.

        Parameters
        ----------
        number_equilibrated : int, optional, default=None
            If specified, only samples ``number_equilibrated:end`` will
            be used in analysis. If not specified, automatically retrieves
            the number from equilibration data or generates it from the
            internal energy.

        Returns
        -------
        mixing_statistics : namedtuple
            A namedtuple containing the following attributes:
            - ``transition_matrix``: (nstates by nstates ``np.array``)
            - ``eigenvalues``: (nstates-dimensional ``np.array``)
            - ``statistical_inefficiency``: float
        """
        # Read data from disk
        if number_equilibrated is None:
            number_equilibrated = self.n_equilibration_iterations
        states = self._reporter.read_replica_thermodynamic_states()
        n_iterations, n_replicas = states.shape
        n_states = self._reporter.n_states
        n_ij = np.zeros([n_states, n_states], np.int64)

        # Compute empirical transition count matrix.
        for iteration in range(number_equilibrated, n_iterations - 1):
            for i_replica in range(n_replicas):
                i_state = states[iteration, i_replica]
                j_state = states[iteration + 1, i_replica]
                n_ij[i_state, j_state] += 1

        # Compute transition matrix estimate.
        # TODO: Replace with maximum likelihood reversible count estimator from msmbuilder or pyemma.
        t_ij = np.zeros([n_states, n_states], np.float64)
        for i_state in range(n_states):
            # Cast to float to ensure we don't get integer division
            denominator = float((n_ij[i_state, :].sum() + n_ij[:, i_state].sum()))
            if denominator > 0:
                for j_state in range(n_states):
                    t_ij[i_state, j_state] = (n_ij[i_state, j_state] + n_ij[j_state, i_state]) / denominator
            else:
                t_ij[i_state, i_state] = 1.0

        # Estimate eigenvalues
        mu = np.linalg.eigvals(t_ij)
        mu = -np.sort(-mu)  # Sort in descending order

        # Compute state index statistical inefficiency of stationary data.
        # states[n][k] is the state index of replica k at iteration n, but
        # the functions wants a list of timeseries states[k][n].
        states_kn = np.transpose(states[number_equilibrated:])
        g = timeseries.statisticalInefficiencyMultiple(states_kn)

        return self._MixingStatistics(transition_matrix=t_ij, eigenvalues=mu,
                                      statistical_inefficiency=g)

    def show_mixing_statistics(self, cutoff=0.05, number_equilibrated=None):
        """
        Print summary of mixing statistics. Passes information off to generate_mixing_statistics then prints it out to
        the logger

        Parameters
        ----------
        cutoff : float, optional, default=0.05
           Only transition probabilities above 'cutoff' will be printed
        number_equilibrated : int, optional, default=None
           If specified, only samples number_equilibrated:end will be used in analysis
           If not specified, it uses the internally held statistics best

        """

        mixing_statistics = self.generate_mixing_statistics(number_equilibrated=number_equilibrated)

        # Print observed transition probabilities.
        nstates = mixing_statistics.transition_matrix.shape[1]
        logger.info("Cumulative symmetrized state mixing transition matrix:")
        str_row = "{:6s}".format("")
        for jstate in range(nstates):
            str_row += "{:6d}".format(jstate)
        logger.info(str_row)

        for istate in range(nstates):
            str_row = ""
            str_row += "{:-6d}".format(istate)
            for jstate in range(nstates):
                P = mixing_statistics.transition_matrix[istate, jstate]
                if P >= cutoff:
                    str_row += "{:6.3f}".format(P)
                else:
                    str_row += "{:6s}".format("")
            logger.info(str_row)

        # Estimate second eigenvalue and equilibration time.
        perron_eigenvalue = mixing_statistics.eigenvalues[1]
        if perron_eigenvalue >= 1:
            logger.info('Perron eigenvalue is unity; Markov chain is decomposable.')
        else:
            equilibration_timescale = 1.0 / (1.0 - perron_eigenvalue)
            logger.info('Perron eigenvalue is {0:.5f}; state equilibration timescale '
                        'is ~ {1:.1f} iterations'.format(perron_eigenvalue, equilibration_timescale)
            )

        # Print information about replica state index statistical efficiency.
        logger.info('Replica state index statistical inefficiency is '
                    '{:.3f}'.format(mixing_statistics.statistical_inefficiency))

    def _get_restraint_data(self):
        """Return the two unsampled states and a reduced version of them containing only the restraint force."""
        # Check cached value.
        if self._restraint_data is not None:
            return self._restraint_data

        # Isolate the end states.
        end_states = self._get_end_thermodynamic_states()

        # Isolate restraint force.
        system = end_states[0].system
        restraint_parent_class = mmtools.forces.RadiallySymmetricRestraintForce
        # This raises mmtools.forces.NoForceFoundError if there's no restraint to unbias.
        force_idx, restraint_force = mmtools.forces.find_forces(system, force_type=restraint_parent_class,
                                                                only_one=True, include_subclasses=True)
        # The force is owned by the System, we have to copy to avoid the memory to be deallocated.
        restraint_force = copy.deepcopy(restraint_force)

        # Check that the restraint was turned on at the end states.
        if end_states[0].lambda_restraints != 1.0 or end_states[-1].lambda_restraints != 1.0:
            raise TypeError('Cannot unbias a restraint that is turned off at one of the end states.')

        # Read the centroid weights (mass) of the restrained particles.
        weights_group1 = [system.getParticleMass(i) for i in restraint_force.restrained_atom_indices1]
        weights_group2 = [system.getParticleMass(i) for i in restraint_force.restrained_atom_indices2]

        # Cache value so that we won't have to deserialize the system again.
        self._restraint_data = restraint_force, weights_group1, weights_group2
        return self._restraint_data

    # -------------------------------------------------------------------------
    # MBAR creation.
    # -------------------------------------------------------------------------

    def get_states_energies(self):
        """
        Extract and deconvolute energies from the ncfile to gather energies common data for other functions.

        Returns
        -------
        energy_matrix : ndarray of shape [n_replicas, n_states, n_iterations]
            Potential energy matrix of the sampled states
            Energy is from each drawn sample n, evaluated at every sampled state k
        unsampled_energy_matrix : ndarray of shape [n_replicas, n_unsamped_states, n_iterations]
            Potential energy matrix of the unsampled states
            Energy from each drawn sample n, evaluated at unsampled state l
            If no unsampled states were drawn, this will be shape (0,N)

        """
        return self._get_states_energies(truncate_max_n_iterations=False)

    def _get_states_energies(self, truncate_max_n_iterations=False):
        """Extract and deconvolute energies from nc file. Optionally truncate to self.max_n_iterations."""
        logger.info("Reading energies...")
        energy_thermodynamic_states, neighborhoods, energy_unsampled_states = self._reporter.read_energies()
        n_iterations, n_replicas, n_states = energy_thermodynamic_states.shape
        _, _, n_unsampled_states = energy_unsampled_states.shape

        # TODO: Figure out what format we need the data in to be useful for both global and local MBAR/WHAM
        # For now, we simply can't handle analysis of non-global calculations.
        if np.any(neighborhoods == 0):
            raise Exception('Non-global MBAR analysis not implemented yet.')

        # Truncate the number of iterations to self.max_n_iterations if requested.
        if truncate_max_n_iterations:
            # The +1 accounts for minimization iteration.
            n_iterations = self.max_n_iterations + 1

        # Convert shape from iterations x replicas x states to replicas x states x iterations
        energy_matrix_replica = np.zeros([n_replicas, n_states, n_iterations], np.float64)
        unsampled_energy_matrix_replica = np.zeros([n_replicas, n_unsampled_states, n_iterations], np.float64)
        for n in range(n_iterations):
            energy_matrix_replica[:, :, n] = energy_thermodynamic_states[n, :, :]
            unsampled_energy_matrix_replica[:, :, n] = energy_unsampled_states[n, :, :]
        logger.info("Done.")

        logger.info("Deconvoluting replicas...")
        energy_matrix = np.zeros([n_states, n_states, n_iterations], np.float64)
        unsampled_energy_matrix = np.zeros([n_states, n_unsampled_states, n_iterations], np.float64)
        for iteration in range(n_iterations):
            state_indices = self._reporter.read_replica_thermodynamic_states(iteration)
            energy_matrix[state_indices, :, iteration] = energy_matrix_replica[:, :, iteration]
            unsampled_energy_matrix[state_indices, :, iteration] = unsampled_energy_matrix_replica[:, :, iteration]
        logger.info("Done.")

        return energy_matrix, unsampled_energy_matrix

    @staticmethod
    def get_timeseries(passed_timeseries):
        """
        Compute the timeseries of a simulation from the Replica Exchange simulation. This is the sum of energies
        for each sample from the state it was drawn from.

        Parameters
        ----------
        passed_timeseries : ndarray of shape (K,L,N), indexed by k,l,n
            K is the total number of sampled states

            L is the total states we want MBAR to analyze

            N is the total number of samples

            The kth sample was drawn from state k at iteration n, the nth configuration of kth state is evaluated in
            thermodynamic state l

        Returns
        -------
        u_n : ndarray of shape (N,)
            Timeseries to compute decorrelation and equilibration data from.
        """
        niterations = passed_timeseries.shape[-1]
        u_n = np.zeros([niterations], np.float64)
        # Compute total negative log probability over all iterations.
        for iteration in range(niterations):
            u_n[iteration] = np.sum(np.diagonal(passed_timeseries[:, :, iteration]))
        return u_n

    def _compute_equilibrated_energies(self):
        """Return the energies after discarding equilibration and iterations > max_n_iterations."""
        u_kln, unsampled_u_kln = self._get_states_energies(truncate_max_n_iterations=True)

        # Discard equilibration iterations.
        number_equilibrated, g_t, Neff_max = self._get_equilibration_data_auto(u_kln)
        u_kln = utils.remove_unequilibrated_data(u_kln, number_equilibrated, -1)
        unsampled_u_kln = utils.remove_unequilibrated_data(unsampled_u_kln, number_equilibrated, -1)

        # Subsample the energies only based on g_t so both ends up with same indices.
        u_kln = utils.subsample_data_along_axis(u_kln, g_t, -1)
        unsampled_u_kln = utils.subsample_data_along_axis(unsampled_u_kln, g_t, -1)
        return u_kln, unsampled_u_kln

    def _compute_decorrelated_energies(self):
        """Return an MBAR-ready decorrelated energy matrix."""
        sampled_energy_matrix, unsampled_energy_matrix = self._compute_equilibrated_energies()
        nstates, _, niterations = sampled_energy_matrix.shape
        _, nunsampled, _ = unsampled_energy_matrix.shape
        # Subsample data to obtain uncorrelated samples
        N_k = np.zeros(nstates, np.int32)
        N = niterations  # number of uncorrelated samples
        N_k[:] = N
        u_kn = self.reformat_energies_for_mbar(sampled_energy_matrix)

        # Add unsampled states.
        if nunsampled > 0:
            new_energy_matrix = np.zeros([nstates + 2, N_k.sum()])
            N_k_new = np.zeros(nstates + 2, np.int32)
            unsampled_kn = self.reformat_energies_for_mbar(unsampled_energy_matrix)
            # Add augmented unsampled energies to the new matrix
            new_energy_matrix[[0, -1], :] = unsampled_kn[[0, -1], :]
            # Fill in the old energies to the middle states
            new_energy_matrix[1:-1, :] = u_kn
            N_k_new[1:-1] = N_k
            # Notify users
            logger.info("Found expanded cutoff states in the energies!")
            logger.info("Free energies will be reported relative to them instead!")
            # Reset values, last step in case something went wrong so we dont overwrite u_kn on accident
            u_kn = new_energy_matrix
            N_k = N_k_new

        # These cached values speed up considerably the computation of the
        # free energy profile along the restraint distance/energy cutoff.
        self._decorrelated_u_kn = u_kn
        self._decorrelated_N_k = N_k
        return self._decorrelated_u_kn, self._decorrelated_N_k

    def _compute_unbiased_energies(self):
        """Unbias the restraint, and apply restraint energy/distance cutoffs."""
        # Check if we need to unbias the restraint.
        unbias_restraint = self.unbias_restraint
        if unbias_restraint:
            try:
                restraint_data = self._get_restraint_data()
            except (TypeError, mmtools.forces.NoForceFoundError) as e:
                # If we don't need to unbias the restraint there's nothing else to do.
                logger.info(str(e) + ' The restraint will not be unbiased.')
                unbias_restraint = False
        if not unbias_restraint:
            self._unbiased_decorrelated_u_kn = self._decorrelated_u_kn
            self._unbiased_decorrelated_N_k = self._decorrelated_N_k
            return self._unbiased_decorrelated_u_kn, self._unbiased_decorrelated_N_k

        is_cutoff_distance = self.restraint_distance_cutoff is not None
        is_cutoff_energy = self.restraint_energy_cutoff is not None

        # Compute the restraint energies/distances.
        restraint_force, weights_group1, weights_group2 = restraint_data

        logger.debug('Found {} restraint. The restraint will be unbiased.'.format(restraint_force.__class__.__name__))
        logger.debug('Receptor restrained atoms: {}'.format(restraint_force.restrained_atom_indices1))
        logger.debug('ligand restrained atoms: {}'.format(restraint_force.restrained_atom_indices2))

        # Compute restraint energies/distances.
        energies_kn, distances_kn = self._compute_restraint_energies(
            restraint_force, weights_group1, weights_group2, compute_distances=is_cutoff_distance)

        # Convert energies to kT unit for comparison to energy cutoff.
        energies_kn = energies_kn / self.kT
        logger.debug('Restraint energy mean: {} kT; std: {} kT'
                     ''.format(np.mean(energies_kn), np.std(energies_kn, ddof=1)))

        # Don't modify the cached decorrelated energies.
        u_kn = copy.deepcopy(self._decorrelated_u_kn)
        N_k = copy.deepcopy(self._decorrelated_N_k)
        n_decorrelated_iterations_kn = u_kn.shape[1]
        assert len(energies_kn) == n_decorrelated_iterations_kn
        assert len(self._decorrelated_state_indices_kn) == n_decorrelated_iterations_kn

        # We need to take into account the initial unsampled states to index correctly N_k.
        state_idx_shift = 0
        while N_k[state_idx_shift] == 0:
            state_idx_shift +=1

        # Determine which samples are outside the cutoffs or have to be truncated.
        columns_to_keep = []
        for iteration_kn_idx, state_idx in enumerate(self._decorrelated_state_indices_kn):
            if ((is_cutoff_energy and energies_kn[iteration_kn_idx] > self.restraint_energy_cutoff) or
                    (is_cutoff_distance and distances_kn[iteration_kn_idx] > self.restraint_distance_cutoff)):
                # Update the number of samples generated from its state.
                N_k[state_idx + state_idx_shift] -= 1
            else:
                columns_to_keep.append(iteration_kn_idx)

        # Drop all columns that exceed the cutoff(s).
        n_discarded = n_decorrelated_iterations_kn - len(columns_to_keep)
        logger.debug('Discarding {}/{} samples outside the cutoffs (restraint_distance_cutoff: {}, '
                     'restraint_energy_cutoff: {}).'.format(n_discarded, n_decorrelated_iterations_kn,
                                                            self.restraint_distance_cutoff,
                                                            self.restraint_energy_cutoff))
        u_kn = u_kn[:, columns_to_keep]

        # Add new end states that don't include the restraint.
        energies_kn = energies_kn[columns_to_keep]
        n_states, n_iterations = u_kn.shape
        n_states_new = n_states + 2
        N_k_new = np.zeros(n_states_new, N_k.dtype)
        u_kn_new = np.zeros((n_states_new, n_iterations), u_kn.dtype)
        u_kn_new[0, :] = u_kn[0] - energies_kn
        u_kn_new[-1, :] = u_kn[-1] - energies_kn
        # Copy old values.
        N_k_new[1:-1] = N_k
        u_kn_new[1:-1, :] = u_kn

        # Cache new values.
        self._unbiased_decorrelated_u_kn = u_kn_new
        self._unbiased_decorrelated_N_k = N_k_new
        return self._unbiased_decorrelated_u_kn, self._unbiased_decorrelated_N_k

    def _compute_restraint_energies(self, restraint_force, weights_group1, weights_group2,
                                    compute_distances=False):
        """Compute the restrain distances for the given iterations.

        Parameters
        ----------
        restraint_force : openmmtools.forces.RadiallySymmetricRestraintForce
            The restraint force.
        weights_group1 : list of float
            The mass of the particle in the first CustomCentroidBondForce group.
        weights_group2 : list of float
            The mass of the particles in the second CustomCentroidBondForce group.
        compute_distances : bool, optional
            If True, the distances will be computed as well. This step is
            unnecessary if only a cutoff on the energy was specified.

        Returns
        -------
        restraint_energies_kn : simtk.unit.Quantity
            The restrain energies at the decorrelated iterations for each frame
            (units of energy/mole).
        restraint_distances_kn : simtk.unit.Quantity or None
            If ``compute_distances`` is False, this is None. Otherwise, this array
            has the restrain distances at the decorrelated iterations for each frame.

        """
        ENERGY_UNIT = units.kilojoules_per_mole
        MDTRAJ_DISTANCE_UNIT = units.nanometers
        decorrelated_iterations = self._decorrelated_iterations  # Shortcut.
        decorrelated_iterations_set = set(decorrelated_iterations)

        # Determine total number of energies/distances to compute.
        # The +1 is for the minimization iteration.
        n_frames_kn = self.n_replicas * len(decorrelated_iterations)

        # Computing the restraint energies/distances is expensive and we
        # don't want to recompute everything when _decorrelated_iterations
        # changes (e.g. when max_n_iterations changes) so we keep the cached
        # values of the iterations we have computed.
        # The dictionary instead of a masked array is for memory efficiency
        # since the matrix will be very sparse (especially with SAMS).

        def extract_decorrelated(cached_dict, dtype, unit):
            if not decorrelated_iterations_set.issubset(set(cached_dict)):
                return None
            decorrelated = np.zeros(n_frames_kn, dtype=dtype)
            for state_idx in range(self.n_states):
                for iteration_idx, iteration in enumerate(decorrelated_iterations):
                    frame_idx = state_idx*len(decorrelated_iterations) + iteration_idx
                    decorrelated[frame_idx] = cached_dict[iteration][state_idx]
            return decorrelated * unit

        # Check cached values.
        if compute_distances and decorrelated_iterations_set.issubset(set(self._restraint_distances_kn)):
            compute_distances = False
        if decorrelated_iterations_set.issubset(set(self._restraint_energies_kn)) and not compute_distances:
            return (extract_decorrelated(self._restraint_energies_kn, dtype=np.float64, unit=ENERGY_UNIT),
                    extract_decorrelated(self._restraint_distances_kn, dtype=np.float32, unit=MDTRAJ_DISTANCE_UNIT))

        # Don't modify the original restraint force.
        restraint_force = copy.deepcopy(restraint_force)

        # Store the original indices of the restrained atoms.
        original_restrained_atom_indices = (restraint_force.restrained_atom_indices1 +
                                            restraint_force.restrained_atom_indices2)

        # Create new system with only solute and restraint forces.
        reduced_system = openmm.System()
        for weight in weights_group1 + weights_group2:
            reduced_system.addParticle(weight)
        # Adapt the restraint force atom indices to the reduced system.
        n_atoms1 = len(weights_group1)
        n_atoms = n_atoms1 + len(weights_group2)
        restraint_force.restrained_atom_indices1 = list(range(n_atoms1))
        restraint_force.restrained_atom_indices2 = list(range(n_atoms1, n_atoms))
        reduced_system.addForce(restraint_force)

        if compute_distances:
            # Create topology with only the restrained atoms.
            serialized_topography = self._reporter.read_dict('metadata/topography')
            topology = mmtools.utils.deserialize(serialized_topography).topology
            topology = topology.subset(original_restrained_atom_indices)
            # Initialize trajectory object needed for imaging molecules.
            trajectory = mdtraj.Trajectory(xyz=np.zeros((n_atoms, 3)), topology=topology)

        # Create context used to compute the energies.
        integrator = openmm.VerletIntegrator(1.0*units.femtosecond)
        context = openmm.Context(reduced_system, integrator)

        # TODO: we need to provide a reporter generator to iterate over single
        # TODO:     iterations but reading automatically one chunksize at a time.
        # chunk_size = self._reporter.checkpoint_interval
        # iterations_groups = itertools.groupby(enumerate(decorrelated_iterations), key=lambda x: int(x[1] / chunk_size))

        # Pre-computing energies/distances.
        logger.debug('Computing restraint energies...')
        replica_state_indices = self._reporter.read_replica_thermodynamic_states()
        for iteration_idx, iteration in enumerate(decorrelated_iterations):
            # Check if we have already computed this energy/distance.
            if (iteration in self._restraint_energies_kn and
                    (not compute_distances or iteration in self._restraint_distances_kn)):
                continue
            self._restraint_energies_kn[iteration] = {}
            if compute_distances:
                self._restraint_distances_kn[iteration] = {}

            # Read sampler states only if we haven't computed this iteration yet.
            # Obtain solute only sampler states.
            sampler_states = self._reporter.read_sampler_states(iteration=iteration,
                                                                analysis_particles_only=True)

            for replica_idx, sampler_state in enumerate(sampler_states):
                state_idx = replica_state_indices[iteration, replica_idx]
                sliced_sampler_state = sampler_state[original_restrained_atom_indices]
                sliced_sampler_state.apply_to_context(context)
                potential_energy = context.getState(getEnergy=True).getPotentialEnergy()
                self._restraint_energies_kn[iteration][state_idx] = potential_energy / ENERGY_UNIT

                if compute_distances:
                    # Check if an analytical solution is available.
                    try:
                        distance = restraint_force.distance_at_energy(potential_energy) / MDTRAJ_DISTANCE_UNIT
                    except (NotImplementedError, ValueError):
                        # Update trajectory positions/box vectors.
                        trajectory.xyz = (sliced_sampler_state / MDTRAJ_DISTANCE_UNIT).astype(np.float32)
                        trajectory.unitcell_vectors = np.array([sampler_state.box_vectors / MDTRAJ_DISTANCE_UNIT],
                                                               dtype=np.float32)
                        trajectory.image_molecules(inplace=True, make_whole=False)
                        positions_group1 = trajectory.xyz[0][restraint_force.restrained_atom_indices1]
                        positions_group2 = trajectory.xyz[0][restraint_force.restrained_atom_indices2]

                        # Set output arrays.
                        distance = compute_centroid_distance(positions_group1, positions_group2,
                                                             weights_group1, weights_group2)
                    self._restraint_distances_kn[iteration][state_idx] = distance

        return (extract_decorrelated(self._restraint_energies_kn, dtype=np.float64, unit=ENERGY_UNIT),
                extract_decorrelated(self._restraint_distances_kn, dtype=np.float32, unit=MDTRAJ_DISTANCE_UNIT))

    # -------------------------------------------------------------------------
    # Observables.
    # -------------------------------------------------------------------------

    def _compute_free_energy(self):
        """
        Estimate free energies of all alchemical states.
        """
        nstates = self.mbar.N_k.size

        # Get matrix of dimensionless free energy differences and uncertainty estimate.
        logger.info("Computing covariance matrix...")

        try:
            # pymbar 2
            (Deltaf_ij, dDeltaf_ij) = self.mbar.getFreeEnergyDifferences()
        except ValueError:
            # pymbar 3
            (Deltaf_ij, dDeltaf_ij, _) = self.mbar.getFreeEnergyDifferences()

        # Matrix of free energy differences
        logger.info("Deltaf_ij:")
        for i in range(nstates):
            str_row = ""
            for j in range(nstates):
                str_row += "{:8.3f}".format(Deltaf_ij[i, j])
            logger.info(str_row)

        # Matrix of uncertainties in free energy difference (expectations standard
        # deviations of the estimator about the true free energy)
        logger.info("dDeltaf_ij:")
        for i in range(nstates):
            str_row = ""
            for j in range(nstates):
                str_row += "{:8.3f}".format(dDeltaf_ij[i, j])
            logger.info(str_row)

        # Return free energy differences and an estimate of the covariance.
        free_energy_dict = {'value': Deltaf_ij, 'error': dDeltaf_ij}
        self._computed_observables['free_energy'] = free_energy_dict

    def get_free_energy(self):
        """
        Compute the free energy and error in free energy from the MBAR object

        Output shape changes based on if there are unsampled states detected in the sampler

        Returns
        -------
        DeltaF_ij : ndarray of floats, shape (K,K) or (K+2, K+2)
            Difference in free energy from each state relative to each other state
        dDeltaF_ij : ndarray of floats, shape (K,K) or (K+2, K+2)
            Error in the difference in free energy from each state relative to each other state
        """
        if self._computed_observables['free_energy'] is None:
            self._compute_free_energy()
        free_energy_dict = self._computed_observables['free_energy']
        return free_energy_dict['value'], free_energy_dict['error']

    def _compute_enthalpy_and_entropy(self):
        """Function to compute the cached values of enthalpy and entropy"""
        (f_k, df_k, H_k, dH_k, S_k, dS_k) = self.mbar.computeEntropyAndEnthalpy()
        enthalpy = {'value': H_k, 'error': dH_k}
        entropy = {'value': S_k, 'error': dS_k}
        self._computed_observables['enthalpy'] = enthalpy
        self._computed_observables['entropy'] = entropy

    def get_enthalpy(self):
        """
        Compute the difference in enthalpy and error in that estimate from the MBAR object

        Output shape changes based on if there are unsampled states detected in the sampler

        Returns
        -------
        DeltaH_ij : ndarray of floats, shape (K,K) or (K+2, K+2)
            Difference in enthalpy from each state relative to each other state
        dDeltaH_ij : ndarray of floats, shape (K,K) or (K+2, K+2)
            Error in the difference in enthalpy from each state relative to each other state
        """
        if self._computed_observables['enthalpy'] is None:
            self._compute_enthalpy_and_entropy()
        enthalpy_dict = self._computed_observables['enthalpy']
        return enthalpy_dict['value'], enthalpy_dict['error']

    def get_entropy(self):
        """
        Compute the difference in entropy and error in that estimate from the MBAR object

        Output shape changes based on if there are unsampled states detected in the sampler

        Returns
        -------
        DeltaH_ij : ndarray of floats, shape (K,K) or (K+2, K+2)
            Difference in enthalpy from each state relative to each other state
        dDeltaH_ij : ndarray of floats, shape (K,K) or (K+2, K+2)
            Error in the difference in enthalpy from each state relative to each other state
        """
        if self._computed_observables['entropy'] is None:
            self._compute_enthalpy_and_entropy()
        entropy_dict = self._computed_observables['entropy']
        return entropy_dict['value'], entropy_dict['error']

    def _get_equilibration_data_auto(self, input_data=None):
        """
        Automatically generate the equilibration data from best practices.

        Parameters
        ----------
        input_data : np.ndarray-like, Optional, Default: None
            Optionally provide the data to look at. If not provided, uses energies from :func:`extract_energies()`

        Returns nothing, but sets self._equilibration_data
        """
        if input_data is None:
            input_data, _ = self._get_states_energies(truncate_max_n_iterations=True)
        u_n = self.get_timeseries(input_data)
        # Discard equilibration samples.
        # TODO: if we include u_n[0] (the energy right after minimization) in the equilibration detection,
        # TODO:         then number_equilibrated is 0. Find a better way than just discarding first frame.
        equilibration_data = list(utils.get_equilibration_data(u_n[1:]))
        # Discard also minimization frame.
        equilibration_data[0] += 1
        self._equilibration_data = tuple(equilibration_data)
        logger.debug('Equilibration data: {}'.format(equilibration_data))
        return self._equilibration_data

    # -------------------------------------------------------------------------
    # Cached properties.
    # -------------------------------------------------------------------------

    unbias_restraint = PhaseAnalyzer.CachedProperty(
        name='unbias_restraint',
        check_changes=True,
    )
    restraint_energy_cutoff = PhaseAnalyzer.CachedProperty(
        name='restraint_energy_cutoff',
        check_changes=True,
    )
    restraint_distance_cutoff = PhaseAnalyzer.CachedProperty(
        name='restraint_distance_cutoff',
        check_changes=True
    )

    _equilibration_data = PhaseAnalyzer.CachedProperty(
        name='equilibration_data',
        dependencies=['max_n_iterations'],
        check_changes=True,
        default=lambda instance: instance._get_equilibration_data_auto()
    )

    @staticmethod
    def _decorrelated_state_indices_kn_default_func(instance):
        decorrelated_iterations = instance._decorrelated_iterations  # Shortcut.
        replica_state_indices = instance._reporter.read_replica_thermodynamic_states()
        n_correlated_iterations, instance._n_replicas = replica_state_indices.shape

        # Initialize output array.
        n_frames = instance.n_replicas * len(decorrelated_iterations)
        decorrelated_state_indices_kn = np.zeros(n_frames, dtype=np.int32)

        # Map kn columns to the state.
        for iteration_idx, iteration in enumerate(decorrelated_iterations):
            for replica_idx in range(instance.n_replicas):
                # Deconvolute index.
                state_idx = replica_state_indices[iteration, replica_idx]
                frame_idx = state_idx*len(decorrelated_iterations) + iteration_idx
                # Set output array.
                decorrelated_state_indices_kn[frame_idx] = state_idx
        instance._decorrelated_state_indices_kn = decorrelated_state_indices_kn
        return decorrelated_state_indices_kn

    _decorrelated_state_indices_kn = PhaseAnalyzer.CachedProperty(
        name='decorrelated_state_indices_kn',
        dependencies=['equilibration_data'],
        default=_decorrelated_state_indices_kn_default_func.__func__
    )
    _decorrelated_u_kn = PhaseAnalyzer.CachedProperty(
        name='decorrelated_u_kn',
        dependencies=['equilibration_data'],
        default=lambda instance: instance._compute_decorrelated_energies()[0]
    )
    _decorrelated_N_k = PhaseAnalyzer.CachedProperty(
        name='decorrelated_N_k',
        dependencies=['equilibration_data'],
        default=lambda instance: instance._compute_decorrelated_energies()[1]
    )
    _unbiased_decorrelated_u_kn = PhaseAnalyzer.CachedProperty(
        name='unbiased_decorrelated_u_kn',
        dependencies=['unbias_restraint', 'restraint_energy_cutoff', 'restraint_distance_cutoff',
                      'decorrelated_state_indices_kn', 'decorrelated_u_kn', 'decorrelated_N_k'],
        default=lambda instance: instance._compute_unbiased_energies()[0]
    )
    _unbiased_decorrelated_N_k = PhaseAnalyzer.CachedProperty(
        name='unbiased_decorrelated_N_k',
        dependencies=['unbias_restraint', 'restraint_energy_cutoff', 'restraint_distance_cutoff',
                      'decorrelated_state_indices_kn', 'decorrelated_u_kn', 'decorrelated_N_k'],
        default=lambda instance: instance._compute_unbiased_energies()[1]
    )
    mbar = PhaseAnalyzer.CachedProperty(
        name='mbar',
        dependencies=['unbiased_decorrelated_u_kn', 'unbiased_decorrelated_N_k'],
        default=lambda instance: instance._create_mbar(instance._unbiased_decorrelated_u_kn,
                                                       instance._unbiased_decorrelated_N_k)
    )

    # -------------------------------------------------------------------------
    # Dynamic properties.
    # -------------------------------------------------------------------------

    @property
    def n_equilibration_iterations(self):
        return self._equilibration_data[0]

    @property
    def statistical_inefficiency(self):
        return self._equilibration_data[1]

    @property
    def _decorrelated_iterations(self):
        """list of int: the indices of the decorrelated iterations truncated to max_n_iterations."""
        equilibrium_iterations = np.array(range(self.n_equilibration_iterations, self.max_n_iterations + 1))
        decorrelated_iterations_indices = timeseries.subsampleCorrelatedData(equilibrium_iterations,
                                                                             self.statistical_inefficiency)
        return equilibrium_iterations[decorrelated_iterations_indices]

# https://choderalab.slack.com/files/levi.naden/F4G6L9X8S/quick_diagram.png

class MultiPhaseAnalyzer(object):
    """
    Multiple Phase Analyzer creator, not to be directly called itself, but instead called by adding or subtracting
    different implemented :class:`PhaseAnalyzer` or other :class:`MultiPhaseAnalyzers`'s. The individual Phases of
    the :class:`MultiPhaseAnalyzer` are only references to existing Phase objects, not copies. All
    :class:`PhaseAnalyzer` and :class:`MultiPhaseAnalyzer` classes support ``+`` and ``-`` operations.

    The observables of this phase are determined through inspection of all the passed in phases and only observables
    which are shared can be computed. For example:

        ``PhaseA`` has ``.get_free_energy`` and ``.get_entropy``

        ``PhaseB`` has ``.get_free_energy`` and ``.get_enthalpy``,

        ``PhaseAB = PhaseA + PhaseB`` will only have a ``.get_free_energy`` method

    Because each Phase may have a different number of states, the ``reference_states`` property of each phase
    determines which states from each phase to read the data from.

    For observables defined by two states, the i'th and j'th reference states are used:

        If we define ``PhaseAB = PhaseA - PhaseB``

        Then ``PhaseAB.get_free_energy()`` is roughly equivalent to doing the following:

            ``A_i, A_j = PhaseA.reference_states``

            ``B_i, B_j = PhaseB.reference_states``

            ``PhaseA.get_free_energy()[A_i, A_j] - PhaseB.get_free_energy()[B_i, B_j]``

        The above is not exact since get_free_energy returns an error estimate as well

    For observables defined by a single state, only the i'th reference state is used

        Given ``PhaseAB = PhaseA + PhaseB``, ``PhaseAB.get_temperature()`` is equivalent to:

            ``A_i = PhaseA.reference_states[0]``

            ``B_i = PhaseB.reference_states[0]``

            ``PhaseA.get_temperature()[A_i] + PhaseB.get_temperature()[B_i]``

    For observables defined entirely by the phase, no reference states are needed.

        Given ``PhaseAB = PhaseA + PhaseB``, ``PhaseAB.get_standard_state_correction()`` gives:

            ``PhaseA.get_standard_state_correction() + PhaseB.get_standard_state_correction()``

    Each phase MUST use the same ObservablesRegistry, otherwise an error is raised

    This class is public to see its API.

    Parameters
    ----------
    phases : dict
        has keys "phases", "names", and "signs"

    Attributes
    ----------
    observables
    phases
    names
    signs
    registry

    See Also
    --------
    PhaseAnalyzer
    ObservablesRegistry

    """
    def __init__(self, phases):
        """
        Create the compound phase which is any combination of phases to generate a new MultiPhaseAnalyzer.

        """
        # Compare ObservableRegistries
        ref_registry = phases['phases'][0].registry
        for phase in phases['phases'][1:]:
            # Use is comparison since we are checking same insetance
            if phase.registry is not ref_registry:
                raise ValueError("Not all phases have the same ObservablesRegistry! Observable calculation "
                                 "will be inconsistent!")
        self.registry = ref_registry
        # Determine available observables
        observables = []
        for observable in self.registry.observables:
            shared_observable = True
            for phase in phases['phases']:
                if observable not in phase.observables:
                    shared_observable = False
                    break
            if shared_observable:
                observables.append(observable)
        if len(observables) == 0:
            raise RuntimeError("There are no shared computable observable between the phases, combining them will do "
                               "nothing.")
        self._observables = tuple(observables)
        self._phases = phases['phases']
        self._names = phases['names']
        self._signs = phases['signs']
        # Set the methods shared between both objects
        for observable in self.observables:
            setattr(self, "get_" + observable, self._spool_function(observable))

    def _spool_function(self, observable):
        """
        Dynamic observable calculator layer

        Must be in its own function to isolate the variable name space

        If you have this in the __init__, the "observable" variable colides with any others in the list, causing a
        the wrong property to be fetched.
        """
        return lambda: self._compute_observable(observable)

    @property
    def observables(self):
        """List of observables this :class:`MultiPhaseAnalyzer` can generate"""
        return self._observables

    @property
    def phases(self):
        """List of implemented :class:`PhaseAnalyzer`'s objects this :class:`MultiPhaseAnalyzer` is tied to"""
        return self._phases

    @property
    def names(self):
        """
        Unique list of string names identifying this phase. If this :class:`MultiPhaseAnalyzer` is combined with
        another, its possible that new names will be generated unique to that :class:`MultiPhaseAnalyzer`, but will
        still reference the same phase.

        When in doubt, use :func:`MultiPhaseAnalyzer.phases` to get the actual phase objects.
        """
        return self._names

    @property
    def signs(self):
        """
        List of signs that are used by the :class:`MultiPhaseAnalyzer` to
        """
        return self._signs

    def _combine_phases(self, other, operator='+'):
        """
        Function to combine the phases regardless of operator to reduce code duplication. Creates a new
        :class:`MultiPhaseAnalyzer` object based on the combined phases of the other. Accepts either a
        :class:`PhaseAnalyzer` or a :class:`MultiPhaseAnalyzer`.

        If the names have collision, they are re-named with an extra digit at the end.

        Parameters
        ----------
        other : :class:`MultiPhaseAnalyzer` or :class:`PhaseAnalyzer`
        operator : sign of the operator connecting the two objects

        Returns
        -------
        output : :class:`MultiPhaseAnalyzer`
            New :class:`MultiPhaseAnalyzer` where the phases are the combined list of the individual phases from each
            component. Because the memory pointers to the individual phases are the same, changing any
            single :class:`PhaseAnalyzer`'s
            reference_state objects updates all :class:`MultiPhaseAnalyzer` objects they are tied to

        """
        phases = []
        names = []
        signs = []
        # create copies
        phases.extend(self.phases)
        names.extend(self.names)
        signs.extend(self.signs)
        if isinstance(other, MultiPhaseAnalyzer):
            new_phases = other.phases
            new_signs = other.signs
            new_names = other.names
            final_new_names = []
            for name in new_names:
                other_names = [n for n in new_names if n != name]
                final_new_names.append(utils.generate_phase_name(name, other_names + names))
            names.extend(final_new_names)
            for new_sign in new_signs:
                if (operator == '-' and new_sign == '+') or (operator == '+' and new_sign == '-'):
                    signs.append('-')
                else:
                    signs.append('+')
            signs.extend(new_signs)
            phases.extend(new_phases)
        elif isinstance(other, PhaseAnalyzer):
            names.append(utils.generate_phase_name(other.name, names))
            if (operator == '-' and other._sign == '+') or (operator == '+' and other._sign == '-'):
                signs.append('-')
            else:
                signs.append('+')
            other._sign = '+'  # Recast to positive if negated
            phases.append(other)
        else:
            baseerr = "cannot {} 'MultiPhaseAnalyzer' and '{}' objects"
            if operator == '+':
                err = baseerr.format('add', type(other))
            else:
                err = baseerr.format('subtract', type(other))
            raise TypeError(err)
        phase_pass = {'phases': phases, 'signs': signs, 'names': names}
        return MultiPhaseAnalyzer(phase_pass)

    def __add__(self, other):
        return self._combine_phases(other, operator='+')

    def __sub__(self, other):
        return self._combine_phases(other, operator='-')

    def __neg__(self):
        """
        Return a SHALLOW copy of self with negated signs so that the phase objects all still point to the same
        objects
        """
        new_signs = []
        for sign in self._signs:
            if sign == '+':
                new_signs.append('-')
            else:
                new_signs.append('+')
        # return a *shallow* copy of self with the signs reversed
        output = copy.copy(self)
        output._signs = new_signs
        return output

    def __str__(self):
        """Simplified string output"""
        header = "MultiPhaseAnalyzer<{}>"
        output_string = ""
        for phase_name, sign in zip(self.names, self.signs):
            if output_string == "" and sign == '-':
                output_string += '{}{} '.format(sign, phase_name)
            elif output_string == "":
                output_string += '{} '.format(phase_name)
            else:
                output_string += '{} {} '.format(sign, phase_name)
        return header.format(output_string)

    def __repr__(self):
        """Generate a detailed representation of the MultiPhase"""
        header = "MultiPhaseAnalyzer <\n{}>"
        output_string = ""
        for phase, phase_name, sign in zip(self.phases, self.names, self.signs):
            if output_string == "" and sign == '-':
                output_string += '{}{} ({})\n'.format(sign, phase_name, phase)
            elif output_string == "":
                output_string += '{} ({})\n'.format(phase_name, phase)
            else:
                output_string += '    {} {} ({})\n'.format(sign, phase_name, phase)
        return header.format(output_string)

    def _compute_observable(self, observable_name):
        """
        Helper function to compute arbitrary observable in both phases

        Parameters
        ----------
        observable_name : str
            Name of the observable as its defined in the ObservablesRegistry

        Returns
        -------
        observable_value
            The observable as its combined between all the phases

        """
        def prepare_phase_observable(single_phase):
            """Helper function to cast the observable in terms of observable's registry"""
            observable = getattr(single_phase, "get_" + observable_name)()
            if isinstance(single_phase, MultiPhaseAnalyzer):
                if observable_name in self.registry.observables_with_error:
                    observable_payload = dict()
                    observable_payload['value'], observable_payload['error'] = observable
                else:
                    observable_payload = observable
            else:
                raise_registry_error = False
                if observable_name in self.registry.observables_with_error:
                    observable_payload = {}
                    if observable_name in self.registry.observables_defined_by_phase:
                        observable_payload['value'], observable_payload['error'] = observable
                    elif observable_name in self.registry.observables_defined_by_single_state:
                        observable_payload['value'] = observable[0][single_phase.reference_states[0]]
                        observable_payload['error'] = observable[1][single_phase.reference_states[0]]
                    elif observable_name in self.registry.observables_defined_by_two_states:
                        observable_payload['value'] = observable[0][single_phase.reference_states[0],
                                                                    single_phase.reference_states[1]]
                        observable_payload['error'] = observable[1][single_phase.reference_states[0],
                                                                    single_phase.reference_states[1]]
                    else:
                        raise_registry_error = True
                else:  # No error
                    if observable_name in self.registry.observables_defined_by_phase:
                        observable_payload = observable
                    elif observable_name in self.registry.observables_defined_by_single_state:
                        observable_payload = observable[single_phase.reference_states[0]]
                    elif observable_name in self.registry.observables_defined_by_two_states:
                        observable_payload = observable[single_phase.reference_states[0],
                                                        single_phase.reference_states[1]]
                    else:
                        raise_registry_error = True
                if raise_registry_error:
                    raise RuntimeError("You have requested an observable that is improperly registered in the "
                                       "ObservablesRegistry!")
            return observable_payload

        def modify_final_output(passed_output, payload, sign):
            if observable_name in self.registry.observables_with_error:
                if sign == '+':
                    passed_output['value'] += payload['value']
                else:
                    passed_output['value'] -= payload['value']
                if observable_name in self.registry.observables_with_error_adding_linear:
                    passed_output['error'] += payload['error']
                elif observable_name in self.registry.observables_with_error_adding_quadrature:
                    passed_output['error'] = (passed_output['error']**2 + payload['error']**2)**0.5
            else:
                if sign == '+':
                    passed_output += payload
                else:
                    passed_output -= payload
            return passed_output

        if observable_name in self.registry.observables_with_error:
            final_output = {'value': 0, 'error': 0}
        else:
            final_output = 0
        for phase, phase_sign in zip(self.phases, self.signs):
            phase_observable = prepare_phase_observable(phase)
            final_output = modify_final_output(final_output, phase_observable, phase_sign)
        if observable_name in self.registry.observables_with_error:
            # Cast output to tuple
            final_output = (final_output['value'], final_output['error'])
        return final_output
