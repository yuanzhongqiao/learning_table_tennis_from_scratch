import pickle
import time
import math
import random
import o80
import o80_pam
import pam_mujoco
import context

SEGMENT_ID_BALL = pam_mujoco.segment_ids.ball
SEGMENT_ID_GOAL = pam_mujoco.segment_ids.goal
SEGMENT_ID_HIT_POINT = pam_mujoco.segment_ids.hit_point
SEGMENT_ID_CONTACT_ROBOT = pam_mujoco.segment_ids.contact_robot
SEGMENT_ID_ROBOT_MIRROR = pam_mujoco.segment_ids.mirroring
SEGMENT_ID_PSEUDO_REAL_ROBOT = o80_pam.segment_ids.robot


def _no_hit_reward(min_distance_ball_racket):
    return -min_distance_ball_racket


def _return_task_reward( min_distance_ball_target,
                         c,
                         rtt_cap ):
    reward = 1.- c * (min_distance_ball_target ** 0.75)
    reward = max(reward,rtt_cap)
    return reward


def _smash_task_reward( min_distance_ball_target,
                        max_ball_velocity,
                        c,
                        rtt_cap ):
    reward = 1.- c * (min_distance_ball_target ** 0.75)
    reward = reward * max_ball_velocity
    reward = max(reward,rtt_cap)
    return reward


def _reward(smash, 
            min_distance_ball_racket,
            min_distance_ball_target,
            max_ball_velocity,
            c,
            rtt_cap):

    # i.e. the ball did not hit the racket,
    # so computing a reward based on the minimum
    # distance between the racket and the ball
    if min_distance_ball_racket is not None:
        return _no_hit_reward(min_distance_ball_racket)

    # the ball did hit the racket, so computing
    # a reward based on the ball / target
    
    if smash:
        return _smash_task_reward( min_distance_ball_target,
                                   max_ball_velocity,
                                   c,
                                   rtt_cap )
    
    else:
        return _return_task_reward( min_distance_ball_target,
                                    c,
                                    rtt_cap )

    
def _convert_pressures_in(pressures):
    # convert pressure from [ago1, antago1, ago2, antago2, ...]
    # to [(ago1, antago1), (ago2, antago2), ...]
    return list(zip(pressures[::2],pressures[1::2]))


def _convert_pressures_out(pressures_ago,pressures_antago):
    pressures = list(zip(pressures_ago,pressures_antago))
    return [p for sublist in pressures for p in sublist]


class _Observation:

    def __init__(self,
                 joint_positions,
                 joint_velocities,
                 pressures,
                 ball_position,
                 ball_velocity):
        self.joint_positions = joint_positions
        self.joint_velocities = joint_velocities
        self.pressures = pressures
        self.ball_position = ball_position
        self.ball_velocity = ball_velocity


