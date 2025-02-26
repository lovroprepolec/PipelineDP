import abc
import typing
import pickle
from dataclasses import dataclass
from functools import reduce

from typing import Iterable, Optional, Tuple, Union
import pipeline_dp
from pipeline_dp import aggregate_params
from pipeline_dp import dp_computations
from pipeline_dp import budget_accounting
import numpy as np


@dataclass
class AccumulatorParams:
    accumulator_type: type
    constructor_params: typing.Any


def merge(accumulators: typing.Iterable['Accumulator']) -> 'Accumulator':
    """Merges the accumulators."""
    return reduce(lambda acc1, acc2: acc1.add_accumulator(acc2), accumulators)


def create_accumulator_params(
    aggregation_params: pipeline_dp.AggregateParams,
    budget_accountant: budget_accounting.BudgetAccountant
) -> typing.List[AccumulatorParams]:
    accumulator_params = []
    if pipeline_dp.Metrics.COUNT in aggregation_params.metrics:
        # TODO: populate CountParams from budget_accountant when it is ready
        accumulator_params.append(
            AccumulatorParams(accumulator_type=CountAccumulator,
                              constructor_params=CountParams()))
    if pipeline_dp.Metrics.SUM in aggregation_params.metrics:
        budget = budget_accountant.request_budget(
            aggregation_params.noise_kind.convert_to_mechanism_type())
        sum_params = SumParams(budget, aggregation_params)
        accumulator_params.append(
            AccumulatorParams(accumulator_type=SumAccumulator,
                              constructor_params=sum_params))

    return accumulator_params


class Accumulator(abc.ABC):
    """Base class for all accumulators.

    Accumulators are objects that encapsulate aggregations and computations of
    differential private metrics.
    """

    @abc.abstractmethod
    def add_value(self, value):
        """Adds 'value' to accumulate.
        Args:
          value: value to be added.

        Returns: self.
        """
        pass

    def _check_mergeable(self, accumulator: 'Accumulator'):
        if not isinstance(accumulator, type(self)):
            raise TypeError(
                f"The accumulator to be added is not of the same type: "
                f"{accumulator.__class__.__name__} != "
                f"{self.__class__.__name__}")

    @abc.abstractmethod
    def add_accumulator(self, accumulator: 'Accumulator') -> 'Accumulator':
        """Merges the accumulator to self and returns self.

        Sub-class implementation is responsible for checking that types of
        self and accumulator are the same.

        Args:
         accumulator:

        Returns: self
        """
        pass

    @abc.abstractmethod
    def compute_metrics(self):
        """Computes and returns the result of aggregation."""
        pass

    def serialize(self):
        return pickle.dumps(self)

    @classmethod
    def deserialize(cls, serialized_obj: str):
        deserialized_obj = pickle.loads(serialized_obj)
        if not isinstance(deserialized_obj, cls):
            raise TypeError("The deserialized object is not of the right type.")
        return deserialized_obj


class CompoundAccumulator(Accumulator):
    """Accumulator for computing multiple metrics at once.

    CompoundAccumulator contains one or more accumulators of other types for
    computing multiple metrics.
    For example it can contain [CountAccumulator,  SumAccumulator].
    CompoundAccumulator delegates all operations to the internal accumulators.
    """

    def __init__(self, accumulators: typing.Iterable['Accumulator']):
        """Constructs CompoundAccumulator.

        The assumption is that each accumulator from 'accumulators' contain data
        from the same privacy id.
        """
        self._accumulators = accumulators
        self._privacy_id_count = 1

    def add_value(self, value):
        """Adds 'value' to accumulate.

        The assumption is that value correspond to privacy id which is already
        known for self.
        """
        for accumulator in self._accumulators:
            accumulator.add_value(value)
        return self

    def add_accumulator(self, accumulator: 'CompoundAccumulator') -> \
      'CompoundAccumulator':
        """Merges the accumulators of the CompoundAccumulators.

        The expectation is that the internal accumulators are of the same type
        and are in the same order.

        The assumption is that self and accumulator have data from
        non-overlapping set of privacy ids.
        """
        self._check_mergeable(accumulator)
        if len(accumulator._accumulators) != len(self._accumulators):
            raise ValueError(
                "Accumulators in the input are not of the same size." +
                f" Expected size = {len(self._accumulators)}" +
                f" received size = {len(accumulator._accumulators)}.")

        self._privacy_id_count += accumulator._privacy_id_count

        for pos, (base_accumulator_type, to_add_accumulator_type) in enumerate(
                zip(self._accumulators, accumulator._accumulators)):
            if type(base_accumulator_type) != type(to_add_accumulator_type):
                raise TypeError(
                    "The type of the accumulators don't match at "
                    f"index {pos}. {type(base_accumulator_type).__name__} "
                    f"!= {type(to_add_accumulator_type).__name__}.")

        for (base_accumulator,
             to_add_accumulator) in zip(self._accumulators,
                                        accumulator._accumulators):
            base_accumulator.add_accumulator(to_add_accumulator)
        return self

    @property
    def privacy_id_count(self):
        """Returns the number of privacy ids which contributed to 'self'."""
        return self._privacy_id_count

    def compute_metrics(self):
        """Returns a list of metrics computed by internal accumulators."""
        return [
            accumulator.compute_metrics() for accumulator in self._accumulators
        ]


