"""
API for the UI
"""
import abc
import inspect
import logging
from typing import Dict, Iterable, Union, List

# third-party
import functools
from pyomo.environ import ConcreteModel
from pyomo.environ import Block, Var, value
from idaes.core.util import get_solver
import idaes.logger as idaeslog

# Set up logger
_log = idaeslog.getLogger(__name__)

_log.setLevel(logging.DEBUG)


# Utility functions to wrap the 'algorithm' methods in a begin/end logging message


def log_meth(meth):
    @functools.wraps(meth)
    def wrapper(*args, **kwargs):
        name = _get_method_classname(meth)
        _log.debug(f"Begin {name}")
        try:
            result = meth(*args, **kwargs)
        except Exception as e:
            error_msg = f"Error in {name}"
            _log.exception(error_msg)
            raise e
        _log.debug(f"End {name}")
        return result

    return wrapper


def _get_method_classname(m):
    """Get class name for method, assuming method is bound and class has __dict__."""
    for k, v in inspect.getmembers(m):
        if k == "__qualname__":
            return v
    return "<unknown>"

# End logging utility functions

# Interface to get exported variables


class BlockInterface:
    """Interface to a block.
    """
    def __init__(self, block: Block, config: Dict = None):
        self._block = block
        config = config or {}
        self.display_name = config.get("display_name", block.name)
        self.description = config.get("description", block.doc)
        self._variables = config.get("variables", {})

    def get_exported_variables(self) -> Iterable[Var]:
        """"Called by client to get variables exported by the block.
        """
        result = {}
        for name, data in self._variables.items():
            c = data.copy()
            v = getattr(self._block, name)
            if v.is_indexed():
                c["indexed_values"] = {}
                for idx in v.index_set():
                    c["indexed_values"][str(idx)] = value(v[idx])
            else:
                c["value"] = value(v)
            if "display_name" not in c:
                c["display_name"] = v.local_name
            if "description" not in c:
                c["description"] = v.doc
            c["units"] = str(v.get_units())
            result[name] = c
        return result


class FlowsheetInterface(BlockInterface):

    def __init__(self, flowsheet, **kwargs):
        super().__init__(flowsheet, **kwargs)
        self.flowsheet = flowsheet

    @log_meth
    def as_dict(self):
        block_map = self._get_block_map()
        return {"blocks": block_map}

    def _get_block_map(self):
        stack, mapping = [(["fs"], self.flowsheet)], {}
        while stack:
            key, val = stack.pop()
            if hasattr(val, "component_map"):
                for key2, val2 in val.component_map(ctype=Block).items():
                    qkey = key + [key2]
                    self._add_mapping_key(mapping, qkey, val2)
                    stack.append((qkey, val2))
        return mapping

    @staticmethod
    def _add_mapping_key(m, keys, value):
        node_keys, leaf_key = keys[:-1], keys[-1]
        for k in node_keys:
            if k not in m:
                m[k] = {}
            m = m[k]  # descend
        if hasattr(value, "ui"):
            m[leaf_key] = {"variables": value.ui.get_exported_variables()}

# Workflow interface

class Steps:
    setup = "flowsheet_setup"
    build = "perf_build"
    init = "perf_init"
    optimize = "perf_opt"
    build_costing = "cost_build"
    init_costing = "cost_init"
    optimize_costing = "cost_opt"


STEP_NAMES = (
    Steps.setup,
    Steps.build,
    Steps.init,
    Steps.optimize,
    Steps.build_costing,
    Steps.init_costing,
    Steps.optimize_costing,
)
SCHEMA = {k: {} for k in STEP_NAMES}


class WorkflowStep(abc.ABC):
    """Subclasses are an implementation of the Strategy Pattern where the algorithm is, e.g., building
    or initializing or solving the model. For API friendliness, the
    word 'strategy' is replaced with 'action' in names and documentation.
    """

    def __init__(self, workflow: "AnalysisWorkflow", name: str):
        """Constructor."""
        self.workflow = workflow
        self.flowsheet_data = {}
        self.name = name

    @abc.abstractmethod
    def algorithm(self, data: Dict) -> Dict:
        pass

    @staticmethod
    def _flowsheet_data(data):
        return data[AnalysisWorkflow.FLOWSHEET_DATA]


class Build(WorkflowStep):
    @log_meth
    def algorithm(self, data):
        return {"model": self.build_model(data)}

    @abc.abstractmethod
    def build_model(self, data: Dict) -> ConcreteModel:
        pass


class Initialize(WorkflowStep):
    @log_meth
    def algorithm(self, data) -> Dict:
        self.initialize_model(data)
        return {}

    @abc.abstractmethod
    def initialize_model(self, data: Dict) -> None:
        pass


