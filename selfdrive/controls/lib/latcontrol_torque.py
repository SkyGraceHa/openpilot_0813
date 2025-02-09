import math
from common.numpy_fast import interp
from selfdrive.controls.lib.latcontrol_pid import ERROR_RATE_FRAME
from selfdrive.controls.lib.pid import PIController
from selfdrive.controls.lib.latcontrol import LatControl, MIN_STEER_SPEED
from cereal import log
from common.params import Params
from decimal import Decimal

CURVATURE_SCALE = 200
JERK_THRESHOLD = 0.2


class LatControlTorque(LatControl):
  def __init__(self, CP, CI):
    super().__init__(CP, CI)
    self.mpc_frame = 0    
    self.pid = PIController(CP.lateralTuning.torque.kp, CP.lateralTuning.torque.ki,
                            k_f=CP.lateralTuning.torque.kf, pos_limit=1.0, neg_limit=-1.0)
    self.get_steer_feedforward = CI.get_steer_feedforward_function()
    self.steer_max = 1.0
    self.pid.pos_limit = self.steer_max
    self.pid.neg_limit = -self.steer_max
    self.use_steering_angle = CP.lateralTuning.torque.useSteeringAngle
    self.friction = CP.lateralTuning.torque.friction
    self.errors = []

    self.params = Params()
    
    self.live_tune_enabled = False

    self.reset()

    self.ll_timer = 0    

  def reset(self):
    super().reset()
    self.pid.reset()

  def live_tune(self, CP):
    self.mpc_frame += 1
    if self.mpc_frame % 300 == 0:
      self.kp = float(Decimal(self.params.get("TorqKp", encoding="utf8")) * Decimal('0.1'))
      self.ki = float(Decimal(self.params.get("TorqKi", encoding="utf8")) * Decimal('0.001'))
      self.kf = float(Decimal(self.params.get("TorqKf", encoding="utf8")) * Decimal('0.001'))
      self.pid = PIController(self.kp, self.ki, k_f=self.kf, pos_limit=1.0, neg_limit=-1.0)
      
      self.friction = float(Decimal(self.params.get("friction", encoding="utf8")) * Decimal('0.001'))
        
      self.mpc_frame = 0

  def update(self, active, CS, CP, VM, params, last_actuators, desired_curvature, desired_curvature_rate, llk):
    self.ll_timer += 1
    if self.ll_timer > 100:
      self.ll_timer = 0
      self.live_tune_enabled = self.params.get_bool("OpkrLiveTunePanelEnable")
    if self.live_tune_enabled:
      self.live_tune(CP)

    pid_log = log.ControlsState.LateralTorqueState.new_message()

    if CS.vEgo < MIN_STEER_SPEED or not active:
      output_torque = 0.0
      pid_log.active = False
      self.pid.reset()
    else:
      if self.use_steering_angle:
        actual_curvature = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
      else:
        actual_curvature = llk.angularVelocityCalibrated.value[2] / CS.vEgo
      desired_lateral_accel = desired_curvature * CS.vEgo**2
      desired_lateral_jerk = desired_curvature_rate * CS.vEgo**2
      actual_lateral_accel = actual_curvature * CS.vEgo**2

      setpoint = desired_lateral_accel + CURVATURE_SCALE * desired_curvature
      measurement = actual_lateral_accel + CURVATURE_SCALE * actual_curvature
      error = setpoint - measurement
      pid_log.error = error

      error_rate = 0
      if len(self.errors) >= ERROR_RATE_FRAME:
        error_rate = (error - self.errors[-ERROR_RATE_FRAME]) / ERROR_RATE_FRAME

      self.errors.append(float(error))
      while len(self.errors) > ERROR_RATE_FRAME:
        self.errors.pop(0)

      ff = desired_lateral_accel - params.roll * 9.81
      output_torque = self.pid.update(error, error_rate, override=CS.steeringPressed,
                                     feedforward=ff, speed=CS.vEgo)

      friction_compensation = interp(desired_lateral_jerk, [-JERK_THRESHOLD, JERK_THRESHOLD], [-self.friction, self.friction])
      output_torque += friction_compensation

      pid_log.active = True
      pid_log.p = self.pid.p
      pid_log.i = self.pid.i
      #pid_log.d = self.pid.d
      pid_log.f = self.pid.f
      pid_log.output = -output_torque
      pid_log.saturated = self._check_saturation(self.steer_max - abs(output_torque) < 1e-3, CS)

    #TODO left is positive in this convention
    return -output_torque, 0.0, pid_log 