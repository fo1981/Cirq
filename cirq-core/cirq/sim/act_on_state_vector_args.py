# Copyright 2018 The Cirq Developers
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Objects and methods for acting efficiently on a state vector."""

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING, Type, Union

import numpy as np

from cirq import _compat, linalg, protocols, qis, sim
from cirq._compat import proper_repr
from cirq.sim.act_on_args import ActOnArgs, strat_act_on_from_apply_decompose
from cirq.linalg import transformations

if TYPE_CHECKING:
    import cirq
    from numpy.typing import DTypeLike


class _BufferedStateVector:
    """Contains the state vector and buffer for efficient state evolution."""

    def __init__(self, state_vector: np.ndarray, buffer: Optional[np.ndarray] = None):
        """Initializes the object with the inputs.

        This initializer creates the buffer if necessary.

        Args:
            state_vector: The state vector, must be correctly formatted. The data is not checked
                for validity here due to performance concerns.
            buffer: Optional, must be same shape as the state vector. If not provided, a buffer
                will be created automatically.
        """
        self._state_vector = state_vector
        if buffer is None:
            buffer = np.empty_like(state_vector)
        self._buffer = buffer
        self._qid_shape = state_vector.shape

    @classmethod
    def create(
        cls,
        *,
        initial_state: Union[np.ndarray, 'cirq.STATE_VECTOR_LIKE'] = 0,
        qid_shape: Optional[Tuple[int, ...]] = None,
        dtype: Optional['DTypeLike'] = None,
        buffer: Optional[List[np.ndarray]] = None,
    ):
        """Initializes the object with the inputs.

        This initializer creates the buffer if necessary.

        Args:
            initial_state: The density matrix, must be correctly formatted. The data is not
                checked for validity here due to performance concerns.
            qid_shape: The shape of the density matrix, if the initial state is provided as an int.
            dtype: The dtype of the density matrix, if the initial state is provided as an int.
            buffer: Optional, must be length 3 and same shape as the density matrix. If not
                provided, a buffer will be created automatically.
        Raises:
            ValueError: If initial state is provided as integer, but qid_shape is not provided.
        """
        if not isinstance(initial_state, np.ndarray):
            if qid_shape is None:
                raise ValueError('qid_shape must be provided if initial_state is not ndarray')
            state_vector = qis.to_valid_state_vector(
                initial_state, len(qid_shape), qid_shape=qid_shape, dtype=dtype
            ).reshape(qid_shape)
        else:
            if qid_shape is not None:
                state_vector = initial_state.reshape(qid_shape)
            else:
                state_vector = initial_state
            if np.may_share_memory(state_vector, initial_state):
                state_vector = state_vector.copy()
        state_vector = state_vector.astype(dtype, copy=False)
        return cls(state_vector, buffer)

    def copy(self, deep_copy_buffers: bool = True) -> '_BufferedStateVector':
        """Copies the object.

        Args:
            deep_copy_buffers: True by default, False to reuse the existing buffers.
        Returns:
            A copy of the object.
        """
        return _BufferedStateVector(
            state_vector=self._state_vector.copy(),
            buffer=self._buffer.copy() if deep_copy_buffers else self._buffer,
        )

    def kron(self, other: '_BufferedStateVector') -> '_BufferedStateVector':
        """Creates the Kronecker product with the other state vector.

        Args:
            other: The state vector with which to kron.
        Returns:
            The Kronecker product of the two state vectors.
        """
        target_tensor = transformations.state_vector_kronecker_product(
            self._state_vector, other._state_vector
        )
        return _BufferedStateVector(
            state_vector=target_tensor,
            buffer=np.empty_like(target_tensor),
        )

    def factor(
        self, axes: Sequence[int], *, validate=True, atol=1e-07
    ) -> Tuple['_BufferedStateVector', '_BufferedStateVector']:
        """Factors a state vector into two independent state vectors.

        This function should only be called on state vectors that are known to be separable, such
        as immediately after a measurement or reset operation. It does not verify that the provided
        state vector is indeed separable, and will return nonsense results for vectors
        representing entangled states.

        Args:
            axes: The axes to factor out.
            validate: Perform a validation that the state vector factors cleanly.
            atol: The absolute tolerance for the validation.

        Returns:
            A tuple with the `(extracted, remainder)` state vectors, where `extracted` means the
            sub-state vector which corresponds to the axes requested, and with the axes in the
            requested order, and where `remainder` means the sub-state vector on the remaining
            axes, in the same order as the original state vector.
        """
        extracted_tensor, remainder_tensor = transformations.factor_state_vector(
            self._state_vector, axes, validate=validate, atol=atol
        )
        extracted = _BufferedStateVector(
            state_vector=extracted_tensor,
            buffer=np.empty_like(extracted_tensor),
        )
        remainder = _BufferedStateVector(
            state_vector=remainder_tensor,
            buffer=np.empty_like(remainder_tensor),
        )
        return extracted, remainder

    def reindex(self, axes: Sequence[int]) -> '_BufferedStateVector':
        """Transposes the axes of a state vector to a specified order.

        Args:
            axes: The desired axis order.
        Returns:
            The transposed state vector.
        """
        new_tensor = transformations.transpose_state_vector_to_axis_order(self._state_vector, axes)
        return _BufferedStateVector(
            state_vector=new_tensor,
            buffer=np.empty_like(new_tensor),
        )

    def apply_unitary(self, action: Any, axes: Sequence[int]) -> bool:
        """Apply unitary to state.

        Args:
            action: The value with a unitary to apply.
            axes: The axes on which to apply the unitary.
        Returns:
            True if the operation succeeded.
        """
        new_target_tensor = protocols.apply_unitary(
            action,
            protocols.ApplyUnitaryArgs(
                target_tensor=self._state_vector,
                available_buffer=self._buffer,
                axes=axes,
            ),
            allow_decompose=False,
            default=NotImplemented,
        )
        if new_target_tensor is NotImplemented:
            return False
        self._swap_target_tensor_for(new_target_tensor)
        return True

    def apply_mixture(self, action: Any, axes: Sequence[int], prng) -> Optional[int]:
        """Apply mixture to state.

        Args:
            action: The value with a mixture to apply.
            axes: The axes on which to apply the mixture.
            prng: The pseudo random number generator to use.
        Returns:
            The mixture index if the operation succeeded, otherwise None.
        """
        mixture = protocols.mixture(action, default=None)
        if mixture is None:
            return None
        probabilities, unitaries = zip(*mixture)

        index = prng.choice(range(len(unitaries)), p=probabilities)
        shape = protocols.qid_shape(action) * 2
        unitary = unitaries[index].astype(self._state_vector.dtype).reshape(shape)
        linalg.targeted_left_multiply(unitary, self._state_vector, axes, out=self._buffer)
        self._swap_target_tensor_for(self._buffer)
        return index

    def apply_channel(self, action: Any, axes: Sequence[int], prng) -> Optional[int]:
        """Apply channel to state.

        Args:
            action: The value with a channel to apply.
            axes: The axes on which to apply the channel.
            prng: The pseudo random number generator to use.
        Returns:
            The kraus index if the operation succeeded, otherwise None.
        """
        kraus_operators = protocols.kraus(action, default=None)
        if kraus_operators is None:
            return None

        def prepare_into_buffer(k: int):
            linalg.targeted_left_multiply(
                left_matrix=kraus_tensors[k],
                right_target=self._state_vector,
                target_axes=axes,
                out=self._buffer,
            )

        shape = protocols.qid_shape(action)
        kraus_tensors = [
            e.reshape(shape * 2).astype(self._state_vector.dtype) for e in kraus_operators
        ]
        p = prng.random()
        weight = None
        fallback_weight = 0
        fallback_weight_index = 0
        index = None
        for index in range(len(kraus_tensors)):
            prepare_into_buffer(index)
            weight = np.linalg.norm(self._buffer) ** 2

            if weight > fallback_weight:
                fallback_weight_index = index
                fallback_weight = weight

            p -= weight
            if p < 0:
                break

        assert weight is not None, "No Kraus operators"
        if p >= 0 or weight == 0:
            # Floating point error resulted in a malformed sample.
            # Fall back to the most likely case.
            prepare_into_buffer(fallback_weight_index)
            weight = fallback_weight
            index = fallback_weight_index

        self._buffer /= np.sqrt(weight)
        self._swap_target_tensor_for(self._buffer)
        return index

    def measure(
        self, axes: Sequence[int], seed: 'cirq.RANDOM_STATE_OR_SEED_LIKE' = None
    ) -> List[int]:
        """Measures the state vector.

        Args:
            axes: The axes to measure.
            seed: The random number seed to use.
        Returns:
            The measurements in order.
        """
        bits, _ = sim.measure_state_vector(
            self._state_vector,
            axes,
            out=self._state_vector,
            qid_shape=self._qid_shape,
            seed=seed,
        )
        return bits

    def sample(
        self,
        axes: Sequence[int],
        repetitions: int = 1,
        seed: 'cirq.RANDOM_STATE_OR_SEED_LIKE' = None,
    ) -> np.ndarray:
        """Samples the state vector.

        Args:
            axes: The axes to sample.
            repetitions: The number of samples to make.
            seed: The random number seed to use.
        Returns:
            The samples in order.
        """
        return sim.sample_state_vector(
            self._state_vector,
            axes,
            qid_shape=self._qid_shape,
            repetitions=repetitions,
            seed=seed,
        )

    def _swap_target_tensor_for(self, new_target_tensor: np.ndarray):
        """Gives a new state vector for the system.

        Typically, the new state vector should be `args.available_buffer` where
        `args` is this `cirq.ActOnStateVectorArgs` instance.

        Args:
            new_target_tensor: The new system state. Must have the same shape
                and dtype as the old system state.
        """
        if new_target_tensor is self._buffer:
            self._buffer = self._state_vector
        self._state_vector = new_target_tensor


class ActOnStateVectorArgs(ActOnArgs):
    """State and context for an operation acting on a state vector.

    There are two common ways to act on this object:

    1. Directly edit the `target_tensor` property, which is storing the state
        vector of the quantum system as a numpy array with one axis per qudit.
    2. Overwrite the `available_buffer` property with the new state vector, and
        then pass `available_buffer` into `swap_target_tensor_for`.
    """

    @_compat.deprecated_parameter(
        deadline='v0.15',
        fix='Use classical_data.',
        parameter_desc='log_of_measurement_results and positional arguments',
        match=lambda args, kwargs: 'log_of_measurement_results' in kwargs or len(args) > 4,
    )
    @_compat.deprecated_parameter(
        deadline='v0.15',
        fix='Use initial_state instead and specify all the arguments with keywords.',
        parameter_desc='target_tensor and positional arguments',
        match=lambda args, kwargs: 'target_tensor' in kwargs or len(args) != 1,
    )
    def __init__(
        self,
        target_tensor: Optional[np.ndarray] = None,
        available_buffer: Optional[np.ndarray] = None,
        prng: Optional[np.random.RandomState] = None,
        log_of_measurement_results: Optional[Dict[str, List[int]]] = None,
        qubits: Optional[Sequence['cirq.Qid']] = None,
        initial_state: Union[np.ndarray, 'cirq.STATE_VECTOR_LIKE'] = 0,
        dtype: Type[np.number] = np.complex64,
        classical_data: Optional['cirq.ClassicalDataStore'] = None,
    ):
        """Inits ActOnStateVectorArgs.

        Args:
            target_tensor: The state vector to act on, stored as a numpy array
                with one dimension for each qubit in the system. Operations are
                expected to perform inplace edits of this object.
            available_buffer: A workspace with the same shape and dtype as
                `target_tensor`. Used by operations that cannot be applied to
                `target_tensor` inline, in order to avoid unnecessary
                allocations. Passing `available_buffer` into
                `swap_target_tensor_for` will swap it for `target_tensor`.
            qubits: Determines the canonical ordering of the qubits. This
                is often used in specifying the initial state, i.e. the
                ordering of the computational basis states.
            prng: The pseudo random number generator to use for probabilistic
                effects.
            log_of_measurement_results: A mutable object that measurements are
                being recorded into.
            initial_state: The initial state for the simulation in the
                computational basis.
            dtype: The `numpy.dtype` of the inferred state vector. One of
                `numpy.complex64` or `numpy.complex128`. Only used when
                `target_tenson` is None.
            classical_data: The shared classical data container for this
                simulation.
        """
        super().__init__(
            prng=prng,
            qubits=qubits,
            log_of_measurement_results=log_of_measurement_results,
            classical_data=classical_data,
        )
        self._state = _BufferedStateVector.create(
            initial_state=target_tensor if target_tensor is not None else initial_state,
            qid_shape=tuple(q.dimension for q in qubits) if qubits is not None else None,
            dtype=dtype,
            buffer=available_buffer,
        )

    @_compat.deprecated(
        deadline='v0.16',
        fix='None, this function was unintentionally made public.',
    )
    def swap_target_tensor_for(self, new_target_tensor: np.ndarray):
        """Gives a new state vector for the system.

        Typically, the new state vector should be `args.available_buffer` where
        `args` is this `cirq.ActOnStateVectorArgs` instance.

        Args:
            new_target_tensor: The new system state. Must have the same shape
                and dtype as the old system state.
        """
        self._state._swap_target_tensor_for(new_target_tensor)

    @_compat.deprecated(
        deadline='v0.16',
        fix='None, this function was unintentionally made public.',
    )
    def subspace_index(
        self, axes: Sequence[int], little_endian_bits_int: int = 0, *, big_endian_bits_int: int = 0
    ) -> Tuple[Union[slice, int, 'ellipsis'], ...]:
        """An index for the subspace where the target axes equal a value.

        Args:
            axes: The qubits that are specified by the index bits.
            little_endian_bits_int: The desired value of the qubits at the
                targeted `axes`, packed into an integer. The least significant
                bit of the integer is the desired bit for the first axis, and
                so forth in increasing order. Can't be specified at the same
                time as `big_endian_bits_int`.

                When operating on qudits instead of qubits, the same basic logic
                applies but in a different basis. For example, if the target
                axes have dimension [a:2, b:3, c:2] then the integer 10
                decomposes into [a=0, b=2, c=1] via 7 = 1*(3*2) +  2*(2) + 0.
            big_endian_bits_int: The desired value of the qubits at the
                targeted `axes`, packed into an integer. The most significant
                bit of the integer is the desired bit for the first axis, and
                so forth in decreasing order. Can't be specified at the same
                time as `little_endian_bits_int`.

                When operating on qudits instead of qubits, the same basic logic
                applies but in a different basis. For example, if the target
                axes have dimension [a:2, b:3, c:2] then the integer 10
                decomposes into [a=1, b=2, c=0] via 7 = 1*(3*2) +  2*(2) + 0.

        Returns:
            A value that can be used to index into `target_tensor` and
            `available_buffer`, and manipulate only the part of Hilbert space
            corresponding to a given bit assignment.

        Example:
            If `target_tensor` is a 4 qubit tensor and `axes` is `[1, 3]` and
            then this method will return the following when given
            `little_endian_bits=0b01`:

                `(slice(None), 0, slice(None), 1, Ellipsis)`

            Therefore the following two lines would be equivalent:

                args.target_tensor[args.subspace_index(0b01)] += 1

                args.target_tensor[:, 0, :, 1] += 1
        """
        return linalg.slice_for_qubits_equal_to(
            axes,
            little_endian_qureg_value=little_endian_bits_int,
            big_endian_qureg_value=big_endian_bits_int,
            qid_shape=self.target_tensor.shape,
        )

    def _act_on_fallback_(
        self,
        action: Union['cirq.Operation', 'cirq.Gate'],
        qubits: Sequence['cirq.Qid'],
        allow_decompose: bool = True,
    ) -> bool:
        strats: List[Callable[[Any, Any, Sequence['cirq.Qid']], bool]] = [
            _strat_act_on_state_vector_from_apply_unitary,
            _strat_act_on_state_vector_from_mixture,
            _strat_act_on_state_vector_from_channel,
        ]
        if allow_decompose:
            strats.append(strat_act_on_from_apply_decompose)

        # Try each strategy, stopping if one works.
        for strat in strats:
            result = strat(action, self, qubits)
            if result is False:
                break  # coverage: ignore
            if result is True:
                return True
            assert result is NotImplemented, str(result)
        raise TypeError(
            "Can't simulate operations that don't implement "
            "SupportsUnitary, SupportsConsistentApplyUnitary, "
            "SupportsMixture or is a measurement: {!r}".format(action)
        )

    def _perform_measurement(self, qubits: Sequence['cirq.Qid']) -> List[int]:
        """Delegates the call to measure the state vector."""
        return self._state.measure(self.get_axes(qubits), self.prng)

    def _on_copy(self, target: 'cirq.ActOnStateVectorArgs', deep_copy_buffers: bool = True):
        target._state = self._state.copy(deep_copy_buffers)

    def _on_kronecker_product(
        self, other: 'cirq.ActOnStateVectorArgs', target: 'cirq.ActOnStateVectorArgs'
    ):
        target._state = self._state.kron(other._state)

    def _on_factor(
        self,
        qubits: Sequence['cirq.Qid'],
        extracted: 'cirq.ActOnStateVectorArgs',
        remainder: 'cirq.ActOnStateVectorArgs',
        validate=True,
        atol=1e-07,
    ):
        axes = self.get_axes(qubits)
        extracted._state, remainder._state = self._state.factor(axes, validate=validate, atol=atol)

    @property
    def allows_factoring(self):
        return True

    def _on_transpose_to_qubit_order(
        self, qubits: Sequence['cirq.Qid'], target: 'cirq.ActOnStateVectorArgs'
    ):
        target._state = self._state.reindex(self.get_axes(qubits))

    def sample(
        self,
        qubits: Sequence['cirq.Qid'],
        repetitions: int = 1,
        seed: 'cirq.RANDOM_STATE_OR_SEED_LIKE' = None,
    ) -> np.ndarray:
        return self._state.sample(self.get_axes(qubits), repetitions, seed)

    def __repr__(self) -> str:
        return (
            'cirq.ActOnStateVectorArgs('
            f'target_tensor={proper_repr(self.target_tensor)},'
            f' available_buffer={proper_repr(self.available_buffer)},'
            f' qubits={self.qubits!r},'
            f' log_of_measurement_results={proper_repr(self.log_of_measurement_results)})'
        )

    @property
    def target_tensor(self):
        return self._state._state_vector

    @property
    def available_buffer(self):
        return self._state._buffer


def _strat_act_on_state_vector_from_apply_unitary(
    action: Any, args: 'cirq.ActOnStateVectorArgs', qubits: Sequence['cirq.Qid']
) -> bool:
    return True if args._state.apply_unitary(action, args.get_axes(qubits)) else NotImplemented


def _strat_act_on_state_vector_from_mixture(
    action: Any, args: 'cirq.ActOnStateVectorArgs', qubits: Sequence['cirq.Qid']
) -> bool:
    index = args._state.apply_mixture(action, args.get_axes(qubits), args.prng)
    if index is None:
        return NotImplemented
    if protocols.is_measurement(action):
        key = protocols.measurement_key_name(action)
        args._classical_data.record_channel_measurement(key, index)
    return True


def _strat_act_on_state_vector_from_channel(
    action: Any, args: 'cirq.ActOnStateVectorArgs', qubits: Sequence['cirq.Qid']
) -> bool:
    index = args._state.apply_channel(action, args.get_axes(qubits), args.prng)
    if index is None:
        return NotImplemented
    if protocols.is_measurement(action):
        key = protocols.measurement_key_name(action)
        args._classical_data.record_channel_measurement(key, index)
    return True
