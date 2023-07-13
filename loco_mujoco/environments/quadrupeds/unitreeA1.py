import os

import numpy as np
from dm_control import mjcf

from pathlib import Path

from mushroom_rl.utils.running_stats import *
from mushroom_rl.utils.mujoco import *

import loco_mujoco
from loco_mujoco.environments import ValidTaskConf
from loco_mujoco.environments import LocoEnv
from loco_mujoco.utils.reward import VelocityVectorReward
from loco_mujoco.utils.math import rotate_obs
from loco_mujoco.utils.goals import GoalDirectionVelocity
from loco_mujoco.utils.math import mat2angle_xy, angle2mat_xy, transform_angle_2pi
from loco_mujoco.utils.checks import check_validity_task_mode_dataset


class UnitreeA1(LocoEnv):
    """
    Mujoco simulation of Unitree A1 model.

    """

    valid_task_confs = ValidTaskConf(tasks=["simple", "hard"],
                                     data_types=["real"])

    def __init__(self, use_torque_ctrl=True, setup_random_rot=False, tmp_dir_name=None,
                 default_target_velocity=0.5, camera_params=None, **kwargs):
        """
        Constructor.

        Args:
            use_torque_ctrl (bool): If True, the Unitree uses torque control, else position control;
            setup_random_rot (bool): If True, the robot is initialized with a random rotation;
            tmp_dir_name (str): Specifies a name of a directory to which temporary files are
                written, if created. By default, temporary directory names are created automatically.
            default_target_velocity (float): Default target velocity set in the goal, when no trajectory
                data is provided.
            camera_params (dict): Dictionary defining some of the camera parameters for visualization.

        """

        # Choose xml file (either for torque or position control)
        if use_torque_ctrl:
            xml_path = (Path(__file__).resolve().parent.parent / "data" / "quadrupeds" /
                        "unitree_a1_torque.xml").as_posix()
        else:
            xml_path = (Path(__file__).resolve().parent.parent / "data" / "quadrupeds" /
                        "unitree_a1_position.xml").as_posix()

        action_spec = self._get_action_specification()

        observation_spec = self._get_observation_specification()

        collision_groups = [("floor", ["floor"]),
                            ("foot_FR", ["FR_foot"]),
                            ("foot_FL", ["FL_foot"]),
                            ("foot_RR", ["RR_foot"]),
                            ("foot_RL", ["RL_foot"])]

        # append observation_spec with the direction arrow and add it to the xml file
        observation_spec.append(("dir_arrow", "dir_arrow", ObservationType.SITE_ROT))
        xml_handle = self._add_dir_vector_to_xml_handle(mjcf.from_path(xml_path))
        xml_path = self._save_xml_handle(xml_handle, tmp_dir_name)

        self.setup_random_rot = setup_random_rot

        # setup goal including the desired direction and velocity
        self._goal = GoalDirectionVelocity()
        self._goal.set_goal(0.0, default_target_velocity)

        if camera_params is None:
            # make the camera by default a bit higher
            camera_params = dict(follow=dict(distance=3.5, elevation=-20.0, azimuth=90.0))
        super().__init__(xml_path, action_spec, observation_spec,  collision_groups,
                         camera_params=camera_params, **kwargs)

    def setup(self, obs):
        """
        Function to setup the initial state of the simulation. Initialization can be done either
        randomly, from a certain initial, or from the default initial state of the model.

        Args:
            obs (np.array): Observation to initialize the environment from;

        """

        self._reward_function.reset_state()

        if obs is not None:
            self._init_sim_from_obs(obs)
        else:
            if not self.trajectories and self._random_start:
                raise ValueError("Random start not possible without trajectory data.")
            elif not self.trajectories and self._init_step_no is not None:
                raise ValueError("Setting an initial step is not possible without trajectory data.")
            elif self._init_step_no is not None and self._random_start:
                raise ValueError("Either use a random start or set an initial step, not both.")

            if self.trajectories is not None:
                if self._random_start:
                    sample = self.trajectories.reset_trajectory()
                    if self.setup_random_rot:
                        angle = np.random.uniform(0, 2 * np.pi)
                        sample = rotate_obs(sample, angle,  *self._get_relevant_idx_rotation())
                elif self._init_step_no:
                    traj_len = self.trajectories.trajectory_length
                    n_traj = self.trajectories.nnumber_of_trajectories
                    assert self._init_step_no <= traj_len * n_traj
                    substep_no = int(self._init_step_no % traj_len)
                    traj_no = int(self._init_step_no / traj_len)
                    sample = self.trajectories.reset_trajectory(substep_no, traj_no)
                else:
                    # sample random trajectory and use the first sample
                    sample = self.trajectories.reset_trajectory(substep_no=0)
                    if self.setup_random_rot:
                        angle = np.random.uniform(0, 2 * np.pi)
                        sample = rotate_obs(sample, angle,  *self._get_relevant_idx_rotation())

                # set the goal
                rot_mat = self.trajectories.get_from_sample(sample, "dir_arrow")
                angle = mat2angle_xy(rot_mat)
                desired_vel = self.trajectories.get_from_sample(sample, "goal_speed")
                self._goal.set_goal(angle, desired_vel)

                # set the state of the simulation
                self.set_sim_state(sample)

    def set_sim_state(self, sample):
        """
        Sets the state of the simulation according to an observation.

        Args:
            sample (list or np.array): Sample used to set the state of the simulation.

        """

        # set simulation state
        sample = sample[:-1]    # remove goal velocity
        super(UnitreeA1, self).set_sim_state(sample)

    def create_dataset(self, ignore_keys=None):
        """
        Creates a dataset from the specified trajectories.

        Args:
            ignore_keys (list): List of keys to ignore in the dataset.

        Returns:
            Dictionary containing states, next_states and absorbing flags. For the states the shape is
            (N_traj x N_samples_per_traj, dim_state), while the absorbing flag has the shape is
            (N_traj x N_samples_per_traj).

        """

        if ignore_keys is None:
            ignore_keys = ["q_trunk_tx", "q_trunk_ty"]

        if self.trajectories is not None:
            rot_mat_idx = self._get_idx("dir_arrow")
            state_callback_params = dict(rot_mat_idx=rot_mat_idx, goal_velocity_idx=self._goal_velocity_idx)
            dataset = self.trajectories.create_dataset(ignore_keys=ignore_keys,
                                                       state_callback=self._modify_observation_callback,
                                                       state_callback_params=state_callback_params)
            # check that all state in the dataset satisfy the has fallen method.
            for state in dataset["states"]:
                has_fallen, msg = self._has_fallen(state, return_err_msg=True)
                if has_fallen:
                    err_msg = "Some of the states in the created dataset are terminal states. " \
                              "This should not happen.\n\nViolations:\n"
                    err_msg += msg
                    raise ValueError(err_msg)
        else:
            raise ValueError("No trajectory was passed to the environment. "
                             "To create a dataset pass a trajectory first.")

        return dataset


    def get_kinematic_obs_mask(self):
        """
        Returns a mask (np.array) for the observation specified in observation_spec (or part of it).

        """

        return np.arange(len(self.obs_helper.observation_spec)) # -2 (for x,y ) + 1 (for sin_cos_dir) + 1 (for goal_vel)

    def _get_observation_space(self):
        """
        Returns a tuple of the lows and highs (np.array) of the observation space.

        """
        dir_arrow_idx = self._get_idx("dir_arrow")
        sim_low, sim_high = (self.info.observation_space.low[2:],
                             self.info.observation_space.high[2:])
        sin_cos_angle_low = [-1, -1]
        sin_cos_angle_high = [1, 1]
        goal_vel_low = -np.inf
        goal_vel_high = np.inf
        sim_low = np.concatenate([sim_low[:dir_arrow_idx[0]], sin_cos_angle_low, [goal_vel_low]])
        sim_high = np.concatenate([sim_high[:dir_arrow_idx[0]], sin_cos_angle_high, [goal_vel_high]])

        if self._use_foot_forces:
            grf_low, grf_high = (-np.ones((12,)) * np.inf,
                                 np.ones((12,)) * np.inf)
            return (np.concatenate([sim_low, grf_low]),
                    np.concatenate([sim_high, grf_high]))
        else:
            return sim_low, sim_high

    def _simulation_post_step(self):
        """
        Sets the correct rotation of the goal arrow and the calculates the
        statistics of the ground forces if required. This function is
        called after each step.

        """

        self._set_goal_arrow()
        super()._simulation_post_step()

    def _create_observation(self, obs):
        """
        Creates a full vector of observations.

        Args:
            obs (np.array): Observation vector to be modified or extended;

        Returns:
            New observation vector (np.array);

        """

        if self._use_foot_forces:
            obs = np.concatenate([obs[2:],
                                  [self._goal.get_velocity()],
                                  self.mean_grf.mean / 1000.,
                                  ]).flatten()
        else:
            obs = np.concatenate([obs[2:],
                                  [self._goal.get_velocity()]
                                  ]).flatten()

        return obs

    def _modify_observation(self, obs):
        """
        Transforms the rotation matrix from obs to a sin-cos feature.

        Args:
            obs (np.array): Generated observation.

        Returns:
            The final environment observation for the agent.

        """

        rot_mat_idx = self._get_idx("dir_arrow")

        return self._modify_observation_callback(obs, rot_mat_idx, self._goal_velocity_idx)

    def _get_reward_function(self, reward_type, reward_params):
        """
        Constructs a reward function.

        Args:
            reward_type (string): Name of the reward.
            reward_params (dict): Parameters of the reward function.

        Returns:
            Reward function.

        """

        if reward_type == "velocity_vector":
            x_vel_idx = self.get_obs_idx("dq_trunk_tx")[0]
            y_vel_idx = self.get_obs_idx("dq_trunk_ty")[0]
            rot_mat_idx = self.get_obs_idx("dir_arrow")
            goal_vel_idx = self._goal_velocity_idx
            goal_reward_func = VelocityVectorReward(x_vel_idx=x_vel_idx, y_vel_idx=y_vel_idx,
                                                    rot_mat_idx=rot_mat_idx, goal_vel_idx=goal_vel_idx)
        else:
            goal_reward_func = super()._get_reward_function(reward_type, reward_params)

        return goal_reward_func

    def _has_fallen(self, obs, return_err_msg=False):
        """
        Checks if a model has fallen.

        Args:
            obs (np.array): Current observation.
            return_err_msg (bool): If True, an error message with violations is returned.

        Returns:
            True, if the model has fallen for the current observation, False otherwise.
            Optionally an error message is returned.

        """

        trunk_euler = self._get_from_obs(obs, ["q_trunk_list", "q_trunk_tilt"])
        trunk_height = self._get_from_obs(obs, ["q_trunk_tz"])

        trunk_list_condition = (trunk_euler[0] < -0.2793) or (trunk_euler[0] > 0.2793)
        trunk_tilt_condition = (trunk_euler[1] < -0.192) or (trunk_euler[1] > 0.192)
        trunk_height_condition = trunk_height[0] < -.24
        trunk_condition = (trunk_list_condition or trunk_tilt_condition or trunk_height_condition)

        if return_err_msg:
            error_msg = ""
            if trunk_list_condition:
                error_msg += "trunk_list_condition violated.\n"
            elif trunk_tilt_condition:
                error_msg += "trunk_tilt_condition violated.\n"
            elif trunk_height_condition:
                error_msg += "trunk_height_condition violated. %f \n" % trunk_height

            return trunk_condition, error_msg
        else:
            return trunk_condition

    def _get_relevant_idx_rotation(self):
        """
        Returns the indices relevant for rotating the observation space
        around the vertical axis.

        """

        keys = self.obs_helper.get_all_observation_keys()
        idx_rot = keys.index("q_trunk_rotation")
        idx_xvel = keys.index("dq_trunk_tx")
        idx_yvel = keys.index("dq_trunk_ty")
        return idx_rot, idx_xvel, idx_yvel

    def _get_ground_forces(self):
        """
        Returns the ground forces (np.array).

        """

        grf = np.concatenate([self._get_collision_force("floor", "foot_FL")[:3],
                              self._get_collision_force("floor", "foot_FR")[:3],
                              self._get_collision_force("floor", "foot_RL")[:3],
                              self._get_collision_force("floor", "foot_RR")[:3]])

        return grf

    def _set_goal_arrow(self):
        """
        Sets the rotation of the goal arrow based on the current trunk
        rotation angle and the current goal direction.

        """

        trunk_rotation = self._data.joint("trunk_rotation").qpos[0]
        desired_angle = self._goal.get_direction() + trunk_rotation
        rot_mat = angle2mat_xy(desired_angle)

        # and set the rotation of the cylinder
        self._data.site("dir_arrow").xmat = rot_mat.reshape((9,))

        # calc position of the ball corresponding to the arrow
        self._data.site("dir_arrow_ball").xpos = self._data.body("dir_arrow").xpos + \
                                                 [-0.1 * np.cos(desired_angle), -0.1 * np.sin(desired_angle), 0]

    def _init_sim_from_obs(self, obs):
        """
        Initializes the simulation from an observation.

        Args:
            obs (np.array): The observation to set the simulation state to.

        """
        raise TypeError("Initializing from observation is currently not supported in this environment. ")

    def _get_interpolate_map_params(self):
        """
        Returns all parameters needed to do the interpolation mapping for the respective environment.

        """
        keys = self.get_all_observation_keys()
        rot_mat_idx = keys.index("dir_arrow")
        trunk_rot_idx = keys.index("q_trunk_rotation")
        trunk_list_idx = keys.index("q_trunk_list")
        trunk_tilt_idx = keys.index("q_trunk_tilt")

        return dict(rot_mat_idx=rot_mat_idx, trunk_orientation_idx=[trunk_rot_idx, trunk_list_idx, trunk_tilt_idx])

    def _get_interpolate_remap_params(self):
        """
        Returns all parameters needed to do the interpolation remapping for the respective environment.

        """
        keys = self.get_all_observation_keys()
        angle_idx = keys.index("dir_arrow")
        trunk_rot_idx = keys.index("q_trunk_rotation")
        trunk_list_idx = keys.index("q_trunk_list")
        trunk_tilt_idx = keys.index("q_trunk_tilt")

        return dict(angle_idx=angle_idx, trunk_orientation_idx=[trunk_rot_idx, trunk_list_idx, trunk_tilt_idx])


    @staticmethod
    def generate(task="simple", dataset_type="real", gamma=0.99, horizon=1000, use_foot_forces=False,
                 random_start=True, init_step_no=None):
        """
        Returns a Unitree environment corresponding to the specified task.

        Args:
            task (str): Main task to solve. Either "simple" or "hard". "simple" is a straight walking
                task. "hard" is a walking task in 8 direction. This makes this environment goal conditioned.
            dataset_type (str): "real" or "perfect". "real" uses real motion capture data as the
                reference trajectory. This data does not perfectly match the kinematics
                and dynamics of this environment, hence it is more challenging. "perfect" uses
                a perfect dataset.
            gamma (float): Discounting parameter of the environment.
            horizon (int): Horizon of the environment.
            use_foot_forces (bool): If True, foot forces are added to the observation space.
            random_start (bool): If True, a random sample from the trajectories
                is chosen at the beginning of each time step and initializes the
                simulation according to that.
            init_step_no (int): If set, the respective sample from the trajectories
                is taken to initialize the simulation.

        Returns:
            An MDP of the Unitree A1 Robot.

        """
        check_validity_task_mode_dataset(UnitreeA1.__name__, task, None, dataset_type,
                                         *UnitreeA1.valid_task_confs.get_all())

        # Generate the MDP
        # todo: once the trajectory is learned without random init rotation, activate the latter.
        if task == "simple":
            mdp = UnitreeA1(gamma=gamma, horizon=horizon, use_foot_forces=use_foot_forces, random_start=random_start,
                            init_step_no=init_step_no, use_torque_ctrl=True, setup_random_rot=False,
                            reward_type="velocity_vector")
            traj_path = Path(loco_mujoco.__file__).resolve().parent.parent /\
                        "datasets/quadrupeds/walk_straight.npz"
        elif task == "hard":
            mdp = UnitreeA1(gamma=gamma, horizon=horizon, use_foot_forces=use_foot_forces, random_start=random_start,
                            init_step_no=init_step_no, use_torque_ctrl=True, setup_random_rot=False,
                            reward_type="velocity_vector")
            traj_path = Path(loco_mujoco.__file__).resolve().parent.parent /\
                        "datasets/quadrupeds/walk_8_dir.npz"

        # Load the trajectory
        env_freq = 1 / mdp._timestep  # hz
        desired_contr_freq = 1 / mdp.dt  # hz
        n_substeps = env_freq // desired_contr_freq

        if dataset_type == "real":
            traj_data_freq = 500  # hz
            traj_params = dict(traj_path=traj_path,
                               traj_dt=(1 / traj_data_freq),
                               control_dt=(1 / desired_contr_freq))
        elif dataset_type == "perfect":
            # todo: generate and add this dataset
            raise ValueError(f"currently not implemented.")

        mdp.load_trajectory(traj_params)

        return mdp

    @property
    def _goal_velocity_idx(self):
        """
        The goal speed is not in the observation helper, but only in the trajectory. This is a workaround
        to access it anyways.

        """
        return 43

    @staticmethod
    def _modify_observation_callback(obs, rot_mat_idx, goal_velocity_idx):
        """
        Transforms the rotation matrix from obs to a sin-cos feature.

        Args:
            obs (np.array): Generated observation.
            rot_mat_idx (int): Index of the beginning rotation matrix in the observation.
            goal_velocity_idx (int): Index of the goal speed in the observation.

        Returns:
            The final environment observation for the agent.

        """

        rot_mat = obs[rot_mat_idx].reshape((3, 3))
        # convert mat to angle
        angle = mat2angle_xy(rot_mat)
        # transform the angle to be in [-pi, pi] todo: this is not needed anymore when doing sin cos transformation.
        angle = transform_angle_2pi(angle)
        # make sin-cos transformation
        angle = np.array([np.cos(angle), np.sin(angle)])

        # get goal velocity
        goal_velocity = obs[goal_velocity_idx]

        # concatenate everything to new obs
        new_obs = np.concatenate([obs[:rot_mat_idx[0]], angle, [goal_velocity]])

        return new_obs

    @staticmethod
    def _add_dir_vector_to_xml_handle(xml_handle):
        """
        Adds a direction vector to the Mujoco XML visualizing the goal direction.

        Args:
            xml_handle: Handle to Mujoco XML.

        Returns:
            Modified Mujoco XML handle.

        """

        # find trunk and attach direction arrow
        trunk = xml_handle.find("body", "trunk")
        trunk.add("body", name="dir_arrow", pos="0 0 0.15")
        dir_vec = xml_handle.find("body", "dir_arrow")
        # todo: once Mujoco support cones, make an actual arrow (its requested feature).
        dir_vec.add("site", name="dir_arrow_ball", type="sphere", size=".03", pos="-.1 0 0")
        dir_vec.add("site", name="dir_arrow", type="cylinder", size=".01", fromto="-.1 0 0 .1 0 0")

        return xml_handle

    @staticmethod
    def _get_observation_specification():
        """
        Getter for the observation space specification.

        Returns:
            A list of tuples containing the specification of each observation
            space entry.

        """

        observation_spec = [
            # ------------------- JOINT POS -------------------
            # --- Trunk ---
            ("q_trunk_tx", "trunk_tx", ObservationType.JOINT_POS),
            ("q_trunk_ty", "trunk_ty", ObservationType.JOINT_POS),
            ("q_trunk_tz", "trunk_tz", ObservationType.JOINT_POS),
            ("q_trunk_rotation", "trunk_rotation", ObservationType.JOINT_POS),
            ("q_trunk_list", "trunk_list", ObservationType.JOINT_POS),
            ("q_trunk_tilt", "trunk_tilt", ObservationType.JOINT_POS),
            # --- Front ---
            ("q_FR_hip_joint", "FR_hip_joint", ObservationType.JOINT_POS),
            ("q_FR_thigh_joint", "FR_thigh_joint", ObservationType.JOINT_POS),
            ("q_FR_calf_joint", "FR_calf_joint", ObservationType.JOINT_POS),
            ("q_FL_hip_joint", "FL_hip_joint", ObservationType.JOINT_POS),
            ("q_FL_thigh_joint", "FL_thigh_joint", ObservationType.JOINT_POS),
            ("q_FL_calf_joint", "FL_calf_joint", ObservationType.JOINT_POS),
            # --- Rear ---
            ("q_RR_hip_joint", "RR_hip_joint", ObservationType.JOINT_POS),
            ("q_RR_thigh_joint", "RR_thigh_joint", ObservationType.JOINT_POS),
            ("q_RR_calf_joint", "RR_calf_joint", ObservationType.JOINT_POS),
            ("q_RL_hip_joint", "RL_hip_joint", ObservationType.JOINT_POS),
            ("q_RL_thigh_joint", "RL_thigh_joint", ObservationType.JOINT_POS),
            ("q_RL_calf_joint", "RL_calf_joint", ObservationType.JOINT_POS),
            # ------------------- JOINT VEL -------------------
            # --- Trunk ---
            ("dq_trunk_tx", "trunk_tx", ObservationType.JOINT_VEL),
            ("dq_trunk_ty", "trunk_ty", ObservationType.JOINT_VEL),
            ("dq_trunk_tz", "trunk_tz", ObservationType.JOINT_VEL),
            ("dq_trunk_rotation", "trunk_rotation", ObservationType.JOINT_VEL),
            ("dq_trunk_list", "trunk_list", ObservationType.JOINT_VEL),
            ("dq_trunk_tilt", "trunk_tilt", ObservationType.JOINT_VEL),
            # --- Front ---
            ("dq_FR_hip_joint", "FR_hip_joint", ObservationType.JOINT_VEL),
            ("dq_FR_thigh_joint", "FR_thigh_joint", ObservationType.JOINT_VEL),
            ("dq_FR_calf_joint", "FR_calf_joint", ObservationType.JOINT_VEL),
            ("dq_FL_hip_joint", "FL_hip_joint", ObservationType.JOINT_VEL),
            ("dq_FL_thigh_joint", "FL_thigh_joint", ObservationType.JOINT_VEL),
            ("dq_FL_calf_joint", "FL_calf_joint", ObservationType.JOINT_VEL),
            # --- Rear ---
            ("dq_RR_hip_joint", "RR_hip_joint", ObservationType.JOINT_VEL),
            ("dq_RR_thigh_joint", "RR_thigh_joint", ObservationType.JOINT_VEL),
            ("dq_RR_calf_joint", "RR_calf_joint", ObservationType.JOINT_VEL),
            ("dq_RL_hip_joint", "RL_hip_joint", ObservationType.JOINT_VEL),
            ("dq_RL_thigh_joint", "RL_thigh_joint", ObservationType.JOINT_VEL),
            ("dq_RL_calf_joint", "RL_calf_joint", ObservationType.JOINT_VEL)]

        return observation_spec

    @staticmethod
    def _get_action_specification():
        """
        Getter for the action space specification.

        Returns:
            A list of tuples containing the specification of each action
            space entry.

        """

        action_spec = [
            "FR_hip", "FR_thigh", "FR_calf",
            "FL_hip", "FL_thigh", "FL_calf",
            "RR_hip", "RR_thigh", "RR_calf",
            "RL_hip", "RL_thigh", "RL_calf"]

        return action_spec

    @staticmethod
    def _interpolate_map(traj, **interpolate_map_params):
        """
        A mapping that is supposed to transform a trajectory into a space where interpolation is
        allowed. E.g., maps a rotation matrix to a set of angles.

        Args:
            traj (list): List of np.arrays containing each observations. Each np.array
                has the shape (n_trajectories, n_samples, (dim_observation)). If dim_observation
                is one the shape of the array is just (n_trajectories, n_samples).
            interpolate_map_params: Set of parameters needed to do the interpolation by the Unitree environment.

        Returns:
            A np.array with shape (n_observations, n_trajectories, n_samples). dim_observation
            has to be one.

        """

        rot_mat_idx = interpolate_map_params["rot_mat_idx"]
        trunk_orientation_idx = interpolate_map_params["trunk_orientation_idx"]
        traj_list = [list() for j in range(len(traj))]
        for i in range(len(traj_list)):
            # if the state is a rotation
            if i in trunk_orientation_idx:  # todo: not sure if this is actually needed.
                # change it to the nearest rotation presentation to the previous state
                # -> no huge jumps between -pi and pi for example
                traj_list[i] = list(np.unwrap(traj[i]))
            else:
                traj_list[i] = list(traj[i])
        # turn matrices into angles todo: this is slow, implement vectorized implementation
        traj_list[rot_mat_idx] = np.array([mat2angle_xy(mat) for mat in traj[rot_mat_idx]])
        return np.array(traj_list)

    @staticmethod
    def _interpolate_remap(traj, **interpolate_remap_params):
        """
        The corresponding backwards transformation to _interpolation_map.

        Args:
            traj (np.array): Trajectory as np.array with shape (n_observations, n_trajectories, n_samples).
            dim_observation is one.
            interpolate_remap_params: Set of parameters needed to do the interpolation by the Unitree environment.

        Returns:
            List of np.arrays containing each observations. Each np.array has the shape
            (n_trajectories, n_samples, (dim_observation)). If dim_observation
            is one the shape of the array is just (n_trajectories, n_samples).

        """

        angle_idx = interpolate_remap_params["angle_idx"]
        trunk_orientation_idx = interpolate_remap_params["trunk_orientation_idx"]
        traj_list = [list() for j in range(len(traj))]
        for i in range(len(traj_list)):
            # if the state is a rotation
            if i in trunk_orientation_idx:
                # make sure it is in range -pi,pi
                traj_list[i] = [transform_angle_2pi(angle) for angle in traj[i]]
            else:
                traj_list[i] = list(traj[i])
        # transforms angles into rotation matrices todo: this is slow, implement vectorized implementation
        traj_list[angle_idx] = np.array([angle2mat_xy(angle).reshape(9,) for angle in traj[angle_idx]])
        return traj_list