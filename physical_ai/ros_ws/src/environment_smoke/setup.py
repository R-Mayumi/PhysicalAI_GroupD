from setuptools import setup
setup(name='environment_smoke', version='0.1.0',
      packages=['environment_smoke'],
      data_files=[('share/ament_index/resource_index/packages',['resource/environment_smoke']),
                  ('share/environment_smoke',['package.xml']),
                  ('share/environment_smoke/launch',['launch/check.launch.py'])],
      entry_points={'console_scripts':['check = environment_smoke.check:main']})
