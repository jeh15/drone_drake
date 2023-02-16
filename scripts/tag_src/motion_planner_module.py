from functools import partial

import numpy as np
import ml_collections

import jax
import jax.numpy as jnp
from jax import (
    jit,
    jacfwd,
    jacrev
)

from pydrake.solvers import mathematicalprogram as mp
from pydrake.common.value import Value
from pydrake.solvers.gurobi import GurobiSolver
from pydrake.solvers.mathematicalprogram import SolverOptions
from pydrake.systems.framework import (
    LeafSystem,
    PublishEvent,
    TriggerType,
    BasicVector_,
)

import pdb


class QuadraticProgram(LeafSystem):
    def __init__(self, config: ml_collections.ConfigDict()):
        LeafSystem.__init__(self)
        # Class Parameters:
        self._UPDATE_RATE = config.motion_planner_rate

        # Mathematical Program Parameters: Takes from Config
        self._nodes = config.nodes
        self._time_horizon = config.time_horizon
        self._state_dimension = config.state_dimension
        self._dt = config.dt
        self._time_vector = np.linspace(0, self._time_horizon, self._nodes)
        self._spline_resolution = config.spline_resolution
        
        # Constants 
        self._mass = 0.027 # Actual Crazyflie Mass
        self._friction = 0.01
        self._tol = 1e-03
        self._solve_flag = 0
        self._area_limit = jnp.array([config.area_bounds, config.area_bounds], dtype=float)

        # State Size for Optimization: (Seems specific to this implementation should not be a config param)
        self._state_size = self._state_dimension * 2
        self._full_size = self._state_dimension * 3
        self._num_state_slack = 2
        self._num_risk_slack = 1
        self._num_slack = self._num_state_slack + self._num_risk_slack
        # Size of design variable vector:
        self._setpoint = jnp.zeros(
            ((self._full_size + self._num_slack) * self._nodes,),
            dtype=float,
        )

        self._state_bounds = jnp.asarray(
            [2.0, 2.0, 0.075],
            dtype=float,
        )

        self._weights = jnp.asarray(
            [100.0, 1.0, 0.01, 1.0],
            dtype=float,
        )

        # self._state_bounds = jnp.asarray(
        #     [2.0, 2.0, 0.1],
        #     dtype=float,
        # )

        self.opt_var_lb = np.concatenate(
            [
                -self._state_bounds[0] * np.ones(self._state_dimension * self._nodes,),
                -self._state_bounds[1] * np.ones(self._state_dimension * self._nodes,),
                -self._state_bounds[2] * np.ones(self._state_dimension * self._nodes,),
                np.zeros(self._num_state_slack * self._nodes,),
                np.NINF * np.ones(self._num_risk_slack * self._nodes,),
            ],
            axis=0,
        )
        self.opt_var_ub = np.concatenate(
            [
                self._state_bounds[0] * np.ones(self._state_dimension * self._nodes,),
                self._state_bounds[1] * np.ones(self._state_dimension * self._nodes,),
                self._state_bounds[2] * np.ones(self._state_dimension * self._nodes,),
                np.Inf * np.ones(self._num_state_slack * self._nodes,),
                np.zeros(self._num_risk_slack * self._nodes,),
            ],
            axis=0,
        )

        # self._weights = jnp.asarray(
        #     [100.0, 4.0, 1.0],
        #     dtype=float,
        # )

        # Initialize for T = 0:
        self._full_state_trajectory = np.zeros(
            (self._full_size * self._nodes,),
            dtype=float,
        )

        self._warm_start = np.zeros(
            ((self._full_size + self._num_slack) * self._nodes,),
            dtype=float,
        )

        # Initialize Abstract States:
        # Output: (x, y, dx, dy, ux, uy)
        output_size = np.zeros((self._full_size * self._nodes,))
        output_state_init = Value[BasicVector_[float]](output_size)
        self.state_index = self.DeclareAbstractState(output_state_init)

        """ Declare Input: """
        # Full State From CrazySwarm: (x, y, z, dx, dy, dz, ddx, ddy, ddz)
        self.initial_condition_input = self.DeclareVectorInputPort(
            "initial_condition",
            9,
        ).get_index()

        # Current Obstacle States from CrazySwarm: (x, y, z, dx, dy, dz)
        self.obstacle_states_input = self.DeclareVectorInputPort(
            "obstacle_states",
            self._full_size,
        ).get_index()

        # Updated Constraints from Learning Framework: (slope, intercept)
        self.constraint_input = self.DeclareVectorInputPort(
            "risk_constraints",
            self._spline_resolution * 2,
        ).get_index()

        """ Declare Output: """
        self.DeclareVectorOutputPort(
            "motion_plan_output",
            np.size(output_size),
            self.output_callback,
            {self.abstract_state_ticket(self.state_index)},
        )

        # Declare Initialization Event:
        def on_initialize(context, event):
            self.build_optimization(context, event)

        self.DeclareInitializationEvent(
            event=PublishEvent(
                trigger_type=TriggerType.kInitialization,
                callback=on_initialize,
            )
        )

        # Declare Update Event: Solve Quadratic Program
        def on_periodic(context, event):
            self.update_qp(context, event)

        self.DeclarePeriodicEvent(
            period_sec=self._UPDATE_RATE,
            offset_sec=0.0,
            event=PublishEvent(
                trigger_type=TriggerType.kPeriodic,
                callback=on_periodic,
                )
            )

    # Output Port Callback:
    def output_callback(self, context, output_motion_plan):
        a_state = context.get_mutable_abstract_state(self.state_index)
        a_value = a_state.get_mutable_value()
        output_motion_plan.SetFromVector(a_value.get_mutable_value())

    # Jax Methods:
    @partial(jit, static_argnums=(0,), static_argnames=['mass', 'friction', 'dt', 'num_states', 'num_slack'])
    def _equality_constraints(
        self,
        q: jax.Array,
        initial_conditions: jax.Array,
        mass: float,
        friction: float,
        dt: float,
        num_states: int,
        num_slack: int,
    ) -> jnp.ndarray:
        print(f"Equality Compiled")
        """
        Helper Functions:
            1. Euler Collocation
        """

        # Euler Collocation:
        def _collocation_constraint(x: jax.Array, dt: float) -> jnp.ndarray:
            collocation = x[0][1:] - x[0][:-1] - x[1][:-1] * dt
            return collocation

        """
        Equality Constraints:
            1. Initial Condition
            2. Collocation Constraint
        """

        # Sort State Vector:
        q = q.reshape((num_states + num_slack, -1))

        # State Nodes:
        x = q[0, :]
        y = q[1, :]
        dx = q[2, :]
        dy = q[3, :]
        ux = q[4, :]
        uy = q[5, :]

        # 1. Initial Condition Constraints:
        initial_condition = jnp.array([
            x[0] - initial_conditions[0],
            y[0] - initial_conditions[1],
            dx[0] - initial_conditions[3],
            dy[0] - initial_conditions[4],
            ux[0] - initial_conditions[6],
            uy[0] - initial_conditions[7],
        ], dtype=float)

        # 2. Collocation Constraints:
        ddx = (ux - friction * dx) / mass
        ddy = (uy - friction * dy) / mass
        x_defect = _collocation_constraint([x, dx], dt)
        y_defect = _collocation_constraint([y, dy], dt)
        dx_defect = _collocation_constraint([dx, ddx], dt)
        dy_defect = _collocation_constraint([dy, ddy], dt)

        equality_constraint = jnp.concatenate(
            [
                initial_condition,
                x_defect,
                y_defect,
                dx_defect,
                dy_defect,
            ]
        )

        return equality_constraint

    @partial(jit, static_argnums=(0,), static_argnames=['num_states', 'num_slack'])
    def _inequality_constraints(
        self,
        q: jax.Array,
        obstacle_trajectory: jax.Array,
        halfspace_vector: jax.Array,
        slope: jax.Array,
        intercept: jax.Array,
        position_limit: jax.Array,
        num_states: int,
        num_slack: int,
    ) -> jnp.ndarray:
        print(f"Inequality Compiled")
        """
        Inquality Constraints:
            1. Area Boundary Constraint
            2. Risk Constraints
        """

        # Sort State Vector:
        q = q.reshape((num_states + num_slack, -1))

        # State Nodes:
        states_position = q[:2, :]

        # Position Slack Variables:
        slack_position = q[6:8, :]

        # Risk Slack Variable:
        slack_variable = q[-1, :]

        # Area Boundary Constraint
        area_bound = jnp.vstack(
            [
                [-states_position - position_limit.reshape(-1, 1) - slack_position],
                [states_position - position_limit.reshape(-1, 1) - slack_position],
            ],
        ).flatten()

        # 2. Risk Constraints:
        relative_distance = obstacle_trajectory - states_position
        linearized_distance = (jnp.einsum('ij,ij->j', relative_distance, halfspace_vector) / jnp.einsum('ij,ij->j', halfspace_vector, halfspace_vector)) * jnp.linalg.norm(halfspace_vector, axis=0)
        # rfun[i, j] = s[j] - (m[i] * x[j] + b[i])
        rfun = (
            -((jnp.einsum('i,j->ij', slope, linearized_distance)) + intercept.reshape(intercept.shape[0], -1)) + slack_variable
        ).flatten()

        # Concatenate the constraints:
        inequality_constraints = jnp.concatenate(
            [
                area_bound,
                rfun,
            ],
            axis=0,
        )

        return inequality_constraints

    @partial(jit, static_argnums=(0,), static_argnames=['num_states', 'num_slack'])
    def _objective_function(
        self,
        q: jax.Array,
        w: jax.Array,
        num_states: int,
        num_slack: int,
    ) -> jnp.ndarray:
        print(f"Objective Compiled")
        """
        Objective Function:
            1. State Area Bound
            2. Control Effort Objective
            3. Risk Minimization
        """

        # Sort State Vector:
        q = q.reshape((num_states + num_slack, -1))

        # State Nodes:
        states_position = q[:2, :]
        states_velocity = q[2:4, :]
        states_control = q[4:6, :]

        # Position Slack Variables:
        slack_position = q[6:8, :]

        # Risk Slack Variable:
        slack_variable = q[-1, :]

        minimize_slack_position = w[0] * jnp.sum(slack_position ** 2, axis=0)
        minimize_acceleration_effort = w[1] * jnp.sum(states_control ** 2, axis=0)
        minimize_velocity_effort = w[2] * jnp.sum(states_velocity ** 2, axis=0)
        minimize_failure = -w[3] * jnp.sum(slack_variable, axis=0)
        objective_function = jnp.sum(minimize_slack_position + minimize_acceleration_effort + minimize_velocity_effort + minimize_failure, axis=0)

        return objective_function

    def build_optimization(self, context, event):
        # Get Input Port Values and Convert to Jax Array:
        initial_conditions = self.get_input_port(self.initial_condition_input).Eval(context)
        # Convert Acceleration to Control Input:
        initial_conditions[-3] = self.compute_control(
            ddq=initial_conditions[-3],
            dq=initial_conditions[3],
            limit=self._state_bounds[2].astype(float),
        )
        initial_conditions[-2] = self.compute_control(
            ddq=initial_conditions[-2],
            dq=initial_conditions[4],
            limit=self._state_bounds[2].astype(float),
        )
        initial_conditions = np.asarray(initial_conditions)

        # Update Risk Constraints:
        risk_constraints = np.asarray(
            self.get_input_port(self.constraint_input).Eval(context)
        ).reshape((self._spline_resolution, -1))

        # Get Values for Halfspace-Constraint:
        previous_trajectory = np.reshape(self._warm_start, (-1, self._nodes))[:self._state_dimension, :]
        obstacle_states = np.asarray(self.get_input_port(self.obstacle_states_input).Eval(context))
        # This needs to be generalized depending on the model dimenisions used:
        obstacle_trajectory = np.einsum('i,j->ij', obstacle_states[3:5], self._time_vector) \
            + obstacle_states[:2].reshape((2, -1))
        halfspace_vector = obstacle_trajectory - previous_trajectory

        # Isolate Functions to Lambda Functions and wrap them in staticmethod:
        self.equality_func = lambda x, ic: self._equality_constraints(
            q=x,
            initial_conditions=ic,
            mass=self._mass,
            friction=self._friction,
            dt=self._dt,
            num_states=self._full_size,
            num_slack=self._num_slack,
        )

        self.inequality_func = lambda x, y, n, m, b: self._inequality_constraints(
            q=x,
            obstacle_trajectory=y,
            halfspace_vector=n,
            slope=m,
            intercept=b,
            position_limit=self._area_limit,
            num_states=self._full_size,
            num_slack=self._num_slack,
        )

        self.objective_func = lambda x: self._objective_function(
            q=x,
            w=self._weights,
            num_states=self._full_size,
            num_slack=self._num_slack,
        )

        # Compute A and b matricies for equality constraints:
        self._A_eq_fn = jax.jit(jacfwd(self.equality_func))
        A_eq = self._A_eq_fn(self._setpoint, initial_conditions)
        b_eq = -self.equality_func(self._setpoint, initial_conditions)

        # Compute A and b matricies for inequality constraints:
        self._A_ineq_fn = jax.jit(jacfwd(self.inequality_func))
        A_ineq = self._A_ineq_fn(
            self._setpoint,
            obstacle_trajectory,
            halfspace_vector,
            risk_constraints[:, 0],
            risk_constraints[:, 1],
        )
        b_ineq_ub = -self.inequality_func(
            self._setpoint,
            obstacle_trajectory,
            halfspace_vector,
            risk_constraints[:, 0],
            risk_constraints[:, 1],
        )

        # Inequality Constraints: change A*x <= b -> lb < A*x < ub:
        b_ineq_lb = np.copy(-b_ineq_ub)
        b_ineq_lb[:] = np.NINF

        # Compute H and f matrcies for objective function:
        self._H_fn = jax.jit(jacfwd(jacrev(self.objective_func)))
        self._f_fn = jax.jit(jacfwd(self.objective_func))
        H = self._H_fn(self._setpoint)
        f = self._f_fn(self._setpoint)

        # Construct gurobi Problem:
        self.prog = mp.MathematicalProgram()

        # State and Control Input Variables:
        self.opt_vars = self.prog.NewContinuousVariables((self._full_size + self._num_slack) * self._nodes, "x")

        # Optimization Variable Bounds:
        self.prog.AddBoundingBoxConstraint(
            self.opt_var_lb,
            self.opt_var_ub,
            self.opt_vars,
        )

        # Add Constraints and Objective Function:
        self.equality_constraint = self.prog.AddLinearConstraint(
            A=A_eq,
            lb=b_eq,
            ub=b_eq,
            vars=self.opt_vars,
        )

        self.inequality_constraint = self.prog.AddLinearConstraint(
            A=A_ineq,
            lb=b_ineq_lb,
            ub=b_ineq_ub,
            vars=self.opt_vars,
        )

        self.objective_function = self.prog.AddQuadraticCost(
            Q=H,
            b=f,
            vars=self.opt_vars,
            is_convex=True,
        )

        # Solve the program:
        """gurobi:"""
        self.gurobi = GurobiSolver()
        self.solver_options = SolverOptions()
        self.solver_options.SetOption(self.gurobi.solver_id(), "BarConvTol", 1e-6)
        self.solver_options.SetOption(self.gurobi.solver_id(), "FeasibilityTol", 1e-6)
        self.solver_options.SetOption(self.gurobi.solver_id(), "OptimalityTol", 1e-8)

        self.solution = self.gurobi.Solve(
            self.prog,
            self._warm_start,
            self.solver_options,
        )

        # Parse Solution:
        self.parse_solution(context, event)

    def update_qp(self, context, event):
        # Get Input Port Values and Convert to Jax Array:
        initial_conditions = self.get_input_port(self.initial_condition_input).Eval(context)
        # Convert Acceleration to Control Input:
        initial_conditions[-3] = self.compute_control(
            ddq=initial_conditions[-3],
            dq=initial_conditions[3],
            limit=self._state_bounds[2].astype(float),
        )
        initial_conditions[-2] = self.compute_control(
            ddq=initial_conditions[-2],
            dq=initial_conditions[4],
            limit=self._state_bounds[2].astype(float),
        )
        initial_conditions = np.asarray(initial_conditions)

        # print(f"Velocity Initial Conditions: {initial_conditions[3:6]}")
        # print(f"Control Input Initial Conditions: {initial_conditions[6:9]}")

        # Update Risk Constraints:
        risk_constraints = np.asarray(
            self.get_input_port(self.constraint_input).Eval(context)
        ).reshape((self._spline_resolution, -1))

        # Get Values for Halfspace-Constraint:
        previous_trajectory = np.reshape(self._warm_start, (-1, self._nodes))[:self._state_dimension, :]
        obstacle_states = np.asarray(self.get_input_port(self.obstacle_states_input).Eval(context))
        # This needs to be generalized depending on the model dimenisions used:
        obstacle_trajectory = np.einsum('i,j->ij', obstacle_states[3:5], self._time_vector) \
            + obstacle_states[:2].reshape((2, -1))
        halfspace_vector = obstacle_trajectory - previous_trajectory

        # Compute A and b matricies for equality constraints:
        A_eq = self._A_eq_fn(self._setpoint, initial_conditions)
        b_eq = -self.equality_func(self._setpoint, initial_conditions)

        # Compute A and b matricies for inequality constraints:
        A_ineq = self._A_ineq_fn(
            self._setpoint,
            obstacle_trajectory,
            halfspace_vector,
            risk_constraints[:, 0],
            risk_constraints[:, 1],
        )
        b_ineq_ub = -self.inequality_func(
            self._setpoint,
            obstacle_trajectory,
            halfspace_vector,
            risk_constraints[:, 0],
            risk_constraints[:, 1],
        )

        # Inequality Constraints: change A*x <= b -> lb < A*x < ub:
        b_ineq_lb = np.copy(-b_ineq_ub)
        b_ineq_lb[:] = np.NINF

        # Compute H and f matrcies for objective function:
        H = self._H_fn(self._setpoint)
        f = self._f_fn(self._setpoint)

        # Update Constraint and Objective Function:
        self.equality_constraint.evaluator().UpdateCoefficients(
            new_A=A_eq,
            new_lb=b_eq,
            new_ub=b_eq,
        )
        self.equality_constraint.evaluator().RemoveTinyCoefficient(self._tol)

        self.inequality_constraint.evaluator().UpdateCoefficients(
            new_A=A_ineq,
            new_lb=b_ineq_lb,
            new_ub=b_ineq_ub,
        )
        self.inequality_constraint.evaluator().RemoveTinyCoefficient(self._tol)

        self.objective_function.evaluator().UpdateCoefficients(
            new_Q=H,
            new_b=f,
        )

        # Solve Updated Optimization:
        self.solution = self.gurobi.Solve(
            self.prog,
            self._warm_start,
            self.solver_options,
        )

        # Visibility for Debug/Logging:
        self._optimizer_time = self.solution.get_solver_details().optimizer_time

        # Parse Solution:
        self.parse_solution(context, event)

    # Helper Functions:
    def compute_control(self, ddq: float, dq: float, limit: float) -> float:
        def convert_to_control(self, ddq: float, dq: float) -> float:
            return (ddq * self._mass + self._friction * dq)
        u = convert_to_control(self, ddq, dq)
        return (u if np.abs(u) < limit else np.sign(u) * limit)

    def compute_acceleration(self, u: np.ndarray, dx: np.ndarray) -> np.ndarray:
        return np.asarray([(u[:] - self._friction * dx[:]) / self._mass]).flatten()

    def parse_solution(self, context, event):
        if not self.solution.is_success():
            print(f"Optimization did not solve!")
            print(f"Solver Status: {self.solution.get_solver_details().optimization_status}")
            print(f"Objective Function Convex: {self.objective_function.evaluator().is_convex()}")
            pdb.set_trace()

        opt_sol = np.reshape(self.solution.GetSolution(self.opt_vars), (self._full_size + self._num_slack, -1))
        
        x_sol = opt_sol[0, :]
        y_sol = opt_sol[1, :]
        dx_sol = opt_sol[2, :]
        dy_sol = opt_sol[3, :]
        ux_sol = opt_sol[4, :]
        uy_sol = opt_sol[5, :]
        sx_sol = opt_sol[6, :]
        sy_sol = opt_sol[7, :]
        s_sol = opt_sol[8, :]

        ddx = self.compute_acceleration(ux_sol, dx_sol)
        ddy = self.compute_acceleration(uy_sol, dy_sol)

        self._full_state_trajectory = np.concatenate(
            [
                x_sol, y_sol,
                dx_sol, dy_sol,
                ddx, ddy
            ],
            axis=0,
        )

        self._warm_start = np.concatenate(
            [
                x_sol, y_sol,
                dx_sol, dy_sol,
                ux_sol, uy_sol,
                sx_sol, sy_sol,
                s_sol,
            ], 
            axis=0,
        )

        # How can I clean this up?
        a_state = context.get_mutable_abstract_state(self.state_index)
        a_state.set_value(self._full_state_trajectory)
