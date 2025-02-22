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
from typing import List, Optional

import cirq


@cirq._compat.deprecated_class(
    deadline='v1.0',
    fix='Use cirq.optimize_for_target_gateset and cirq.CZTargetGateset instead.',
)
class ConvertToXmonGates(cirq.PointOptimizer):
    """Attempts to convert strange gates into XmonGates.

    First, checks if the given operation is already a native xmon operation.

    Second, checks if the operation has a known unitary. If so, and the gate
        is a 1-qubit or 2-qubit gate, then performs circuit synthesis of the
        operation.

    Third, attempts to `cirq.decompose` to the operation.

    Fourth, if ignore_failures is set, gives up and returns the gate unchanged.
        Otherwise raises a TypeError.
    """

    def __init__(self, ignore_failures=False) -> None:
        """Inits ConvertToXmonGates.

        Args:
            ignore_failures: If set, gates that fail to convert are forwarded
                unchanged. If not set, conversion failures raise a TypeError.
        """
        super().__init__()
        self.ignore_failures = ignore_failures

    def _convert_one(self, op: cirq.Operation) -> cirq.OP_TREE:
        # Known matrix?
        mat = cirq.unitary(op, None) if len(op.qubits) <= 2 else None
        if mat is not None and len(op.qubits) == 1:
            gates = cirq.single_qubit_matrix_to_phased_x_z(mat)
            return [g.on(op.qubits[0]) for g in gates]
        if mat is not None and len(op.qubits) == 2:
            return cirq.two_qubit_matrix_to_cz_operations(
                op.qubits[0], op.qubits[1], mat, allow_partial_czs=True, clean_operations=False
            )

        return NotImplemented

    def _is_native_xmon_op(self, op: cirq.Operation) -> bool:
        """Check if the gate within an operation is a native xmon gate.

        Args:
            op: Input operation.

        Returns:
            True if the operation is native to the xmon, false otherwise.
        """
        from cirq_google.devices import XmonDevice

        return op.gate is not None and XmonDevice.is_supported_gate(op.gate)

    def convert(self, op: cirq.Operation) -> List[cirq.Operation]:
        def on_stuck_raise(bad):
            return TypeError(
                "Don't know how to work with {!r}. "
                "It isn't a native xmon operation, "
                "a 1 or 2 qubit gate with a known unitary, "
                "or composite.".format(bad)
            )

        return cirq.decompose(
            op,
            keep=self._is_native_xmon_op,
            intercepting_decomposer=self._convert_one,
            on_stuck_raise=None if self.ignore_failures else on_stuck_raise,
        )

    def optimization_at(
        self, circuit: cirq.Circuit, index: int, op: cirq.Operation
    ) -> Optional[cirq.PointOptimizationSummary]:
        if op.gate is None:
            return None

        converted = self.convert(op)
        if len(converted) == 1 and converted[0] is op:
            return None

        return cirq.PointOptimizationSummary(
            clear_span=1, new_operations=converted, clear_qubits=op.qubits
        )