class AccumulatorFactory:
    """Factory for producing the appropriate Accumulator depending on the
    AggregateParams and BudgetAccountant."""

    def __init__(self, params: pipeline_dp.AggregateParams,
                 budget_accountant: budget_accounting.BudgetAccountant):
        self._params = params
        self._budget_accountant = budget_accountant

    def initialize(self):
        self._accumulator_params = create_accumulator_params(
            self._params, self._budget_accountant)

    def create(self, values: typing.List) -> Accumulator:
        accumulators = []
        for accumulator_param in self._accumulator_params:
            accumulators.append(
                accumulator_param.accumulator_type(
                    accumulator_param.constructor_params, values))

        return CompoundAccumulator(accumulators)


@dataclass
class CountParams:
    pass


class CountAccumulator(Accumulator):

    def __init__(self, params: CountParams, values):
        self._count = len(values)

    def add_value(self, value):
        self._count += 1

    def add_accumulator(self,
                        accumulator: 'CountAccumulator') -> 'CountAccumulator':
        self._check_mergeable(accumulator)
        self._count += accumulator._count
        return self

    def compute_metrics(self) -> float:
        # TODO: add differential privacy
        return self._count


_FloatVector = Union[Tuple[float], np.ndarray]


class VectorSummationAccumulator(Accumulator):
    _vec_sum: np.ndarray
    _params: dp_computations.AdditiveVectorNoiseParams

    def __init__(self, params: dp_computations.AdditiveVectorNoiseParams,
                 values: Iterable[_FloatVector]):
        if not isinstance(params, dp_computations.AdditiveVectorNoiseParams):
            raise TypeError(
                f"'params' parameters should be of type "
                f"dp_computations.AdditiveVectorNoiseParams, not {params.__class__.__name__}"
            )
        self._params = params
        self._vec_sum = None
        for val in values:
            self.add_value(val)

    def add_value(self, value: _FloatVector):
        if not isinstance(value, np.ndarray):
            value = np.array(value)

        if self._vec_sum is None:
            self._vec_sum = value
        else:
            if self._vec_sum.shape != value.shape:
                raise TypeError(
                    f"Shape mismatch: {self._vec_sum.shape} != {value.shape}")
            self._vec_sum += value
        return self

    def add_accumulator(
        self, accumulator: 'VectorSummationAccumulator'
    ) -> 'VectorSummationAccumulator':
        self._check_mergeable(accumulator)
        self.add_value(accumulator._vec_sum)
        return self

    def compute_metrics(self):
        if self._vec_sum is None:
            raise IndexError("No data provided for metrics computation.")
        return dp_computations.add_noise_vector(self._vec_sum, self._params)


class SumParams:
    """Parameters for a SumAccumulator.

    Wraps epsilon and delta from the budget which are lazily loaded.
    AggregateParams are copied into a MeanVarParams instance.
    """

    def __init__(self, budget: pipeline_dp.budget_accounting.MechanismSpec,
                 aggregate_params: aggregate_params.AggregateParams):
        self._budget = budget
        self._aggregate_params = aggregate_params

    @property
    def eps(self):
        return self._budget.eps

    @property
    def delta(self):
        return self._budget.delta

    @property
    def mean_var_params(self):
        return dp_computations.MeanVarParams(
            self.eps, self.delta, self._aggregate_params.low,
            self._aggregate_params.high,
            self._aggregate_params.max_partitions_contributed,
            self._aggregate_params.max_contributions_per_partition,
            self._aggregate_params.noise_kind)


class SumAccumulator(Accumulator):

    def __init__(self, params: SumParams, values):
        self._sum = sum(values)
        self._params = params

    def add_value(self, value):
        self._sum += value

    def add_accumulator(self,
                        accumulator: 'SumAccumulator') -> 'SumAccumulator':
        self._check_mergeable(accumulator)
        self._sum += accumulator._sum

    def compute_metrics(self) -> float:
        return pipeline_dp.dp_computations.compute_dp_sum(
            self._sum, self._params.mean_var_params)