class HysrOneBall:

    def __init__(self,
                 accelerated_time,
                 o80_time_step,
                 mujoco_time_step,
                 algo_time_step,
                 reference_posture,
                 target_position,
                 reward_normalization_constant,
                 smash_task,
                 rtt_cap=-0.2,
                 trajectory_index=None):

        # moving the goal to the target position
        goal = o80_pam.o80Goal(SEGMENT_ID_GOAL)
        goal.set(target_position,[0,0,0])

        # if o80_pam (i.e. the pseudo real robot)
        # has been started in accelerated time,
        # the corresponding o80 backend will burst through
        # an algorithm time step
        self._accelerated_time = accelerated_time
        if accelerated_time:
            self._o80_time_step = o80_time_step
            self._nb_robot_bursts = int(algo_time_step/o80_time_step)

        # pam_mujoco (i.e. simulated ball and robot) should have been
        # started in accelerated time. It burst through algorithm
        # time steps
        self._nb_sim_bursts = int(algo_time_step/mujoco_time_step)
        
        # if a number, the same trajectory will be played
        # all the time. If None, a random trajectory will be
        # played each time
        self._trajectory_index = trajectory_index
        
        # the posture in which the robot will reset itself
        # upon reset
        self._reference_posture = reference_posture

        # the robot will interpolate between current and
        # target posture over this duration
        self._period_ms = algo_time_step
        
        # reward configuration
        self._c = reward_normalization_constant 
        self._smash_task = smash_task # True or False (return task)
        self._rtt_cap = rtt_cap

        # to get information regarding the ball
        self._ball_communication = o80_pam.o80Ball(SEGMENT_ID_BALL)
        
        # to send pressure commands to the real or pseudo-real robot
        self._pressure_commands = o80_pam.o80Pressures(SEGMENT_ID_PSEUDO_REAL_ROBOT)
        
        # will encapsulate all information
        # about the ball (e.g. min distance with racket, etc)
        self._ball_status = context.BallStatus(target_position)
        
        # to send mirroring commands to simulated robot
        self._mirroring = o80_pam.o80RobotMirroring(SEGMENT_ID_ROBOT_MIRROR)

        # to move the hit point marker
        self._hit_point = o80_pam.o80HitPoint(SEGMENT_ID_HIT_POINT)
        

    def _create_observation(self):
        (pressures_ago,pressures_antago,
         joint_positions,joint_velocities) = self._pressure_commands.read()
        observation = _Observation(joint_positions,joint_velocities,
                                   _convert_pressures_out(pressures_ago,
                                                          pressures_antago),
                                   self._ball_status.ball_position,
                                   self._ball_status.ball_velocity)
        return observation
    
        
    def get_robot_iteration(self):
        return self._pressure_commands.get_iteration()

    
    def get_ball_iteration(self):
        return self._ball_communication.get_iteration()


    def get_current_desired_pressures(self):
        (pressures_ago,pressures_antago,
         _,__) = self._pressure_commands.read(desired=True)
        return pressures_ago,pressures_antago
    
        
    def reset(self):

        # resetting the hit point 
        self._hit_point.set([0,0,-0.62],[0,0,0])
        
        # resetting ball info, e.g. min distance ball/racket, etc
        self._ball_status.reset()

        # resetting ball/robot contact information
        pam_mujoco.reset_contact(SEGMENT_ID_CONTACT_ROBOT)
        time.sleep(0.1)

        # moving real robot back to reference posture
        if self._reference_posture is not None:
            self._pressure_commands.set(self._reference_posture,
                                        duration_ms=1500,wait=False)
            if self._accelerated_time:
                self._pressure_commands.burst(int(o80_time_step/1500.0)+1)
            else:
                self._pressure_commands.pulse_and_wait()

        # moving simulated robot to reference posture
        (pressures_ago,pressures_antago,
         joint_positions,joint_velocities) = self._pressure_commands.read()
        self._mirroring.set(joint_positions,joint_velocities,nb_iterations=100)
        self._mirroring.burst(100+1)

        # getting a new trajectory
        if self._trajectory_index is not None:
            trajectory_points = context.BallTrajectories().get_trajectory(self._trajectory_index)
        else:
            _,trajectory_points = context.BallTrajectories().random_trajectory()
            
        # setting the last trajectory point way below the table, to be sure
        # end of episode will be detected
        last_state = context.State([0,0,-10.00],[0,0,0])
        trajectory_points.append(last_state)

        # setting the ball to the first trajectory point
        self._ball_communication.set(trajectory_points[0].position,
                                     trajectory_points[0].velocity)
        self._mirroring.burst(5)

        # shooting the ball
        self._ball_communication.play_trajectory(trajectory_points)

        # returning an observation
        return self._create_observation()
        
            
    def _episode_over(self):
        over = False
        # ball falled below the table
        # note : all prerecorded trajectories are added a last ball position
        # with z = -10.0, to insure this always occurs.
        # see: function reset
        if self._ball_status.ball_position[2] < -0.5:
            over = True
        return over

    
    def create_observation(self):
        # reading current real (or pseudo real) robot state
        (pressures_ago,pressures_antago,
         joint_positions,joint_velocities) = self._pressure_commands.read()
        # getting information about simulated ball
        ball_position,ball_velocity = self._ball_communication.get()
        return _Observation( joint_positions,joint_velocities,
                             pressures_ago,pressures_antago,
                             ball_status.ball_position,
                             ball_status.ball_velocity )

        
    # action assumed to be np.array(ago1,antago1,ago2,antago2,...)
    def step(self,action):

        # reading current real (or pseudo real) robot state
        (pressures_ago,pressures_antago,
         joint_positions,joint_velocities) = self._pressure_commands.read()

        # getting information about simulated ball
        ball_position,ball_velocity = self._ball_communication.get()

        # sending action pressures to real (or pseudo real) robot.
        # Should start acting now in the background if not accelerated time
        self._pressure_commands.set(_convert_pressures_in(list(action)))

        # if accelerated times, running the pseudo real robot iterations
        # (note : o80_pam expected to have started in bursting mode)
        if self._accelerated_time:
            self._pressure_commands.burst(self._nb_robot_bursts)

        # sending mirroring state to simulated robot
        self._mirroring.set(joint_positions,joint_velocities)

        # having the simulated robot/ball performing the right number of iterations
        # (note: simulated expected to run accelerated time)
        self._mirroring.burst(self._nb_sim_bursts)

        # getting ball/racket contact information
        # note : racket_contact_information is an instance
        #        of context.ContactInformation
        racket_contact_information = pam_mujoco.get_contact(SEGMENT_ID_CONTACT_ROBOT)

        # updating ball status
        self._ball_status.update(ball_position,ball_velocity,
                                 racket_contact_information)

        # moving the hit point to the minimal observed distance
        # between ball and target (post racket hit)
        if self._ball_status.min_position_ball_target is not None:
            self._hit_point.set(self._ball_status.min_position_ball_target,[0,0,0])
        
        # observation instance
        observation = _Observation(joint_positions,joint_velocities,
                                   _convert_pressures_out(pressures_ago,
                                                          pressures_antago),
                                   self._ball_status.ball_position,
                                   self._ball_status.ball_velocity)

        # checking if episode is over
        episode_over = self._episode_over()
        reward = None

        # if episode over, computing related reward
        if episode_over:
            reward = _reward( self._smash_task,
                              self._ball_status.min_distance_ball_racket,
                              self._ball_status.min_distance_ball_target,
                              self._ball_status.max_ball_velocity,
                              self._c,self._rtt_cap )
            
                
        # returning
        return observation,reward,episode_over

    
    def close(self):
        pass
