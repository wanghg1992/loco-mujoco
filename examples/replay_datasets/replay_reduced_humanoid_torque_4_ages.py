import numpy as np

from loco_mujoco import LocoEnv


def experiment(seed=0):

    np.random.seed(seed)

    mdp = LocoEnv.make("ReducedHumanoidTorque4Ages")

    mdp.play_trajectory(250)


if __name__ == '__main__':
    experiment()
