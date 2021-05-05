from pathlib import Path

import cv_bridge
import numpy as np
import rospy
import scipy.interpolate

from geometry_msgs.msg import Pose
from sensor_msgs.msg import Image, CameraInfo

from robot_utils.spatial import Rotation, Transform
from robot_utils.ros.conversions import *
from robot_utils.ros.tf import TransformTree
from robot_utils.perception import *
from vgn import vis
from vgn.detection import VGN, compute_grasps


def get_policy(name):
    if name == "single-view":
        return SingleViewBaseline()
    elif name == "fixed-trajectory":
        return FixedTrajectoryBaseline()
    else:
        raise ValueError("{} policy does not exist.".format(name))


class Policy:
    def __init__(self):
        params = rospy.get_param("active_grasp")

        self.frame_id = params["frame_id"]

        # Robot
        self.base_frame_id = params["base_frame_id"]
        self.ee_frame_id = params["ee_frame_id"]
        self.tf = TransformTree()
        self.H_EE_G = Transform.from_list(params["ee_grasp_offset"])
        self.target_pose_pub = rospy.Publisher("/target", Pose, queue_size=10)

        # Camera
        camera_name = params["camera_name"]
        self.cam_frame_id = camera_name + "_optical_frame"
        depth_topic = camera_name + "/depth/image_raw"
        msg = rospy.wait_for_message(camera_name + "/depth/camera_info", CameraInfo)
        self.intrinsic = from_camera_info_msg(msg)
        self.cv_bridge = cv_bridge.CvBridge()

        # TSDF
        self.tsdf = UniformTSDFVolume(0.3, 40)

        # VGN
        params = rospy.get_param("vgn")
        self.vgn = VGN(Path(params["model"]))

        rospy.sleep(1.0)
        self.H_B_T = self.tf.lookup(self.base_frame_id, self.frame_id, rospy.Time.now())
        rospy.Subscriber(depth_topic, Image, self.sensor_cb, queue_size=1)

        vis.draw_workspace(0.3)

    def sensor_cb(self, msg):
        self.last_depth_img = self.cv_bridge.imgmsg_to_cv2(msg).astype(np.float32)
        self.last_extrinsic = self.tf.lookup(
            self.cam_frame_id, self.frame_id, msg.header.stamp, rospy.Duration(0.1)
        )

    def get_tsdf_grid(self):
        map_cloud = self.tsdf.get_map_cloud()
        points = np.asarray(map_cloud.points)
        distances = np.asarray(map_cloud.colors)[:, 0]
        return create_grid_from_map_cloud(points, distances, self.tsdf.voxel_size)

    def plan_best_grasp(self):
        tsdf_grid = self.get_tsdf_grid()
        out = self.vgn.predict(tsdf_grid)
        grasps = compute_grasps(out, voxel_size=self.tsdf.voxel_size)

        vis.draw_tsdf(tsdf_grid, self.tsdf.voxel_size)
        vis.draw_grasps(grasps, 0.05)

        # Ensure that the camera is pointing forward.
        grasp = grasps[0]
        rot = grasp.pose.rotation
        axis = rot.as_matrix()[:, 0]
        if axis[0] < 0:
            grasp.pose.rotation = rot * Rotation.from_euler("z", np.pi)

        # Compute target pose of the EE
        H_T_G = grasp.pose
        H_B_EE = self.H_B_T * H_T_G * self.H_EE_G.inv()
        return H_B_EE


class SingleViewBaseline(Policy):
    def __init__(sel):
        super().__init__()

    def start(self):
        self.done = False

    def update(self):
        # Integrate image
        self.tsdf.integrate(
            self.last_depth_img,
            self.intrinsic,
            self.last_extrinsic,
        )

        # Visualize reconstruction
        cloud = self.tsdf.get_scene_cloud()
        vis.draw_points(np.asarray(cloud.points))

        # Plan grasp
        self.best_grasp = self.plan_best_grasp()
        self.done = True
        return


class FixedTrajectoryBaseline(Policy):
    def __init__(self):
        super().__init__()
        self.duration = 4.0
        self.radius = 0.1
        self.m = scipy.interpolate.interp1d([0, self.duration], [np.pi, 3.0 * np.pi])

    def start(self):
        self.tic = rospy.Time.now()
        timeout = rospy.Duration(0.1)
        x0 = self.tf.lookup(self.base_frame_id, self.ee_frame_id, self.tic, timeout)
        self.origin = np.r_[x0.translation[0] + self.radius, x0.translation[1:]]
        self.target = x0
        self.done = False

    def update(self):
        elapsed_time = (rospy.Time.now() - self.tic).to_sec()

        # Integrate image
        self.tsdf.integrate(
            self.last_depth_img,
            self.intrinsic,
            self.last_extrinsic,
        )

        # Visualize current integration
        cloud = self.tsdf.get_scene_cloud()
        vis.draw_points(np.asarray(cloud.points))

        if elapsed_time > self.duration:
            self.best_grasp = self.plan_best_grasp()
            self.done = True
            return

        t = self.m(elapsed_time)
        self.target.translation = (
            self.origin + np.r_[self.radius * np.cos(t), self.radius * np.sin(t), 0.0]
        )
        self.target_pose_pub.publish(to_pose_msg(self.target))
