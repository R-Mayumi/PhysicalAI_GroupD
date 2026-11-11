from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([Node(package='environment_smoke', executable='check',
                                   arguments=['--seconds','5'], output='screen')])
