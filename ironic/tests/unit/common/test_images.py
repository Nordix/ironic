# coding=utf-8

# Copyright 2013 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import builtins
import io
import os
import shutil
from unittest import mock

from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_utils import fileutils
from oslo_utils.imageutils import format_inspector

from ironic.common import exception
from ironic.common.glance_service import service_utils as glance_utils
from ironic.common import image_service
from ironic.common import images
from ironic.common import qemu_img
from ironic.common import utils
from ironic.tests import base

CONF = cfg.CONF


class IronicImagesTestCase(base.TestCase):

    class FakeImgInfo(object):
        pass

    @mock.patch.object(images, '_handle_zstd_compression', autospec=True)
    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test_fetch_image_service(self, open_mock, image_service_mock,
                                 mock_zstd):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'file'
        open_mock.return_value = mock_file_handle

        images.fetch('context', 'image_href', 'path')

        open_mock.assert_called_once_with('path', 'wb')
        image_service_mock.assert_called_once_with('image_href',
                                                   context='context')
        image_service_mock.return_value.download.assert_called_once_with(
            'image_href', 'file')
        mock_zstd.assert_called_once_with('path')

    @mock.patch.object(images, '_handle_zstd_compression', autospec=True)
    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    @mock.patch.object(images, 'image_to_raw', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test_fetch_image_service_force_raw(self, open_mock, image_to_raw_mock,
                                           image_service_mock,
                                           mock_zstd):
        image_service_mock.return_value.transfer_verified_checksum = None
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'file'
        open_mock.return_value = mock_file_handle

        images.fetch('context', 'image_href', 'path', force_raw=True)

        open_mock.assert_called_once_with('path', 'wb')
        image_service_mock.return_value.download.assert_called_once_with(
            'image_href', 'file')
        image_to_raw_mock.assert_called_once_with(
            'image_href', 'path', 'path.part')
        mock_zstd.assert_called_once_with('path')

    @mock.patch.object(images, '_handle_zstd_compression', autospec=True)
    @mock.patch.object(fileutils, 'compute_file_checksum',
                       autospec=True)
    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    @mock.patch.object(images, 'image_to_raw', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test_fetch_image_service_force_raw_with_checksum(
            self, open_mock, image_to_raw_mock,
            image_service_mock, mock_checksum, mock_zstd):
        image_service_mock.return_value.transfer_verified_checksum = None
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'file'
        open_mock.return_value = mock_file_handle
        mock_checksum.return_value = 'f00'

        images.fetch('context', 'image_href', 'path', force_raw=True,
                     checksum='f00', checksum_algo='sha256')

        mock_checksum.assert_called_once_with('path', algorithm='sha256')
        open_mock.assert_called_once_with('path', 'wb')
        image_service_mock.return_value.download.assert_called_once_with(
            'image_href', 'file')
        image_to_raw_mock.assert_called_once_with(
            'image_href', 'path', 'path.part')
        mock_zstd.assert_called_once_with('path')

    @mock.patch.object(images, '_handle_zstd_compression', autospec=True)
    @mock.patch.object(fileutils, 'compute_file_checksum',
                       autospec=True)
    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    @mock.patch.object(images, 'image_to_raw', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test_fetch_image_service_with_checksum_mismatch(
            self, open_mock, image_to_raw_mock,
            image_service_mock, mock_checksum,
            mock_zstd):
        image_service_mock.return_value.transfer_verified_checksum = None
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'file'
        open_mock.return_value = mock_file_handle
        mock_checksum.return_value = 'a00'

        self.assertRaises(exception.ImageChecksumError,
                          images.fetch, 'context', 'image_href',
                          'path', force_raw=True,
                          checksum='f00', checksum_algo='sha256')

        mock_checksum.assert_called_once_with('path', algorithm='sha256')
        open_mock.assert_called_once_with('path', 'wb')
        image_service_mock.return_value.download.assert_called_once_with(
            'image_href', 'file')
        # If the checksum fails, then we don't attempt to convert the image.
        image_to_raw_mock.assert_not_called()
        mock_zstd.assert_not_called()

    @mock.patch.object(images, '_handle_zstd_compression', autospec=True)
    @mock.patch.object(fileutils, 'compute_file_checksum',
                       autospec=True)
    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    @mock.patch.object(images, 'image_to_raw', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test_fetch_image_service_force_raw_no_checksum_algo(
            self, open_mock, image_to_raw_mock,
            image_service_mock, mock_checksum,
            mock_zstd):
        image_service_mock.return_value.transfer_verified_checksum = None
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'file'
        open_mock.return_value = mock_file_handle
        mock_checksum.return_value = 'f00'

        images.fetch('context', 'image_href', 'path', force_raw=True,
                     checksum='f00')

        mock_checksum.assert_called_once_with('path', algorithm='md5')
        open_mock.assert_called_once_with('path', 'wb')
        image_service_mock.return_value.download.assert_called_once_with(
            'image_href', 'file')
        image_to_raw_mock.assert_called_once_with(
            'image_href', 'path', 'path.part')
        mock_zstd.assert_called_once_with('path')

    @mock.patch.object(images, '_handle_zstd_compression', autospec=True)
    @mock.patch.object(fileutils, 'compute_file_checksum',
                       autospec=True)
    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    @mock.patch.object(images, 'image_to_raw', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test_fetch_image_service_force_raw_combined_algo(
            self, open_mock, image_to_raw_mock,
            image_service_mock, mock_checksum,
            mock_zstd):
        image_service_mock.return_value.transfer_verified_checksum = None
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'file'
        open_mock.return_value = mock_file_handle
        mock_checksum.return_value = 'f00'

        images.fetch('context', 'image_href', 'path', force_raw=True,
                     checksum='sha512:f00')

        mock_checksum.assert_called_once_with('path', algorithm='sha512')
        open_mock.assert_called_once_with('path', 'wb')
        image_service_mock.return_value.download.assert_called_once_with(
            'image_href', 'file')
        image_to_raw_mock.assert_called_once_with(
            'image_href', 'path', 'path.part')
        mock_zstd.assert_called_once_with('path')

    @mock.patch.object(images, '_handle_zstd_compression', autospec=True)
    @mock.patch.object(fileutils, 'compute_file_checksum',
                       autospec=True)
    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    @mock.patch.object(images, 'image_to_raw', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test_fetch_image_service_auth_data_checksum(
            self, open_mock, image_to_raw_mock,
            svc_mock, mock_checksum,
            mock_zstd):
        svc_mock.return_value.transfer_verified_checksum = 'f00'
        svc_mock.return_value.is_auth_set_needed = True
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'file'
        open_mock.return_value = mock_file_handle
        mock_checksum.return_value = 'f00'

        images.fetch('context', 'image_href', 'path', force_raw=True,
                     checksum='sha512:f00', image_auth_data='meow')
        # In this case, the image service does the checksum so we know
        # we don't need to do a checksum pass as part of the common image
        # handling code path.
        mock_checksum.assert_not_called()
        open_mock.assert_called_once_with('path', 'wb')
        svc_mock.return_value.download.assert_called_once_with(
            'image_href', 'file')
        image_to_raw_mock.assert_called_once_with(
            'image_href', 'path', 'path.part')
        svc_mock.return_value.set_image_auth.assert_called_once_with(
            'image_href', 'meow')
        mock_zstd.assert_called_once_with('path')

    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_not_permitted_format(self, detect_format_mock):
        info = mock.MagicMock()
        # In the case the image looks okay, but it is not in our permitted
        # format list, we need to ensure we still fail appropriately.
        info.__str__.return_value = 'vhd'
        detect_format_mock.return_value = info

        e = self.assertRaises(exception.ImageUnacceptable, images.image_to_raw,
                              'image_href', 'path', 'path_tmp')
        info.safety_check.assert_called_once()
        detect_format_mock.assert_called_once_with('path_tmp')
        self.assertIn("The requested image is not valid for use.", str(e))

    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_fails_safety_check(self, detect_format_mock):
        info = mock.MagicMock()
        info.__str__.return_value = 'qcow2'
        info.safety_check.side_effect = \
            format_inspector.SafetyCheckFailed({"I'm a teapot": True})
        detect_format_mock.return_value = info

        e = self.assertRaises(exception.ImageUnacceptable, images.image_to_raw,
                              'image_href', 'path', 'path_tmp')
        info.safety_check.assert_called_once()
        detect_format_mock.assert_called_once_with('path_tmp')
        self.assertIn("The requested image is not valid for use.", str(e))
        # Do not disclose the actual error message to evil hackers
        self.assertNotIn("I'm a teapot", str(e))

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(os, 'unlink', autospec=True)
    @mock.patch.object(qemu_img, 'convert_image', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw(self, detect_format_mock, convert_image_mock,
                          unlink_mock, rename_mock):
        CONF.set_override('force_raw_images', True)
        info = mock.MagicMock()
        info.__str__.side_effect = iter(['qcow2', 'raw'])
        info.backing_file = None
        detect_format_mock.return_value = info

        def convert_side_effect(source, dest, out_format, source_format):
            info.file_format = 'raw'
        convert_image_mock.side_effect = convert_side_effect

        images.image_to_raw('image_href', 'path', 'path_tmp')
        info.safety_check.assert_called_once()
        self.assertEqual(2, info.__str__.call_count)
        detect_format_mock.assert_has_calls([
            mock.call('path_tmp'),
            mock.call('path.converted')])
        convert_image_mock.assert_called_once_with('path_tmp',
                                                   'path.converted', 'raw',
                                                   source_format='qcow2')
        unlink_mock.assert_called_once_with('path_tmp')
        rename_mock.assert_called_once_with('path.converted', 'path')

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(os, 'unlink', autospec=True)
    @mock.patch.object(qemu_img, 'convert_image', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_gpt(self, detect_format_mock, convert_image_mock,
                          unlink_mock, rename_mock):
        CONF.set_override('force_raw_images', True)
        info = mock.MagicMock()
        info.__str__.side_effect = iter(['qcow2', 'gpt'])
        info.backing_file = None
        detect_format_mock.return_value = info

        def convert_side_effect(source, dest, out_format, source_format):
            info.file_format = 'gpt'
        convert_image_mock.side_effect = convert_side_effect

        images.image_to_raw('image_href', 'path', 'path_tmp')
        info.safety_check.assert_called_once()
        self.assertEqual(2, info.__str__.call_count)
        detect_format_mock.assert_has_calls([
            mock.call('path_tmp'),
            mock.call('path.converted')])
        convert_image_mock.assert_called_once_with('path_tmp',
                                                   'path.converted', 'raw',
                                                   source_format='qcow2')
        unlink_mock.assert_called_once_with('path_tmp')
        rename_mock.assert_called_once_with('path.converted', 'path')

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(os, 'unlink', autospec=True)
    @mock.patch.object(qemu_img, 'convert_image', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_gpt_backward_compatibility(self, detect_format_mock,
                                                 convert_image_mock,
                                                 unlink_mock, rename_mock):
        CONF.set_override('force_raw_images', True)
        CONF.set_override('permitted_image_formats', 'raw,qcow2',
                          group='conductor')
        info = mock.MagicMock()
        info.__str__.side_effect = iter(['qcow2', 'gpt'])
        info.backing_file = None
        detect_format_mock.return_value = info

        def convert_side_effect(source, dest, out_format, source_format):
            info.file_format = 'gpt'
        convert_image_mock.side_effect = convert_side_effect

        images.image_to_raw('image_href', 'path', 'path_tmp')
        info.safety_check.assert_called_once()
        self.assertEqual(2, info.__str__.call_count)
        detect_format_mock.assert_has_calls([
            mock.call('path_tmp'),
            mock.call('path.converted')])
        convert_image_mock.assert_called_once_with('path_tmp',
                                                   'path.converted', 'raw',
                                                   source_format='qcow2')
        unlink_mock.assert_called_once_with('path_tmp')
        rename_mock.assert_called_once_with('path.converted', 'path')

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(os, 'unlink', autospec=True)
    @mock.patch.object(qemu_img, 'convert_image', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_safety_check_disabled(
            self, detect_format_mock, convert_image_mock,
            unlink_mock, rename_mock):
        CONF.set_override('force_raw_images', True)
        CONF.set_override('disable_deep_image_inspection', True,
                          group='conductor')
        info = mock.MagicMock()
        info.__str__.side_effect = iter(['vmdk', 'raw'])
        info.backing_file = None
        detect_format_mock.return_value = info

        def convert_side_effect(source, dest, out_format, source_format):
            info.file_format = 'raw'
        convert_image_mock.side_effect = convert_side_effect

        images.image_to_raw('image_href', 'path', 'path_tmp')
        info.safety_check.assert_not_called()
        detect_format_mock.assert_has_calls([
            mock.call('path')])
        self.assertEqual(2, info.__str__.call_count)
        convert_image_mock.assert_called_once_with('path_tmp',
                                                   'path.converted', 'raw',
                                                   source_format='vmdk')
        unlink_mock.assert_called_once_with('path_tmp')
        rename_mock.assert_called_once_with('path.converted', 'path')

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(os, 'unlink', autospec=True)
    @mock.patch.object(qemu_img, 'convert_image', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_safety_check_disabled_fails_to_convert(
            self, detect_format_mock, convert_image_mock,
            unlink_mock, rename_mock):
        CONF.set_override('force_raw_images', True)
        CONF.set_override('disable_deep_image_inspection', True,
                          group='conductor')
        info = mock.MagicMock()
        info.__str__.return_value = 'vmdk'
        info.backing_file = None
        detect_format_mock.return_value = info

        self.assertRaises(exception.ImageConvertFailed,
                          images.image_to_raw,
                          'image_href', 'path', 'path_tmp')
        info.safety_check.assert_not_called()
        self.assertEqual(2, info.__str__.call_count)
        detect_format_mock.assert_has_calls([
            mock.call('path')])
        convert_image_mock.assert_called_once_with('path_tmp',
                                                   'path.converted', 'raw',
                                                   source_format='vmdk')
        unlink_mock.assert_called_once_with('path_tmp')
        rename_mock.assert_not_called()

    @mock.patch.object(os, 'unlink', autospec=True)
    @mock.patch.object(qemu_img, 'convert_image', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_not_raw_after_conversion(self, detect_format_mock,
                                                   convert_image_mock,
                                                   unlink_mock):
        CONF.set_override('force_raw_images', True)
        info = mock.MagicMock()
        info.__str__.return_value = 'qcow2'
        detect_format_mock.return_value = info

        self.assertRaises(exception.ImageConvertFailed, images.image_to_raw,
                          'image_href', 'path', 'path_tmp')
        convert_image_mock.assert_called_once_with('path_tmp',
                                                   'path.converted', 'raw',
                                                   source_format='qcow2')
        unlink_mock.assert_called_once_with('path_tmp')
        info.safety_check.assert_called_once()
        self.assertEqual(2, info.__str__.call_count)
        detect_format_mock.assert_has_calls([
            mock.call('path_tmp'),
            mock.call('path.converted')])

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_already_raw_format(self, detect_format_mock,
                                             rename_mock):
        info = mock.MagicMock()
        info.__str__.return_value = 'raw'
        detect_format_mock.return_value = info

        images.image_to_raw('image_href', 'path', 'path_tmp')

        rename_mock.assert_called_once_with('path_tmp', 'path')
        info.safety_check.assert_called_once()
        self.assertEqual(1, info.__str__.call_count)
        detect_format_mock.assert_called_once_with('path_tmp')

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_already_gpt_format(self, detect_format_mock,
                                             rename_mock):
        info = mock.MagicMock()
        info.__str__.return_value = 'gpt'
        detect_format_mock.return_value = info

        images.image_to_raw('image_href', 'path', 'path_tmp')

        rename_mock.assert_called_once_with('path_tmp', 'path')
        info.safety_check.assert_called_once()
        self.assertEqual(1, info.__str__.call_count)
        detect_format_mock.assert_called_once_with('path_tmp')

    @mock.patch.object(os, 'rename', autospec=True)
    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_image_to_raw_already_iso(self, detect_format_mock,
                                      rename_mock):
        info = mock.MagicMock()
        info.__str__.return_value = 'iso'
        detect_format_mock.return_value = info

        images.image_to_raw('image_href', 'path', 'path_tmp')

        rename_mock.assert_called_once_with('path_tmp', 'path')
        info.safety_check.assert_called_once()
        self.assertEqual(1, info.__str__.call_count)
        detect_format_mock.assert_called_once_with('path_tmp')

    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    def test_image_show_no_image_service(self, image_service_mock):
        images.image_show('context', 'image_href')
        image_service_mock.assert_called_once_with('image_href',
                                                   context='context')
        image_service_mock.return_value.show.assert_called_once_with(
            'image_href')

    def test_image_show_image_service(self):
        image_service_mock = mock.MagicMock()
        images.image_show('context', 'image_href', image_service_mock)
        image_service_mock.show.assert_called_once_with('image_href')

    @mock.patch.object(images, 'image_show', autospec=True)
    def test_download_size(self, show_mock):
        show_mock.return_value = {'size': 123456}
        size = images.download_size('context', 'image_href', 'image_service',
                                    image_auth_data='meow')
        self.assertEqual(123456, size)
        show_mock.assert_called_once_with('context', 'image_href',
                                          image_service='image_service',
                                          image_auth_data='meow')

    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_converted_size_estimate_default(self, image_info_mock):
        info = self.FakeImgInfo()
        info.actual_size = 2
        info.virtual_size = 10 ** 10
        image_info_mock.return_value = info
        size = images.converted_size('path', estimate=True)
        image_info_mock.assert_called_once_with('path')
        self.assertEqual(4, size)

    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_converted_size_estimate_custom(self, image_info_mock):
        CONF.set_override('raw_image_growth_factor', 3)
        info = self.FakeImgInfo()
        info.actual_size = 2
        info.virtual_size = 10 ** 10
        image_info_mock.return_value = info
        size = images.converted_size('path', estimate=True)
        image_info_mock.assert_called_once_with('path')
        self.assertEqual(6, size)

    @mock.patch.object(images, 'detect_file_format', autospec=True)
    def test_converted_size_estimate_raw_smaller(self, image_info_mock):
        CONF.set_override('raw_image_growth_factor', 3)
        info = self.FakeImgInfo()
        info.actual_size = 2
        info.virtual_size = 5
        image_info_mock.return_value = info
        size = images.converted_size('path', estimate=True)
        image_info_mock.assert_called_once_with('path')
        self.assertEqual(5, size)

    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_no_img_src(self, mock_igi, mock_gip):
        instance_info = {'image_source': ''}
        iwdi = images.is_whole_disk_image('context', instance_info)
        self.assertIsNone(iwdi)
        self.assertFalse(mock_igi.called)
        self.assertFalse(mock_gip.called)

    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_explicit(self, mock_igi, mock_gip):
        for value, result in [(images.IMAGE_TYPE_PARTITION, False),
                              (images.IMAGE_TYPE_WHOLE_DISK, True)]:
            instance_info = {'image_source': 'glance://partition_image',
                             'image_type': value}
            iwdi = images.is_whole_disk_image('context', instance_info)
            self.assertIs(iwdi, result)
            self.assertFalse(mock_igi.called)
            self.assertFalse(mock_gip.called)

    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_partition_image(self, mock_igi, mock_gip):
        mock_igi.return_value = True
        mock_gip.return_value = {'kernel_id': 'kernel',
                                 'ramdisk_id': 'ramdisk'}
        instance_info = {'image_source': 'glance://partition_image'}
        image_source = instance_info['image_source']
        is_whole_disk_image = images.is_whole_disk_image('context',
                                                         instance_info)
        self.assertFalse(is_whole_disk_image)
        mock_igi.assert_called_once_with(image_source)
        mock_gip.assert_called_once_with('context', image_source)

    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_partition_image_with_type(self, mock_igi,
                                                           mock_gip):
        mock_igi.return_value = True
        mock_gip.return_value = {'img_type': images.IMAGE_TYPE_PARTITION}
        instance_info = {'image_source': 'glance://partition_image'}
        image_source = instance_info['image_source']
        is_whole_disk_image = images.is_whole_disk_image('context',
                                                         instance_info)
        self.assertFalse(is_whole_disk_image)
        mock_igi.assert_called_once_with(image_source)
        mock_gip.assert_called_once_with('context', image_source)

    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_whole_disk_image(self, mock_igi, mock_gip):
        mock_igi.return_value = True
        mock_gip.return_value = {}
        instance_info = {'image_source': 'glance://whole_disk_image'}
        image_source = instance_info['image_source']
        is_whole_disk_image = images.is_whole_disk_image('context',
                                                         instance_info)
        self.assertTrue(is_whole_disk_image)
        mock_igi.assert_called_once_with(image_source)
        mock_gip.assert_called_once_with('context', image_source)

    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(image_service, 'is_container_registry_url',
                       autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_whole_disk_image_oci(self, mock_igi,
                                                      mock_ioi,
                                                      mock_gip):
        mock_igi.return_value = False
        mock_ioi.return_value = True
        mock_gip.return_value = {}
        instance_info = {'image_source': 'oci://image'}
        image_source = instance_info['image_source']
        is_whole_disk_image = images.is_whole_disk_image('context',
                                                         instance_info)
        self.assertTrue(is_whole_disk_image)
        mock_igi.assert_called_once_with(image_source)
        mock_ioi.assert_called_once_with(image_source)
        mock_gip.assert_not_called()

    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_partition_non_glance(self, mock_igi,
                                                      mock_gip):
        mock_igi.return_value = False
        instance_info = {
            'image_source': 'fcf5a777-d9d2-4b86-b3da-bb0b61d5a291',
            'kernel': 'kernel',
            'ramdisk': 'ramdisk'}
        is_whole_disk_image = images.is_whole_disk_image('context',
                                                         instance_info)
        self.assertFalse(is_whole_disk_image)
        self.assertFalse(mock_gip.called)
        mock_igi.assert_called_once_with(instance_info['image_source'])

    @mock.patch.object(image_service.HttpImageService, 'validate_href',
                       autospec=True)
    @mock.patch.object(images, 'get_image_properties', autospec=True)
    @mock.patch.object(glance_utils, 'is_glance_image', autospec=True)
    def test_is_whole_disk_image_whole_disk_non_glance(self, mock_igi,
                                                       mock_gip,
                                                       mock_validate):
        mock_igi.return_value = False
        instance_info = {
            'image_source': 'http://whole-disk-image'}
        is_whole_disk_image = images.is_whole_disk_image('context',
                                                         instance_info)
        self.assertTrue(is_whole_disk_image)
        self.assertFalse(mock_gip.called)
        mock_igi.assert_called_once_with(instance_info['image_source'])
        mock_validate.assert_called_once_with(mock.ANY,
                                              'http://whole-disk-image')

    def test_is_source_a_path_returns_none(self):
        self.assertIsNone(images.is_source_a_path('context', {}))

    @mock.patch.object(image_service.HttpImageService, 'validate_href',
                       autospec=True)
    def test_is_source_a_path_simple(self, validate_mock):
        mock_response = mock.Mock()
        mock_response.headers = {}
        validate_mock.return_value = mock_response
        self.assertTrue(images.is_source_a_path('context', 'http://foo/bar/'))
        validate_mock.assert_called_once_with(mock.ANY, 'http://foo/bar/')

    @mock.patch.object(image_service.HttpImageService, 'validate_href',
                       autospec=True)
    def test_is_source_a_path_content_length(self, validate_mock):
        mock_response = mock.Mock()
        mock_response.headers = {'Content-Length': 1,
                                 'Content-Type': 'text/plain'}
        validate_mock.return_value = mock_response
        self.assertFalse(images.is_source_a_path('context', 'http://foo/bar/'))
        validate_mock.assert_called_once_with(mock.ANY, 'http://foo/bar/')

    @mock.patch.object(image_service.HttpImageService, 'validate_href',
                       autospec=True)
    def test_is_source_a_path_content_type(self, validate_mock):
        mock_response = mock.Mock()
        mock_response.headers = {'Content-Type': 'text/html'}
        validate_mock.return_value = mock_response
        self.assertTrue(images.is_source_a_path('context', 'http://foo/bar'))
        validate_mock.assert_called_once_with(mock.ANY, 'http://foo/bar')

    @mock.patch.object(images, 'LOG', autospec=True)
    @mock.patch.object(image_service.HttpImageService, 'validate_href',
                       autospec=True)
    def test_is_source_a_path_redirect(self, validate_mock, log_mock):
        url = 'http://foo/bar'
        redirect_url = url + '/'
        validate_mock.side_effect = exception.ImageRefIsARedirect(
            url, redirect_url)
        self.assertTrue(images.is_source_a_path('context', url))
        log_mock.debug.assert_called_once_with('Received a URL redirect when '
                                               'attempting to evaluate image '
                                               'reference http://foo/bar '
                                               'pointing to http://foo/bar/. '
                                               'This may, or may not be '
                                               'recoverable.')

    @mock.patch.object(image_service.HttpImageService, 'validate_href',
                       autospec=True)
    def test_is_source_a_path_other_error(self, validate_mock):
        url = 'http://foo/bar'
        validate_mock.side_effect = OSError
        self.assertIsNone(images.is_source_a_path('context', url))

    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(shutil, 'move', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test__hanlde_zstd_compression(self, mock_open, mock_move,
                                      mock_exec):
        mock_file_handle = mock.Mock()
        mock_file_handle.read.return_value = b"\x28\xb5\x2f\xfd"
        mock_open.return_value.__enter__.open = mock_file_handle
        images._handle_zstd_compression('path')
        mock_move.assert_called_once_with('path', 'path.zstd')
        mock_exec.assert_called_once_with('zstd', '-d', '--rm', 'path.zstd')

    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(shutil, 'move', autospec=True)
    @mock.patch.object(builtins, 'open', autospec=True)
    def test__hanlde_zstd_compression_disabled(self, mock_open, mock_move,
                                               mock_exec):
        mock_file_handle = mock.Mock()
        mock_file_handle.read.return_value = b"\x28\xb5\x2f\xfd"
        mock_open.return_value.__enter__.open = mock_file_handle
        CONF.set_override('disable_zstandard_decompression', True,
                          group='conductor')
        images._handle_zstd_compression('path')
        mock_move.assert_not_called()
        mock_exec.assert_not_called()


class ImageDetectFileFormatTestCase(base.TestCase):

    def setUp(self):
        super().setUp()

        read_patcher = mock.patch.object(
            format_inspector.InspectWrapper, "read", autospec=True)
        read_patcher.start()
        self.addCleanup(read_patcher.stop)

        formats_patcher = mock.patch.object(
            format_inspector.InspectWrapper, "formats",
            new=mock.PropertyMock(),
        )
        self.mock_formats = formats_patcher.start()
        self.addCleanup(formats_patcher.stop)

        format_patcher = mock.patch.object(
            format_inspector.InspectWrapper, "format",
            new=mock.PropertyMock(),
        )
        self.mock_format = format_patcher.start()
        self.addCleanup(format_patcher.stop)

        mock_file = mock.Mock()
        mock_file.peek.return_value = True
        open_patcher = mock.patch.object(builtins, 'open', autospec=True)
        self.mock_open = open_patcher.start()
        self.mock_open.return_value.__enter__.open = mock_file
        self.addCleanup(open_patcher.stop)

    def test_detect_file_format_passes(self):
        self.mock_format.return_value = "spam"
        self.mock_formats.side_effect = [None, [], ["spam"]]

        self.assertEqual("spam", images.detect_file_format("foo"))
        self.mock_open.assert_called_once_with("foo", "rb")
        self.assertEqual(3, self.mock_formats.call_count)

    def test_detect_file_format_fails_multiple(self):
        self.mock_formats.return_value = ["spam", "ham"]
        self.mock_format.side_effect = format_inspector.ImageFormatError()
        self.assertRaises(format_inspector.ImageFormatError,
                          images.detect_file_format, "foo")
        self.mock_open.assert_called_once_with("foo", "rb")

    def test_detect_file_format_passes_iso_gpt(self):
        self.mock_formats.return_value = ["gpt", "iso"]
        self.mock_format.side_effect = format_inspector.ImageFormatError()
        self.assertEqual("iso", images.detect_file_format("foo"))
        self.mock_open.assert_called_once_with("foo", "rb")


class FsImageTestCase(base.TestCase):

    @mock.patch.object(builtins, 'open', autospec=True)
    @mock.patch.object(shutil, 'copyfile', autospec=True)
    @mock.patch.object(os, 'makedirs', autospec=True)
    def test__create_root_fs(self, mkdir_mock, cp_mock, open_mock):
        files_info = {
            'a1': 'b1',
            'a2': 'b2',
            'a3': 'sub_dir/b3',
            b'a4': 'b4'}

        images._create_root_fs('root_dir', files_info)

        cp_mock.assert_any_call('a1', 'root_dir/b1')
        cp_mock.assert_any_call('a2', 'root_dir/b2')
        cp_mock.assert_any_call('a3', 'root_dir/sub_dir/b3')

        open_mock.assert_called_once_with('root_dir/b4', 'wb')
        fp = open_mock.return_value.__enter__.return_value
        fp.write.assert_called_once_with(b'a4')

        mkdir_mock.assert_any_call('root_dir', exist_ok=True)
        mkdir_mock.assert_any_call('root_dir/sub_dir', exist_ok=True)

    @mock.patch.object(os, 'listdir', autospec=True)
    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(utils, 'write_to_file', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_vfat_image(
            self, execute_mock, write_mock,
            tempdir_mock, create_root_fs_mock, os_listdir_mock):

        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = '/tempdir'
        tempdir_mock.return_value = mock_file_handle

        parameters = {'p1': 'v1'}
        files_info = {'a': 'b'}
        os_listdir_mock.return_value = ['b', 'qwe']
        images.create_vfat_image('tgt_file', parameters=parameters,
                                 files_info=files_info, parameters_file='qwe',
                                 fs_size_kib=1000)

        execute_mock.assert_has_calls([
            mock.call('dd', 'if=/dev/zero', 'of=tgt_file', 'count=1',
                      'bs=1000KiB'),
            mock.call('mkfs', '-t', 'vfat', '-n', 'ir-vfd-dev', 'tgt_file'),
            mock.call('mcopy', '-s', '/tempdir/b', '/tempdir/qwe', '-i',
                      'tgt_file', '::')
        ])

        parameters_file_path = os.path.join('/tempdir', 'qwe')
        write_mock.assert_called_once_with(parameters_file_path, 'p1=v1')
        create_root_fs_mock.assert_called_once_with('/tempdir', files_info)
        os_listdir_mock.assert_called_once_with('/tempdir')

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_vfat_image_dd_fails(self, execute_mock):

        execute_mock.side_effect = processutils.ProcessExecutionError
        self.assertRaises(exception.ImageCreationFailed,
                          images.create_vfat_image, 'tgt_file')

    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_vfat_image_mkfs_fails(self, execute_mock,
                                          tempdir_mock):

        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tempdir'
        tempdir_mock.return_value = mock_file_handle

        execute_mock.side_effect = [None, processutils.ProcessExecutionError]
        self.assertRaises(exception.ImageCreationFailed,
                          images.create_vfat_image, 'tgt_file')

    def test__generate_isolinux_cfg(self):

        kernel_params = ['key1=value1', 'key2']
        options = {'kernel': '/vmlinuz', 'ramdisk': '/initrd'}
        expected_cfg = ("default boot\n"
                        "\n"
                        "label boot\n"
                        "kernel /vmlinuz\n"
                        "append initrd=/initrd text key1=value1 key2 --")
        cfg = images._generate_cfg(kernel_params,
                                   CONF.isolinux_config_template,
                                   options)
        self.assertEqual(expected_cfg, cfg)

    def test__generate_grub_cfg(self):
        kernel_params = ['key1=value1', 'key2']
        options = {'linux': '/vmlinuz', 'initrd': '/initrd'}
        expected_cfg = ("set default=0\n"
                        "set timeout=5\n"
                        "set hidden_timeout_quiet=false\n"
                        "\n"
                        "menuentry \"boot_partition\" {\n"
                        "linux /vmlinuz key1=value1 key2 --\n"
                        "initrd /initrd\n"
                        "}")

        cfg = images._generate_cfg(kernel_params,
                                   CONF.grub_config_template,
                                   options)
        self.assertEqual(expected_cfg, cfg)

    @mock.patch.object(os.path, 'relpath', autospec=True)
    @mock.patch.object(os, 'walk', autospec=True)
    @mock.patch.object(images, '_extract_iso', autospec=True)
    def test__get_deploy_iso_files(self, extract_mock,
                                   walk_mock, relpath_mock):
        walk_mock.return_value = [('/tmpdir1/EFI/ubuntu', [], ['grub.cfg']),
                                  ('/tmpdir1/isolinux', [],
                                   ['efiboot.img', 'isolinux.bin',
                                    'isolinux.cfg'])]
        relpath_mock.side_effect = ['EFI/ubuntu/grub.cfg',
                                    'isolinux/efiboot.img']

        images._get_deploy_iso_files('path/to/deployiso', 'tmpdir1')
        extract_mock.assert_called_once_with('path/to/deployiso', 'tmpdir1')
        walk_mock.assert_called_once_with('tmpdir1')

    @mock.patch.object(shutil, 'rmtree', autospec=True)
    @mock.patch.object(os.path, 'relpath', autospec=True)
    @mock.patch.object(os, 'walk', autospec=True)
    @mock.patch.object(images, '_extract_iso', autospec=True)
    def test__get_deploy_iso_files_fail_no_esp_imageimg(
            self, extract_mock, walk_mock, relpath_mock, rmtree_mock):
        walk_mock.return_value = [('/tmpdir1/EFI/ubuntu', [], ['grub.cfg']),
                                  ('/tmpdir1/isolinux', [],
                                   ['isolinux.bin', 'isolinux.cfg'])]
        relpath_mock.side_effect = 'EFI/ubuntu/grub.cfg'

        self.assertRaises(exception.ImageCreationFailed,
                          images._get_deploy_iso_files,
                          'path/to/deployiso', 'tmpdir1')
        extract_mock.assert_called_once_with('path/to/deployiso', 'tmpdir1')
        walk_mock.assert_called_once_with('tmpdir1')
        rmtree_mock.assert_called_once_with('tmpdir1')

    @mock.patch.object(shutil, 'rmtree', autospec=True)
    @mock.patch.object(os.path, 'relpath', autospec=True)
    @mock.patch.object(os, 'walk', autospec=True)
    @mock.patch.object(images, '_extract_iso', autospec=True)
    def test__get_deploy_iso_files_fails_no_grub_cfg(
            self, extract_mock, walk_mock, relpath_mock, rmtree_mock):
        walk_mock.return_value = [('/tmpdir1/EFI/ubuntu', '', []),
                                  ('/tmpdir1/isolinux', '',
                                   ['efiboot.img', 'isolinux.bin',
                                    'isolinux.cfg'])]
        relpath_mock.side_effect = 'isolinux/efiboot.img'

        self.assertRaises(exception.ImageCreationFailed,
                          images._get_deploy_iso_files,
                          'path/to/deployiso', 'tmpdir1')
        extract_mock.assert_called_once_with('path/to/deployiso', 'tmpdir1')
        walk_mock.assert_called_once_with('tmpdir1')
        rmtree_mock.assert_called_once_with('tmpdir1')

    def test__get_deploy_iso_files_fail_with_ExecutionError(self):
        self.assertRaisesRegex(exception.ImageCreationFailed,
                               'No such file or directory',
                               images._get_deploy_iso_files,
                               'path/to/deployiso', 'tmpdir1')

    @mock.patch.object(shutil, 'rmtree', autospec=True)
    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'write_to_file', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(images, '_get_deploy_iso_files', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(images, '_generate_cfg', autospec=True)
    def test_create_esp_image_for_uefi_with_deploy_iso(
            self, gen_cfg_mock, tempdir_mock, get_iso_files_mock, execute_mock,
            write_to_file_mock, create_root_fs_mock, rmtree_mock):

        files_info = {
            'path/to/kernel': 'vmlinuz',
            'path/to/ramdisk': 'initrd',
            'sourceabspath/to/efiboot.img': 'path/to/efiboot.img',
            'path/to/grub': 'relpath/to/grub.cfg'
        }

        grubcfg = "grubcfg"
        grub_file = 'tmpdir/relpath/to/grub.cfg'
        gen_cfg_mock.side_effect = (grubcfg,)

        params = ['a=b', 'c']
        grub_options = {'linux': '/vmlinuz',
                        'initrd': '/initrd'}

        uefi_path_info = {
            'sourceabspath/to/efiboot.img': 'path/to/efiboot.img',
            'path/to/grub': 'relpath/to/grub.cfg'}
        grub_rel_path = 'relpath/to/grub.cfg'
        e_img_rel_path = 'path/to/efiboot.img'
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        mock_file_handle1 = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle1.__enter__.return_value = 'mountdir'
        tempdir_mock.side_effect = mock_file_handle, mock_file_handle1
        get_iso_files_mock.return_value = (uefi_path_info,
                                           e_img_rel_path, grub_rel_path)

        images.create_esp_image_for_uefi('tgt_file',
                                         'path/to/kernel',
                                         'path/to/ramdisk',
                                         deploy_iso='path/to/deploy_iso',
                                         kernel_params=params,
                                         publisher_id='1-23-4')
        get_iso_files_mock.assert_called_once_with('path/to/deploy_iso',
                                                   'mountdir')
        create_root_fs_mock.assert_called_once_with('tmpdir', files_info)
        gen_cfg_mock.assert_any_call(params, CONF.grub_config_template,
                                     grub_options)
        write_to_file_mock.assert_any_call(grub_file, grubcfg)
        execute_mock.assert_called_once_with(
            'mkisofs', '-r', '-V', 'VMEDIA_BOOT_ISO', '-l',
            '-publisher', '1-23-4', '-e',
            'path/to/efiboot.img', '-no-emul-boot', '-o', 'tgt_file', 'tmpdir')
        rmtree_mock.assert_called_once_with('mountdir')

    @mock.patch.object(utils, 'write_to_file', autospec=True)
    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(images, '_generate_cfg', autospec=True)
    def test_create_esp_image_for_uefi_with_esp_image(
            self, gen_cfg_mock, tempdir_mock, execute_mock,
            create_root_fs_mock, write_to_file_mock):

        files_info = {
            'path/to/kernel': 'vmlinuz',
            'path/to/ramdisk': 'initrd',
            'sourceabspath/to/efiboot.img': 'boot/grub/efiboot.img',
            '/dev/null': 'EFI/MYBOOT/grub.cfg',
        }

        grub_cfg_file = '/EFI/MYBOOT/grub.cfg'
        CONF.set_override('grub_config_path', grub_cfg_file)
        grubcfg = "grubcfg"
        gen_cfg_mock.side_effect = (grubcfg,)

        params = ['a=b', 'c']
        grub_options = {'linux': '/vmlinuz',
                        'initrd': '/initrd'}

        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        mock_file_handle1 = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle1.__enter__.return_value = 'mountdir'
        tempdir_mock.side_effect = mock_file_handle, mock_file_handle1
        mountdir_grub_cfg_path = 'tmpdir' + grub_cfg_file

        images.create_esp_image_for_uefi(
            'tgt_file', 'path/to/kernel', 'path/to/ramdisk',
            esp_image='sourceabspath/to/efiboot.img',
            kernel_params=params)
        create_root_fs_mock.assert_called_once_with('tmpdir', files_info)
        gen_cfg_mock.assert_any_call(params, CONF.grub_config_template,
                                     grub_options)
        write_to_file_mock.assert_any_call(mountdir_grub_cfg_path, grubcfg)
        execute_mock.assert_called_once_with(
            'mkisofs', '-r', '-V', 'VMEDIA_BOOT_ISO', '-l', '-e',
            'boot/grub/efiboot.img', '-no-emul-boot', '-o', 'tgt_file',
            'tmpdir')

    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'write_to_file', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(images, '_generate_cfg', autospec=True)
    def _test_create_isolinux_image_for_bios(
            self, gen_cfg_mock, execute_mock, tempdir_mock,
            write_to_file_mock, create_root_fs_mock, ldlinux_path=None,
            inject_files=None):

        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle

        cfg = "cfg"
        cfg_file = 'tmpdir/isolinux/isolinux.cfg'
        gen_cfg_mock.return_value = cfg

        params = ['a=b', 'c']
        isolinux_options = {'kernel': '/vmlinuz',
                            'ramdisk': '/initrd'}

        images.create_isolinux_image_for_bios('tgt_file',
                                              'path/to/kernel',
                                              'path/to/ramdisk',
                                              kernel_params=params,
                                              inject_files=inject_files,
                                              publisher_id='1-23-4')

        files_info = {
            'path/to/kernel': 'vmlinuz',
            'path/to/ramdisk': 'initrd',
            CONF.isolinux_bin: 'isolinux/isolinux.bin'
        }
        if inject_files:
            files_info.update(inject_files)
        if ldlinux_path:
            files_info[ldlinux_path] = 'isolinux/ldlinux.c32'
        create_root_fs_mock.assert_called_once_with('tmpdir', files_info)
        gen_cfg_mock.assert_called_once_with(params,
                                             CONF.isolinux_config_template,
                                             isolinux_options)
        write_to_file_mock.assert_called_once_with(cfg_file, cfg)
        execute_mock.assert_called_once_with(
            'mkisofs', '-r', '-V',
            "VMEDIA_BOOT_ISO", '-J', '-l',
            '-publisher', '1-23-4',
            '-no-emul-boot', '-boot-load-size',
            '4', '-boot-info-table', '-b', 'isolinux/isolinux.bin',
            '-o', 'tgt_file', 'tmpdir')

    @mock.patch.object(os.path, 'isfile', autospec=True)
    def test_create_isolinux_image_for_bios(self, mock_isfile):
        mock_isfile.return_value = False
        self._test_create_isolinux_image_for_bios()

    def test_create_isolinux_image_for_bios_conf_ldlinux(self):
        CONF.set_override('ldlinux_c32', 'path/to/ldlinux.c32')
        self._test_create_isolinux_image_for_bios(
            ldlinux_path='path/to/ldlinux.c32')

    @mock.patch.object(os.path, 'isfile', autospec=True)
    def test_create_isolinux_image_for_bios_default_ldlinux(self, mock_isfile):
        mock_isfile.side_effect = [False, True]
        self._test_create_isolinux_image_for_bios(
            ldlinux_path='/usr/share/syslinux/ldlinux.c32')

    @mock.patch.object(os.path, 'isfile', autospec=True)
    def test_create_isolinux_image_for_bios_inject_files(self, mock_isfile):
        mock_isfile.return_value = False
        self._test_create_isolinux_image_for_bios(
            inject_files={'/source': 'target'})

    @mock.patch.object(images, '_extract_iso', autospec=True)
    @mock.patch.object(shutil, 'rmtree', autospec=True)
    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(os, 'walk', autospec=True)
    def test_create_esp_image_uefi_rootfs_fails(
            self, walk_mock, utils_mock, tempdir_mock,
            create_root_fs_mock, rmtree_mock, extract_mock):

        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        mock_file_handle1 = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle1.__enter__.return_value = 'mountdir'
        tempdir_mock.side_effect = mock_file_handle, mock_file_handle1
        create_root_fs_mock.side_effect = IOError

        self.assertRaises(exception.ImageCreationFailed,
                          images.create_esp_image_for_uefi,
                          'tgt_file',
                          'path/to/kernel',
                          'path/to/ramdisk',
                          deploy_iso='path/to/deployiso')
        rmtree_mock.assert_called_once_with('mountdir')

    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(os, 'walk', autospec=True)
    def test_create_isolinux_image_bios_rootfs_fails(self, walk_mock,
                                                     utils_mock,
                                                     tempdir_mock,
                                                     create_root_fs_mock):
        create_root_fs_mock.side_effect = IOError

        self.assertRaises(exception.ImageCreationFailed,
                          images.create_isolinux_image_for_bios,
                          'tgt_file', 'path/to/kernel',
                          'path/to/ramdisk')

    @mock.patch.object(shutil, 'rmtree', autospec=True)
    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'write_to_file', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(images, '_get_deploy_iso_files', autospec=True)
    @mock.patch.object(images, '_generate_cfg', autospec=True)
    def test_create_esp_image_mkisofs_fails(
            self, gen_cfg_mock, get_iso_files_mock, utils_mock, tempdir_mock,
            write_to_file_mock, create_root_fs_mock, rmtree_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        mock_file_handle1 = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle1.__enter__.return_value = 'mountdir'
        tempdir_mock.side_effect = mock_file_handle, mock_file_handle1
        get_iso_files_mock.return_value = ({'a': 'a'}, 'b', 'c')
        utils_mock.side_effect = processutils.ProcessExecutionError

        self.assertRaises(exception.ImageCreationFailed,
                          images.create_esp_image_for_uefi,
                          'tgt_file',
                          'path/to/kernel',
                          'path/to/ramdisk',
                          deploy_iso='path/to/deployiso')
        rmtree_mock.assert_called_once_with('mountdir')

    @mock.patch.object(images, '_create_root_fs', autospec=True)
    @mock.patch.object(utils, 'write_to_file', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(images, '_generate_cfg', autospec=True)
    def test_create_isolinux_image_bios_mkisofs_fails(self,
                                                      gen_cfg_mock,
                                                      utils_mock,
                                                      tempdir_mock,
                                                      write_to_file_mock,
                                                      create_root_fs_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle
        utils_mock.side_effect = processutils.ProcessExecutionError

        self.assertRaises(exception.ImageCreationFailed,
                          images.create_isolinux_image_for_bios,
                          'tgt_file', 'path/to/kernel',
                          'path/to/ramdisk')

    @mock.patch.object(images, 'create_esp_image_for_uefi', autospec=True)
    @mock.patch.object(images, 'fetch', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    def test_create_boot_iso_for_uefi_deploy_iso(
            self, tempdir_mock, fetch_images_mock, create_isolinux_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle

        images.create_boot_iso(
            'ctx', 'output_file', 'kernel-uuid',
            'ramdisk-uuid', deploy_iso_href='deploy_iso-uuid',
            root_uuid='root-uuid', kernel_params='kernel-params',
            boot_mode='uefi')

        fetch_images_mock.assert_any_call(
            'ctx', 'kernel-uuid', 'tmpdir/kernel')
        fetch_images_mock.assert_any_call(
            'ctx', 'ramdisk-uuid', 'tmpdir/ramdisk')
        fetch_images_mock.assert_any_call(
            'ctx', 'deploy_iso-uuid', 'tmpdir/iso')

        params = ['root=UUID=root-uuid', 'kernel-params']
        create_isolinux_mock.assert_called_once_with(
            'output_file', 'tmpdir/kernel', 'tmpdir/ramdisk',
            deploy_iso='tmpdir/iso',
            esp_image=None, kernel_params=params, inject_files=None,
            publisher_id=None)

    @mock.patch.object(images, 'create_esp_image_for_uefi', autospec=True)
    @mock.patch.object(images, 'fetch', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    def test_create_boot_iso_for_uefi_esp_image(
            self, tempdir_mock, fetch_images_mock, create_isolinux_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle

        images.create_boot_iso(
            'ctx', 'output_file', 'kernel-uuid',
            'ramdisk-uuid', esp_image_href='efiboot-uuid',
            root_uuid='root-uuid', kernel_params='kernel-params',
            boot_mode='uefi')

        fetch_images_mock.assert_any_call(
            'ctx', 'kernel-uuid', 'tmpdir/kernel')
        fetch_images_mock.assert_any_call(
            'ctx', 'ramdisk-uuid', 'tmpdir/ramdisk')
        fetch_images_mock.assert_any_call(
            'ctx', 'efiboot-uuid', 'tmpdir/esp')

        params = ['root=UUID=root-uuid', 'kernel-params']
        create_isolinux_mock.assert_called_once_with(
            'output_file', 'tmpdir/kernel', 'tmpdir/ramdisk',
            deploy_iso=None, esp_image='tmpdir/esp',
            kernel_params=params, inject_files=None,
            publisher_id=None)

    @mock.patch.object(images, 'create_esp_image_for_uefi', autospec=True)
    @mock.patch.object(images, 'fetch', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    def test_create_boot_iso_for_uefi_deploy_iso_for_hrefs(
            self, tempdir_mock, fetch_images_mock, create_isolinux_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle

        images.create_boot_iso(
            'ctx', 'output_file', 'http://kernel-href', 'http://ramdisk-href',
            deploy_iso_href='http://deploy_iso-href',
            root_uuid='root-uuid', kernel_params='kernel-params',
            boot_mode='uefi')

        expected_calls = [mock.call('ctx', 'http://kernel-href',
                                    'tmpdir/kernel'),
                          mock.call('ctx', 'http://ramdisk-href',
                                    'tmpdir/ramdisk'),
                          mock.call('ctx', 'http://deploy_iso-href',
                                    'tmpdir/iso')]
        fetch_images_mock.assert_has_calls(expected_calls)
        params = ['root=UUID=root-uuid', 'kernel-params']
        create_isolinux_mock.assert_called_once_with(
            'output_file', 'tmpdir/kernel', 'tmpdir/ramdisk',
            deploy_iso='tmpdir/iso',
            esp_image=None, kernel_params=params, inject_files=None,
            publisher_id=None)

    @mock.patch.object(images, 'create_esp_image_for_uefi', autospec=True)
    @mock.patch.object(images, 'fetch', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    def test_create_boot_iso_for_uefi_esp_image_for_hrefs(
            self, tempdir_mock, fetch_images_mock, create_isolinux_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle

        images.create_boot_iso(
            'ctx', 'output_file', 'http://kernel-href', 'http://ramdisk-href',
            esp_image_href='http://efiboot-href',
            root_uuid='root-uuid', kernel_params='kernel-params',
            boot_mode='uefi', publisher_id='1-23-4')

        expected_calls = [mock.call('ctx', 'http://kernel-href',
                                    'tmpdir/kernel'),
                          mock.call('ctx', 'http://ramdisk-href',
                                    'tmpdir/ramdisk'),
                          mock.call('ctx', 'http://efiboot-href',
                                    'tmpdir/esp')]
        fetch_images_mock.assert_has_calls(expected_calls)
        params = ['root=UUID=root-uuid', 'kernel-params']
        create_isolinux_mock.assert_called_once_with(
            'output_file', 'tmpdir/kernel', 'tmpdir/ramdisk',
            deploy_iso=None, esp_image='tmpdir/esp',
            kernel_params=params, inject_files=None,
            publisher_id='1-23-4')

    @mock.patch.object(images, 'create_isolinux_image_for_bios', autospec=True)
    @mock.patch.object(images, 'fetch', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    def test_create_boot_iso_for_bios(
            self, tempdir_mock, fetch_images_mock, create_isolinux_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle

        images.create_boot_iso('ctx', 'output_file', 'kernel-uuid',
                               'ramdisk-uuid', 'deploy_iso-uuid',
                               'efiboot-uuid', 'root-uuid',
                               'kernel-params', 'bios',
                               publisher_id='1-23-4')

        fetch_images_mock.assert_any_call(
            'ctx', 'kernel-uuid', 'tmpdir/kernel')
        fetch_images_mock.assert_any_call(
            'ctx', 'ramdisk-uuid', 'tmpdir/ramdisk')

        # Note (NobodyCam): the original assert asserted that fetch_images
        #                   was not called with parameters, this did not
        #                   work, So I instead assert that there were only
        #                   Two calls to the mock validating the above
        #                   asserts.
        self.assertEqual(2, fetch_images_mock.call_count)

        params = ['root=UUID=root-uuid', 'kernel-params']
        create_isolinux_mock.assert_called_once_with(
            'output_file', 'tmpdir/kernel', 'tmpdir/ramdisk',
            kernel_params=params, inject_files=None,
            publisher_id='1-23-4')

    @mock.patch.object(images, 'create_isolinux_image_for_bios', autospec=True)
    @mock.patch.object(images, 'fetch', autospec=True)
    @mock.patch.object(utils, 'tempdir', autospec=True)
    def test_create_boot_iso_for_bios_with_no_boot_mode(self, tempdir_mock,
                                                        fetch_images_mock,
                                                        create_isolinux_mock):
        mock_file_handle = mock.MagicMock(spec=io.BytesIO)
        mock_file_handle.__enter__.return_value = 'tmpdir'
        tempdir_mock.return_value = mock_file_handle

        images.create_boot_iso('ctx', 'output_file', 'kernel-uuid',
                               'ramdisk-uuid', 'deploy_iso-uuid',
                               'efiboot-uuid', 'root-uuid',
                               'kernel-params', None)

        fetch_images_mock.assert_any_call(
            'ctx', 'kernel-uuid', 'tmpdir/kernel')
        fetch_images_mock.assert_any_call(
            'ctx', 'ramdisk-uuid', 'tmpdir/ramdisk')

        params = ['root=UUID=root-uuid', 'kernel-params']
        create_isolinux_mock.assert_called_once_with(
            'output_file', 'tmpdir/kernel', 'tmpdir/ramdisk',
            kernel_params=params, inject_files=None,
            publisher_id=None)

    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    def test_get_glance_image_properties_no_such_prop(self,
                                                      image_service_mock):

        prop_dict = {'properties': {'p1': 'v1',
                                    'p2': 'v2'}}

        image_service_obj_mock = image_service_mock.return_value
        image_service_obj_mock.show.return_value = prop_dict

        ret_val = images.get_image_properties('con', 'uuid',
                                              ['p1', 'p2', 'p3'])
        image_service_mock.assert_called_once_with('uuid', context='con')
        image_service_obj_mock.show.assert_called_once_with('uuid')
        self.assertEqual({'p1': 'v1',
                          'p2': 'v2',
                          'p3': None}, ret_val)

    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    def test_get_glance_image_properties_default_all(
            self, image_service_mock):

        prop_dict = {'properties': {'p1': 'v1',
                                    'p2': 'v2'}}

        image_service_obj_mock = image_service_mock.return_value
        image_service_obj_mock.show.return_value = prop_dict

        ret_val = images.get_image_properties('con', 'uuid')
        image_service_mock.assert_called_once_with('uuid', context='con')
        image_service_obj_mock.show.assert_called_once_with('uuid')
        self.assertEqual({'p1': 'v1',
                          'p2': 'v2'}, ret_val)

    @mock.patch.object(image_service, 'get_image_service', autospec=True)
    def test_get_glance_image_properties_with_prop_subset(
            self, image_service_mock):

        prop_dict = {'properties': {'p1': 'v1',
                                    'p2': 'v2',
                                    'p3': 'v3'}}

        image_service_obj_mock = image_service_mock.return_value
        image_service_obj_mock.show.return_value = prop_dict

        ret_val = images.get_image_properties('con', 'uuid',
                                              ['p1', 'p3'])
        image_service_mock.assert_called_once_with('uuid', context='con')
        image_service_obj_mock.show.assert_called_once_with('uuid')
        self.assertEqual({'p1': 'v1',
                          'p3': 'v3'}, ret_val)

    @mock.patch.object(image_service, 'GlanceImageService', autospec=True)
    def test_get_temp_url_for_glance_image(self, image_service_mock):

        direct_url = 'swift+http://host/v1/AUTH_xx/con/obj'
        image_info = {'id': 'qwe', 'properties': {'direct_url': direct_url}}
        glance_service_mock = image_service_mock.return_value
        glance_service_mock.swift_temp_url.return_value = 'temp-url'
        glance_service_mock.show.return_value = image_info

        temp_url = images.get_temp_url_for_glance_image('context',
                                                        'glance_uuid')

        glance_service_mock.show.assert_called_once_with('glance_uuid')
        self.assertEqual('temp-url', temp_url)