class Optimize(WorkflowStep):
    @log_meth
    def algorithm(self, data):
        return self.solve(data)

    @abc.abstractmethod
    def solve(self, data):
        model = data["model"]
        solver = data["solver"]
        if solver is None:
            solver = get_solver()
        results = solver.solve(model)
        # if check_terminaton:
        #     assert_optimal_termination(results)
        return results


class AnalysisWorkflow:
    """A set of analysis workflow 'steps', each associated with a named chunk of data
    from the global `schema`.
    """

    FLOWSHEET_DATA = "fs_data"

    def __init__(self, has_costing=True) -> None:
        self._has_costing = has_costing
        # information about each step
        self._steps = {
            k: {"data": {}, "action": None, "changed": False, "result": {}}
            for k in STEP_NAMES
        }
        # steps in this workflow
        self._wf = []
        self._set_standard_workflow()

    def _set_standard_workflow(self):
        steps = (Steps.build, Steps.init, Steps.optimize)
        self._set_workflow_steps(steps)

    def _set_workflow_steps(self, step_names: Iterable[str]) -> Iterable[str]:
        seen_names, wf = {}, []
        for name in step_names:
            name = self._normalize_step_name(name)
            if name in seen_names:
                raise KeyError(f"Duplicate step name: {name}")
            seen_names[name] = True
            wf.append(name)
        self._wf = wf
        return wf

    def get_step_input(self, name: str) -> Dict:
        """Set inputs to be used for the step.

        Args:
            name: Name of step

        Returns
            The value previously provided to `set_step_input`, or an empty dict if no value
        """
        name = self._normalize_step_name(name)
        return self._steps[name]["data"]

    def set_step_input(self, name: str, d: Dict):
        """Set input data for a step.

        Args:
            name: Name of step
            d: Input values

        Returns:
            None
        """
        name = self._normalize_step_name(name)
        self._steps[name]["data"] = d
        self._steps[name]["changed"] = True

    def set_flowsheet_data(self, d: Dict):
        """Set flowsheet-level metadata.

        Args:
            d: The metadata

        Returns:
            None
        """
        self._steps[Steps.setup]["data"] = d

    def get_step_result(self, name: str) -> Dict:
        """Get result from a previously executed step ``name``.

        Args:
            name: Name of the step (for which the result is retrieved)

        Return:
            The result of that step (always an empty dictionary if not yet run)
        """
        name = self._normalize_step_name(name)
        return self._steps[name]["result"]

    # some syntactic sugar for common step/result combinations

    @property
    def model(self):
        """Get the model.

        Returns:
            Built model, or None if the build step has not yet been executed
        """
        return self.get_step_result(Steps.build)["model"]

    @property
    def optimize_result(self):
        """Get the result of the ``optimize`` step.
        """
        return self.get_step_result(Steps.optimize)

    # end of syntactic sugar

    def set_step_action(self, name: str, clazz: type, **kwargs):
        """Set the action to use for one of the workflow steps.

        Args:
            name: Name of the workflow step
            clazz: Class of action for this step. This should be a subclass of WorkflowStep.
            kwargs: Additional arguments to initialize the action class

        Raises:
            KeyError: If the step name is invalid

        Returns:
            None
        """
        name = self._normalize_step_name(name)
        obj = clazz(self, name, **kwargs)  # instantiate the step
        self._steps[name]["action"] = obj
        self._steps[name]["input"] = {}
        self._steps[name]["_kwargs"] = kwargs  # just for debugging

    def get_step_action(self, name: str) -> Union[WorkflowStep, None]:
        """Get defined action for step.

        Returns:
            Action

        Raises:
            KeyError if step name is unknown
        """
        name = self._normalize_step_name(name)
        if name not in self._steps:
            raise KeyError(f"Unknown name for step: {name}")
        return self._steps[name]["action"]

    def run_all(self) -> None:
        for step_name in self._wf:
            step = self._steps[step_name]
            self._run_step(step, step_name)

    def run_one(self, name: str) -> Dict:
        """Run a single workflow step."""
        name = self._normalize_step_name(name)
        if name not in self._wf:
            name_list = "->".join(self._wf)
            raise KeyError(
                f"Step name not in current workflow. name={name} workflow={name_list}"
            )
        step = self._steps[name]
        self._run_step(step, name)
        return step["result"]

    def _run_step(self, step: Dict, name: str) -> None:
        action = step["action"]
        if action is None:
            _log.warning(f"No action for step. name={name}")
            return
        action.flowsheet_data = self._steps[Steps.setup]["data"]
        input = step["data"]
        step["result"] = action.algorithm(input)

    def _normalize_step_name(self, name: str) -> str:
        try:
            norm_name = name.lower().strip()
        except AttributeError:
            raise KeyError(f"Step name is not a string")
        if norm_name not in STEP_NAMES:
            name_list = "|".join(STEP_NAMES)
            message = f"Bad step name. input-name={name}, normalized-name={norm_name}, expected={name_list}"
            raise KeyError(message)
        return norm_name



