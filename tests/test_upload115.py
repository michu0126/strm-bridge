import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from bridge.upload115 import upload, digest, UploadCancelled, multipart_init
from bridge.cloud_providers import CloudError


class UploadProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'clip.mp4'; self.path.write_bytes(b'hello')
        self.sha = hashlib.sha1(b'hello').hexdigest().upper()
        self.client = Mock(); self.client.request.return_value = {'user_id': '100', 'userkey': 'PRIVATE-KEY', 'upload_allowed': 1}
        self.sdk = Mock()
        self.sdk.upload_file_init.return_value = {'state': True, 'reuse': False,
            'data': {'user_key': 'PRIVATE-KEY', 'object': 'object', 'bucket': 'bucket', 'callback': {'callback': 'private-callback'}}}
        self.sdk.oss_multipart_upload_init.return_value = 'upload-id'
        self.sdk.oss_multipart_part_iter.return_value = iter([])
        self.sdk.oss_multipart_upload_part_iter.return_value = iter([{'PartNumber': 1, 'Size': 5, 'ETag': 'etag'}])
        self.sdk.oss_multipart_upload_complete.return_value = {'state': True}
        self.modules = patch.dict(sys.modules, p115oss=self.sdk); self.modules.start()
    def tearDown(self):
        self.modules.stop(); self.temp.cleanup()
    def test_streaming_multipart_checkpoint_excludes_account_key(self):
        checkpoints = []
        result = upload(self.client, self.path, '0', self.sha, checkpoint=checkpoints.append)
        self.assertFalse(result['rapid']); self.assertEqual(checkpoints[0]['upload_id'], 'upload-id')
        self.assertNotIn('PRIVATE-KEY', json.dumps(checkpoints))
        self.assertEqual(self.sdk.upload_file_init.call_args.kwargs['filesha1'], self.sha)
        self.sdk.oss_multipart_upload_complete.assert_called_once()
    def test_rapid_upload_does_not_start_multipart_transfer(self):
        self.sdk.upload_file_init.return_value = {'state': True, 'reuse': True}
        self.assertTrue(upload(self.client, self.path, '0', self.sha)['rapid'])
        self.sdk.oss_multipart_upload_init.assert_not_called()
    def test_resume_after_all_parts_uploaded_only_completes_existing_session(self):
        session = dict(sha1=self.sha, size=5, parent='0', bucket='bucket', object='object',
                       callback={}, upload_id='existing', partsize=8*1024*1024)
        self.sdk.oss_multipart_part_iter.return_value = iter([{'PartNumber': 1, 'Size': 5}])
        self.sdk.oss_multipart_upload_part_iter.return_value = iter([])
        upload(self.client, self.path, '0', self.sha, session=session)
        self.sdk.upload_file_init.assert_not_called()
        self.assertEqual(self.sdk.oss_multipart_upload_part_iter.call_args.kwargs['part_number_start'], 2)
    def test_invalid_session_or_incomplete_part_bytes_do_not_commit(self):
        self.sdk.oss_multipart_upload_part_iter.return_value = iter([{'PartNumber': 1, 'Size': 4}])
        with self.assertRaises(CloudError): upload(self.client, self.path, '0', self.sha)
        self.sdk.oss_multipart_upload_complete.assert_not_called()
    def test_cancel_and_hashing_cancel_do_not_transfer(self):
        with self.assertRaises(UploadCancelled): digest(self.path, cancel=lambda: True)
        with self.assertRaises(UploadCancelled): upload(self.client, self.path, '0', self.sha, cancel=lambda: True)
        self.sdk.upload_file_init.assert_not_called()
    def test_sdk_error_payload_is_not_exposed(self):
        self.sdk.upload_file_init.side_effect = RuntimeError('PRIVATE-KEY signed request and callback')
        with self.assertRaises(CloudError) as caught: upload(self.client, self.path, '0', self.sha)
        self.assertNotIn('PRIVATE-KEY', str(caught.exception))
    def test_permission_denied_does_not_upload(self):
        self.client.request.return_value.update(upload_allowed=0)
        with self.assertRaises(CloudError): upload(self.client, self.path, '0', self.sha)
        self.sdk.upload_file_init.assert_not_called()
    def test_empty_file_uses_single_put(self):
        self.path.write_bytes(b'')
        self.sdk.oss_upload.return_value = {'state': True}
        upload(self.client, self.path, '0', hashlib.sha1(b'').hexdigest().upper())
        self.sdk.oss_upload.assert_called_once()
        self.sdk.oss_multipart_upload_init.assert_not_called()
    def test_multipart_init_signs_empty_oss_subresources(self):
        token={'AccessKeyId':'test-key','AccessKeySecret':'test-secret'}
        def initialize(obj, **kwargs):
            return kwargs['request'](url='https://bucket.oss-cn-shenzhen.aliyuncs.com/object?uploads=',
                headers={'date':'Tue, 27 Oct 2026 00:00:00 GMT', 'content-type':'application/octet-stream',
                         'x-oss-security-token':'test-token'}, method='POST')
        self.sdk.oss_multipart_upload_init.side_effect=initialize
        transport=Mock();transport.request.return_value='upload-id'
        with patch.dict(sys.modules, urllib3_future_request=transport):
            multipart_init(self.sdk, {'object':'object','bucket':'bucket'}, token)
        args=transport.request.call_args.kwargs
        self.assertTrue(args['url'].endswith('?sequential&uploads&x-oss-enable-sha1'))
        self.assertTrue(args['headers']['authorization'].startswith('OSS test-key:'))
    def test_expired_session_is_reset_but_connection_failure_keeps_it(self):
        self.sdk.oss_multipart_part_iter.side_effect = RuntimeError('NoSuchUpload')
        checkpoints = []
        with self.assertRaises(CloudError): upload(self.client, self.path, '0', self.sha, checkpoint=checkpoints.append)
        self.assertIsNone(checkpoints[-1])


if __name__ == '__main__': unittest.main()
