import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from make_code import go2_camera as app


class CameraTests(unittest.TestCase):
    def test_project_paths_and_configuration(self):
        self.assertEqual(app.ROOT, Path(__file__).resolve().parent)
        self.assertTrue((app.SDK / 'unitree_sdk2py').is_dir())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('GO2_NET_IF="eth-test" # Go2\nOTHER=value\n# comment\n')
            with patch.object(app, 'ROOT', root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(app.configured_interface(), 'eth-test')
                os.environ['GO2_NET_IF'] = 'eth-env'
                self.assertEqual(app.configured_interface(), 'eth-env')
                os.environ['GO2_NET_IF'] = ''
                self.assertIsNone(app.configured_interface())

    def test_interface_requires_ipv4(self):
        result = Mock(returncode=0, stdout=json.dumps([
            {'ifname': 'eth-empty', 'addr_info': []},
            {'ifname': 'eth-go2', 'addr_info': [
                {'family': 'inet', 'local': '192.168.123.99'}]},
        ]))
        with patch.object(app.subprocess, 'run', return_value=result):
            self.assertEqual(app.network_interface(None), 'eth-go2')
            with self.assertRaises(RuntimeError):
                app.network_interface('eth-empty')
            with self.assertRaises(RuntimeError):
                app.network_interface('missing')

    def test_jpeg_passthrough_and_png_conversion(self):
        image = np.zeros((12, 16, 3), dtype=np.uint8)
        client = Mock()
        for extension in ('.jpg', '.png'):
            with self.subTest(extension=extension):
                ok, encoded = cv2.imencode(extension, image)
                self.assertTrue(ok)
                payload = encoded.tobytes()
                client.GetImageSample.return_value = (0, list(payload))
                jpeg = app.get_jpeg(client)
                decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                self.assertEqual(decoded.shape, image.shape)
                if extension == '.jpg':
                    self.assertEqual(jpeg, payload)

    def test_failed_and_empty_image_responses(self):
        client = Mock()
        for response in ((123, []), (0, []), (0, None), (0, b'invalid')):
            with self.subTest(response=response):
                client.GetImageSample.return_value = response
                with self.assertRaises(RuntimeError):
                    app.get_jpeg(client)

    def test_startup_failure_returns_nonzero(self):
        with patch('sys.argv', ['go2_camera.py', '--check', '--net-if', 'missing']), \
                patch.object(app, 'network_interface', side_effect=RuntimeError('missing')), \
                patch.object(app, 'create_client') as create, patch('sys.stderr'):
            self.assertEqual(app.main(), 1)
            create.assert_not_called()

    def test_check_decodes_image_and_exits_without_web_server(self):
        _, encoded = cv2.imencode('.jpg', np.zeros((12, 16, 3), dtype=np.uint8))
        client = Mock()
        client.GetImageSample.return_value = (0, list(encoded.tobytes()))
        with patch('sys.argv', ['go2_camera.py', '--check', '--net-if', 'eth-go2']), \
                patch.object(app, 'network_interface', return_value='eth-go2') as net_if, \
                patch.object(app, 'create_client', return_value=client), \
                patch.object(app, 'ThreadingHTTPServer') as server, patch('builtins.print') as output:
            self.assertEqual(app.main(), 0)
            net_if.assert_called_once_with('eth-go2')
            server.assert_not_called()
            self.assertIn('16×12', output.call_args.args[0])


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.frames = app.Frames()
        self.stop = threading.Event()
        self.server = app.ThreadingHTTPServer(
            ('127.0.0.1', 0), app.make_handler(self.frames, 'test-token', self.stop))
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.worker.start()
        self.connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)

    def tearDown(self):
        self.connection.close()
        self.stop.set()
        with self.frames.condition:
            self.frames.condition.notify_all()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def get(self, path):
        self.connection.request('GET', path)
        response = self.connection.getresponse()
        return response.status, response.read()

    def test_token_page_and_status_recovery(self):
        self.assertEqual(self.get('/')[0], 403)
        status, body = self.get('/?token=test-token')
        self.assertEqual(status, 200)
        self.assertIn(b'/stream?token=test-token', body)
        self.assertEqual(self.get('/status?token=test-token')[0], 503)
        self.frames.publish(b'jpeg')
        status, body = self.get('/status?token=test-token')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['receiving'])
        self.frames.set_error('connection lost')
        status, body = self.get('/status?token=test-token')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)['error'], 'connection lost')
        self.frames.publish(b'jpeg')
        self.assertEqual(self.get('/status?token=test-token')[0], 200)

    def test_mjpeg_contains_published_image(self):
        _, encoded = cv2.imencode('.jpg', np.zeros((12, 16, 3), dtype=np.uint8))
        jpeg = encoded.tobytes()
        self.frames.publish(jpeg)
        self.connection.request('GET', '/stream?token=test-token')
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn('multipart/x-mixed-replace', response.getheader('Content-Type'))
        self.assertEqual(response.readline(), b'--frame\r\n')
        self.assertEqual(response.readline(), b'Content-Type: image/jpeg\r\n')
        self.assertEqual(response.readline(), f'Content-Length: {len(jpeg)}\r\n'.encode())
        self.assertEqual(response.readline(), b'\r\n')
        self.assertEqual(response.read(len(jpeg)), jpeg)
        response.close()


if __name__ == '__main__':
    unittest.main()
