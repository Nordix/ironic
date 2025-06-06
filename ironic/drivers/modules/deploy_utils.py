# Copyright (c) 2012 NTT DOCOMO, INC.
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


import os
import re

from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import fileutils
from oslo_utils import strutils

from ironic.common import async_steps
from ironic.common import checksum_utils
from ironic.common import context
from ironic.common import exception
from ironic.common import faults
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _
from ironic.common import image_service
from ironic.common import images
from ironic.common import keystone
from ironic.common import metrics_utils
from ironic.common import states
from ironic.common import utils
from ironic.conductor import utils as manager_utils
from ironic.conf import CONF
from ironic.drivers.modules import boot_mode_utils
from ironic.drivers.modules import image_cache
from ironic.drivers import utils as driver_utils
from ironic import objects


LOG = logging.getLogger(__name__)

METRICS = metrics_utils.get_metrics_logger(__name__)

SUPPORTED_CAPABILITIES = {
    'boot_mode': ('bios', 'uefi'),
    'secure_boot': ('true', 'false'),
    'disk_label': ('msdos', 'gpt'),
}

# States related to rescue mode.
RESCUE_LIKE_STATES = (states.RESCUING, states.RESCUEWAIT, states.RESCUEFAIL,
                      states.UNRESCUING, states.UNRESCUEFAIL)

DISK_LAYOUT_PARAMS = ('root_gb', 'swap_mb', 'ephemeral_gb')


# All functions are called from deploy() directly or indirectly.
# They are split for stub-out.

_IRONIC_SESSION = None


def _get_ironic_session():
    global _IRONIC_SESSION
    if not _IRONIC_SESSION:
        _IRONIC_SESSION = keystone.get_session('service_catalog')
    return _IRONIC_SESSION


def get_ironic_api_url():
    """Resolve Ironic API endpoint

    either from config of from Keystone catalog.
    """
    adapter_opts = {'session': _get_ironic_session()}
    # NOTE(pas-ha) force 'none' auth plugin for noauth mode
    if CONF.auth_strategy != 'keystone':
        CONF.set_override('auth_type', 'none', group='service_catalog')
    adapter_opts['auth'] = keystone.get_auth('service_catalog')

    try:
        ironic_api = keystone.get_endpoint('service_catalog', **adapter_opts)
    except (exception.KeystoneFailure,
            exception.CatalogNotFound,
            exception.KeystoneUnauthorized) as e:
        raise exception.InvalidParameterValue(_(
            "Couldn't get the URL of the Ironic API service from the "
            "configuration file or keystone catalog. Keystone error: "
            "%s") % str(e))
    # NOTE: we should strip '/' from the end because it might be used in
    # hardcoded ramdisk script
    ironic_api = ironic_api.rstrip('/')
    return ironic_api


def rescue_or_deploy_mode(node):
    return ('rescue' if node.provision_state in RESCUE_LIKE_STATES
            else 'deploy')


def _replace_lines_in_file(path, regex_pattern, replacement):
    with open(path) as f:
        lines = f.readlines()

    compiled_pattern = re.compile(regex_pattern)
    with open(path, 'w') as f:
        for line in lines:
            line = compiled_pattern.sub(replacement, line)
            f.write(line)


def _replace_root_uuid(path, root_uuid):
    root = 'UUID=%s' % root_uuid
    pattern = r'(\(\(|\{\{) ROOT (\)\)|\}\})'
    _replace_lines_in_file(path, pattern, root)


def _replace_boot_line(path, boot_mode, is_whole_disk_image,
                       iscsi_boot=False, ramdisk_boot=False,
                       ipxe_enabled=False, anaconda_boot=False):
    if is_whole_disk_image:
        boot_disk_type = 'boot_whole_disk'
    elif iscsi_boot:
        boot_disk_type = 'boot_iscsi'
    elif ramdisk_boot:
        boot_disk_type = 'boot_ramdisk'
    elif anaconda_boot:
        boot_disk_type = 'boot_anaconda'
    else:
        boot_disk_type = 'boot_partition'

    if boot_mode == 'uefi' and not ipxe_enabled:
        pattern = '^((set )?default)=.*$'
        boot_line = '\\1=%s' % boot_disk_type
    else:
        pxe_cmd = 'goto' if ipxe_enabled else 'default'
        pattern = '^%s .*$' % pxe_cmd
        boot_line = '%s %s' % (pxe_cmd, boot_disk_type)

    _replace_lines_in_file(path, pattern, boot_line)


def _replace_disk_identifier(path, disk_identifier):
    pattern = r'(\(\(|\{\{) DISK_IDENTIFIER (\)\)|\}\})'
    _replace_lines_in_file(path, pattern, disk_identifier)


# NOTE(TheJulia): This should likely be migrated to pxe_utils.
# TODO(dtantsur): with the removal of netboot, root_uuid_or_disk_id and
# the logic of replacing ROOT can be dropped, while is_whole_disk_image can
# be renamed to something like netboot_fallback.
def switch_pxe_config(path, root_uuid_or_disk_id, boot_mode,
                      is_whole_disk_image, iscsi_boot=False,
                      ramdisk_boot=False, ipxe_enabled=False,
                      anaconda_boot=False):
    """Switch a pxe config from deployment mode to service mode.

    :param path: path to the pxe config file in tftpboot.
    :param root_uuid_or_disk_id: root uuid in case of partition image or
                                 disk_id in case of whole disk image.
    :param boot_mode: if boot mode is uefi or bios.
    :param is_whole_disk_image: if the image is a whole disk image or not.
    :param iscsi_boot: if boot is from an iSCSI volume or not.
    :param ramdisk_boot: if the boot is to be to a ramdisk configuration.
    :param ipxe_enabled: A default False boolean value to tell the method
                         if the caller is using iPXE.
    :param anaconda_boot: if the boot is to be to an anaconda configuration.
    """
    if (not (ramdisk_boot or anaconda_boot)
            and root_uuid_or_disk_id is not None):
        if not is_whole_disk_image:
            _replace_root_uuid(path, root_uuid_or_disk_id)
        else:
            _replace_disk_identifier(path, root_uuid_or_disk_id)

    _replace_boot_line(path, boot_mode, is_whole_disk_image, iscsi_boot,
                       ramdisk_boot, ipxe_enabled, anaconda_boot)


def check_for_missing_params(info_dict, error_msg, param_prefix=''):
    """Check for empty params in the provided dictionary.

    :param info_dict: The dictionary to inspect.
    :param error_msg: The error message to prefix before printing the
        information about missing parameters.
    :param param_prefix: Add this prefix to each parameter for error messages
    :raises: MissingParameterValue, if one or more parameters are
        empty in the provided dictionary.
    """
    missing_info = []
    for label, value in info_dict.items():
        if not value:
            missing_info.append(param_prefix + label)

    if missing_info:
        exc_msg = _("%(error_msg)s. Missing are: %(missing_info)s")
        raise exception.MissingParameterValue(
            exc_msg % {'error_msg': error_msg,
                       'missing_info': missing_info})


def fetch_images(ctx, cache, images_info, force_raw=True,
                 expected_format=None, expected_checksum=None,
                 expected_checksum_algo=None,
                 image_auth_data=None):
    """Check for available disk space and fetch images using ImageCache.

    :param ctx: context
    :param cache: ImageCache instance to use for fetching
    :param images_info: list of tuples (image href, destination path)
    :param force_raw: boolean value, whether to convert the image to raw
                      format
    :param expected_format: The expected format of the image.
    :param expected_checksum: The expected image checksum, to be used if we
           need to convert the image to raw prior to deploying.
    :param expected_checksum_algo: The checksum algo in use, if separately
           set.
    :raises: InstanceDeployFailure if unable to find enough disk space
    :raises: InvalidImage if the supplied image metadata or contents are
             deemed to be invalid, unsafe, or not matching the expectations
             asserted by configuration supplied or set.
    """

    try:
        image_cache.clean_up_caches(ctx, cache.master_dir, images_info,
                                    image_auth_data)
    except exception.InsufficientDiskSpace as e:
        raise exception.InstanceDeployFailure(reason=e)

    # NOTE(dtantsur): This code can suffer from race condition,
    # if disk space is used between the check and actual download.
    # This is probably unavoidable, as we can't control other
    # (probably unrelated) processes
    image_list = []
    for href, path in images_info:
        # NOTE(TheJulia): Href in this case can be an image UUID or a URL.
        image_format = cache.fetch_image(
            href, path, ctx=ctx,
            force_raw=force_raw,
            expected_format=expected_format,
            expected_checksum=expected_checksum,
            expected_checksum_algo=expected_checksum_algo,
            image_auth_data=image_auth_data)
        image_list.append((href, path, image_format))
    return image_list


def set_failed_state(task, msg, collect_logs=True):
    """Sets the deploy status as failed with relevant messages.

    This method sets the deployment as fail with the given message.
    It sets node's provision_state to DEPLOYFAIL and updates last_error
    with the given error message. It also powers off the baremetal node.

    :param task: a TaskManager instance containing the node to act on.
    :param msg: the message to set in logs and last_error of the node.
    :param collect_logs: Boolean indicating whether to attempt to collect
                         logs from IPA-based ramdisk. Defaults to True.
                         Actual log collection is also affected by
                         CONF.agent.deploy_logs_collect config option.
    """
    node = task.node

    if (collect_logs
            and CONF.agent.deploy_logs_collect in ('on_failure', 'always')):
        driver_utils.collect_ramdisk_logs(node)

    try:
        manager_utils.deploying_error_handler(task, msg, msg, clean_up=False)
    except exception.InvalidState:
        msg2 = ('Internal error. Node %(node)s in provision state '
                '"%(state)s" could not transition to a failed state.'
                % {'node': node.uuid, 'state': node.provision_state})
        LOG.exception(msg2)

    if CONF.deploy.power_off_after_deploy_failure:
        try:
            manager_utils.node_power_action(task, states.POWER_OFF)
        except Exception:
            msg2 = ('Node %s failed to power off while handling deploy '
                    'failure. This may be a serious condition. Node '
                    'should be removed from Ironic or put in maintenance '
                    'mode until the problem is resolved.' % node.uuid)
            LOG.exception(msg2)
    # NOTE(tenbrae): node_power_action() erases node.last_error
    #             so we need to set it here.
    node.last_error = msg
    node.save()


def get_single_nic_with_vif_port_id(task):
    """Returns the MAC address of a port which has a VIF port id.

    :param task: a TaskManager instance containing the ports to act on.
    :returns: MAC address of the port connected to deployment network.
              None if it cannot find any port with vif id.
    """
    # NOTE(vdrok): We are booting the node only in one network at a time,
    # and presence of cleaning_vif_port_id means we're doing cleaning, of
    # provisioning_vif_port_id - provisioning. Otherwise it's a tenant network
    for port in task.ports:
        if task.driver.network.get_current_vif(task, port):
            return port.address


def agent_add_clean_params(task):
    """Add required config parameters to node's driver_internal_info.

    Adds the required conf options to node's driver_internal_info.
    It is Required to pass the information to IPA.

    :param task: a TaskManager instance.
    """

    random_iterations = CONF.deploy.shred_random_overwrite_iterations
    node = task.node
    node.set_driver_internal_info('agent_erase_devices_iterations',
                                  random_iterations)
    zeroize = CONF.deploy.shred_final_overwrite_with_zeros
    node.set_driver_internal_info('agent_erase_devices_zeroize', zeroize)
    erase_fallback = CONF.deploy.continue_if_disk_secure_erase_fails
    node.set_driver_internal_info('agent_continue_if_secure_erase_failed',
                                  erase_fallback)
    # NOTE(janders) ``agent_continue_if_ata_erase_failed`` is deprecated and
    # will be removed in the "Y" cycle. The replacement option
    # ``agent_continue_if_secure_erase_failed`` is used to control shred
    #  fallback for both ATA Secure Erase and NVMe Secure Erase.
    # The ``agent_continue_if_ata_erase_failed`` line can
    # be deleted along with this comment when support for it is fully removed.
    node.set_driver_internal_info('agent_continue_if_ata_erase_failed',
                                  erase_fallback)
    nvme_secure_erase = CONF.deploy.enable_nvme_secure_erase
    node.set_driver_internal_info('agent_enable_nvme_secure_erase',
                                  nvme_secure_erase)
    secure_erase = CONF.deploy.enable_ata_secure_erase
    node.set_driver_internal_info('agent_enable_ata_secure_erase',
                                  secure_erase)
    node.set_driver_internal_info('disk_erasure_concurrency',
                                  CONF.deploy.disk_erasure_concurrency)
    node.set_driver_internal_info('agent_erase_skip_read_only',
                                  CONF.deploy.erase_skip_read_only)

    node.save()


def try_set_boot_device(task, device, persistent=True):
    """Tries to set the boot device on the node.

    This method tries to set the boot device on the node to the given
    boot device.  Under uefi boot mode, setting of boot device may differ
    between different machines. IPMI does not work for setting boot
    devices in uefi mode for certain machines.  This method ignores the
    expected IPMI failure for uefi boot mode and just logs a message.
    In error cases, it is expected the operator has to manually set the
    node to boot from the correct device.

    :param task: a TaskManager object containing the node
    :param device: the boot device
    :param persistent: Whether to set the boot device persistently
    :raises: Any exception from set_boot_device except IPMIFailure
        (setting of boot device using ipmi is expected to fail).
    """
    try:
        manager_utils.node_set_boot_device(task, device,
                                           persistent=persistent)
    except exception.IPMIFailure:
        with excutils.save_and_reraise_exception() as ctxt:
            if boot_mode_utils.get_boot_mode(task.node) == 'uefi':
                ctxt.reraise = False
                LOG.warning("ipmitool is unable to set boot device while "
                            "the node %s is in UEFI boot mode. Please set "
                            "the boot device manually.", task.node.uuid)


def get_disk_label(node):
    """Return the disk label requested for deploy, if any.

    :param node: a single Node.
    :raises: InvalidParameterValue if the capabilities string is not a
             dictionary or is malformed.
    :returns: the disk label or None if no disk label was specified.
    """
    capabilities = utils.parse_instance_info_capabilities(node)
    label = capabilities.get('disk_label')
    # NOTE(TheJulia): If the node is UEFI based, we should likely just default
    # the table type to gpt as otherwise we rely upon the user to supply the
    # right information, and for UEFI mode bios may work, but is wrong.
    if label is None and boot_mode_utils.get_boot_mode(node) == 'uefi':
        label = 'gpt'
    return label


def get_pxe_boot_file(node):
    """Return the PXE boot file name requested for deploy.

    This method returns PXE boot file name to be used for deploy.
    Architecture specific boot file is searched first. BIOS/UEFI
    boot file is used if no valid architecture specific file found.

    :param node: A single Node.
    :returns: The PXE boot file name.
    """
    cpu_arch = node.properties.get('cpu_arch')
    boot_file = CONF.pxe.pxe_bootfile_name_by_arch.get(cpu_arch)
    if boot_file is None:
        if boot_mode_utils.get_boot_mode(node) == 'uefi':
            boot_file = CONF.pxe.uefi_pxe_bootfile_name
        else:
            boot_file = CONF.pxe.pxe_bootfile_name

    return boot_file


def get_ipxe_boot_file(node):
    """Return the iPXE boot file name requested for deploy.

    This method returns iPXE boot file name to be used for deploy.
    Architecture specific boot file is searched first. BIOS/UEFI
    boot file is used if no valid architecture specific file found.

    If no valid value is found, the default reverts to the
    ``get_pxe_boot_file`` method and thus the
    ``[pxe]pxe_bootfile_name`` and ``[pxe]uefi_ipxe_bootfile_name``
    settings.

    :param node: A single Node.
    :returns: The iPXE boot file name.
    """
    cpu_arch = node.properties.get('cpu_arch')
    boot_file = CONF.pxe.ipxe_bootfile_name_by_arch.get(cpu_arch)
    if boot_file is None:
        if boot_mode_utils.get_boot_mode(node) == 'uefi':
            boot_file = CONF.pxe.uefi_ipxe_bootfile_name
        else:
            boot_file = CONF.pxe.ipxe_bootfile_name

    if boot_file is None:
        boot_file = get_pxe_boot_file(node)

    return boot_file


def get_ipxe_config_template(node):
    """Return the iPXE config template file name requested of deploy.

    This method returns the iPXE configuration template file.

    :param node: A single Node.
    :returns: The iPXE config template file name.
    """
    # NOTE(TheJulia): iPXE configuration files don't change based upon the
    # architecture and we're not trying to support multiple different boot
    # loaders by architecture as they are all consistent. Where as PXE
    # could need to be grub for one arch, PXELINUX for another.
    configured_template = CONF.pxe.ipxe_config_template
    override_template = node.driver_info.get('pxe_template')
    if override_template:
        configured_template = override_template
    return configured_template or get_pxe_config_template(node)


def get_pxe_config_template(node):
    """Return the PXE config template file name requested for deploy.

    This method returns PXE config template file to be used for deploy.
    First specific pxe template is searched in the node. After that
    architecture specific template file is searched. BIOS/UEFI template file
    is used if no valid architecture specific file found.

    :param node: A single Node.
    :returns: The PXE config template file name.
    """
    config_template = node.driver_info.get("pxe_template", None)
    if config_template is None:
        cpu_arch = node.properties.get('cpu_arch')
        config_template = CONF.pxe.pxe_config_template_by_arch.get(cpu_arch)
        if config_template is None:
            if boot_mode_utils.get_boot_mode(node) == 'uefi':
                config_template = CONF.pxe.uefi_pxe_config_template
            else:
                config_template = CONF.pxe.pxe_config_template

    return config_template


def validate_capabilities(node):
    """Validates that specified supported capabilities have valid value

    This method checks if the any of the supported capability is present in
    Node capabilities. For all supported capabilities specified for a Node,
    it validates that it has a valid value.
    The node can have capability as part of the 'properties' or
    'instance_info' or both.
    Note that the actual value of a capability does not need to be the same
    in the node's 'properties' and 'instance_info'.

    :param node: an ironic node object.
    :raises: InvalidParameterValue, if the capability is not set to a
        valid value.
    """
    exp_str = _("The parameter '%(capability)s' from %(field)s has an "
                "invalid value: '%(value)s'. Acceptable values are: "
                "%(valid_values)s.")

    for capability_name, valid_values in SUPPORTED_CAPABILITIES.items():
        # Validate capability_name in node's properties/capabilities
        value = driver_utils.get_node_capability(node, capability_name)
        if value and (value not in valid_values):
            field = "properties/capabilities"
            raise exception.InvalidParameterValue(
                exp_str %
                {'capability': capability_name, 'field': field,
                 'value': value, 'valid_values': ', '.join(valid_values)})

        # Validate capability_name in node's instance_info/['capabilities']
        capabilities = utils.parse_instance_info_capabilities(node)
        value = capabilities.get(capability_name)

        if value and (value not in valid_values):
            field = "instance_info['capabilities']"
            raise exception.InvalidParameterValue(
                exp_str %
                {'capability': capability_name, 'field': field,
                 'value': value, 'valid_values': ', '.join(valid_values)})


def get_image_properties(ctx, image_href):
    """Get properties of the image.

    :param ctx: security context
    :param image_href: reference to the image
    :return: properties as a dictionary
    :raises: InvalidParameterValue if the image cannot be accessed
    """
    try:
        img_service = image_service.get_image_service(image_href, context=ctx)
        return img_service.show(image_href)['properties']
    except (exception.GlanceConnectionFailed,
            exception.ImageNotAuthorized,
            exception.Invalid):
        raise exception.InvalidParameterValue(_(
            "Failed to connect to Glance to get the properties "
            "of the image %s") % image_href)
    except exception.ImageNotFound:
        raise exception.InvalidParameterValue(_(
            "Image %s can not be found.") % image_href)
    except exception.ImageRefValidationFailed as e:
        raise exception.InvalidParameterValue(err=e)


def validate_image_properties(task, deploy_info):
    """Validate the image.

    For Glance images it checks that the image exists in Glance and its
    properties or deployment info contain the properties passed. If it's not a
    Glance image, it checks that deployment info contains needed properties.

    :param task: TaskManager instance with a valid node
    :param deploy_info: the deploy_info to be validated
    :raises: InvalidParameterValue if:
        * connection to glance failed;
        * authorization for accessing image failed;
        * HEAD request to image URL failed or returned response code != 200;
        * HEAD request response does not contain Content-Length header;
        * the protocol specified in image URL is not supported.
    :raises: MissingParameterValue if the image doesn't contain
        the mentioned properties.
    """
    node = task.node
    image_href = deploy_info.get('image_source')
    boot_iso = deploy_info.get('boot_iso')
    isap = task.node.driver_internal_info.get('is_source_a_path')
    if image_href and boot_iso:
        raise exception.InvalidParameterValue(_(
            "An 'image_source' and 'boot_iso' parameter may not be "
            "specified at the same time."))
    if not image_href:
        image_href = boot_iso

    boot_option = get_boot_option(task.node)

    if (boot_iso
            or node.driver_internal_info.get('is_whole_disk_image')
            or boot_option == 'local'
            or isap):
        # No image properties are required in this case, but validate that the
        # image at least looks reasonable.
        try:
            # This doesn't actually test *getting* the defined url or file
            # but actually validates we can parse the data *to* connect.
            image_service.get_image_service(image_href, context=task.context)
        except exception.ImageRefValidationFailed as e:
            raise exception.InvalidParameterValue(err=e)
        if not isap:
            # If the source is not a path, but otherwise matches, we need to
            # exit validation here. Deployments, such as ramdisk or anaconda
            # need further parameter validation and this supplies it.
            return

    if service_utils.is_glance_image(image_href):
        properties = ['kernel_id', 'ramdisk_id']
        if boot_option == 'kickstart':
            properties.append('stage2_id')
        image_props = get_image_properties(task.context, image_href)
    else:
        # We are likely netbooting in this case...
        properties = ['kernel', 'ramdisk']
        image_props = {}

    missing_props = []
    for prop in properties:
        if not (deploy_info.get(prop) or image_props.get(prop)):
            missing_props.append(prop)

    if missing_props:
        props = ', '.join(missing_props)
        raise exception.MissingParameterValue(_(
            "Image %(image)s is missing the following properties: "
            "%(properties)s") % {'image': image_href, 'properties': props})


def get_boot_option(node):
    """Gets the boot option.

    :param node: A single Node.
    :raises: InvalidParameterValue if the capabilities string is not a
         dict or is malformed.
    :returns: A string representing the boot option type. Defaults to
        configuration setting [deploy]default_boot_mode.
    """
    if is_anaconda_deploy(node):
        return 'kickstart'
    if is_ramdisk_deploy(node):
        return 'ramdisk'
    return 'local'


# FIXME(dtantsur): relying on deploy interface name is an anti-pattern.
# Refactor the code so that the deploy interface itself provides the only boot
# option it supports.

def is_ramdisk_deploy(node):
    return node.get_interface('deploy') == 'ramdisk'


def is_anaconda_deploy(node):
    """Determine if Anaconda deploy interface is in use for the deployment.

    :param node: A single Node.
    :returns: A boolean value of True when Anaconda deploy interface is in use
              otherwise False
    """
    if node.get_interface('deploy') == 'anaconda':
        return True
    return False


def is_software_raid(node):
    """Determine if software raid is in use for the deployment.

    :param node: A single Node.
    :returns: A boolean value of True when software raid is in use,
              otherwise False
    """
    target_raid_config = node.target_raid_config
    logical_disks = target_raid_config.get('logical_disks', [])
    software_raid = False
    for logical_disk in logical_disks:
        if logical_disk.get('controller') == 'software':
            software_raid = True
            break
    return software_raid


IPA_URL_PARAM_NAME = 'ipa-api-url'


def build_agent_options(node):
    """Build the options to be passed to the agent ramdisk.

    :param node: an ironic node object
    :returns: a dictionary containing the parameters to be passed to
        agent ramdisk.
    """
    agent_config_opts = {
        IPA_URL_PARAM_NAME: get_ironic_api_url(),
    }
    return agent_config_opts


def prepare_inband_cleaning(task, manage_boot=True):
    """Prepares the node to boot into agent for in-band cleaning.

    This method does the following:
    1. Prepares the cleaning ports for the bare metal
    node and updates the clean parameters in node's driver_internal_info.
    2. If 'manage_boot' parameter is set to true, it also calls the
    'prepare_ramdisk' method of boot interface to boot the agent ramdisk.
    3. Reboots the bare metal node.

    :param task: a TaskManager object containing the node
    :param manage_boot: If this is set to True, this method calls the
                        'prepare_ramdisk' method of boot interface to boot the
                        agent ramdisk. If False, it skips preparing the boot
                        agent ramdisk using boot interface, and assumes that
                        the environment is setup to automatically boot agent
                        ramdisk every time bare metal node is rebooted.
    :returns: states.CLEANWAIT to signify an asynchronous prepare.
    :raises: NetworkError, NodeCleaningFailure if the previous cleaning ports
             cannot be removed or if new cleaning ports cannot be created.
    :raises: InvalidParameterValue if cleaning network UUID config option has
             an invalid value.
    """
    fast_track = manager_utils.is_fast_track(task)
    if not fast_track:
        power_state_to_restore = manager_utils.power_on_node_if_needed(task)

    # WARNING(TheJulia): When fast track is available, trying to plug the
    # cleaning network is problematic and in practice this may fail if
    # cleaning/provisioning/discovery all take place on different
    # networks when..
    # Translation: Here be a realistically unavoidable footgun
    # fast track support.
    # TODO(TheJulia): Lets improve this somehow such that the agent host
    # gracefully handles these sorts of changes.
    task.driver.network.add_cleaning_network(task)
    if not fast_track:
        manager_utils.restore_power_state_if_needed(
            task, power_state_to_restore)

    # Append required config parameters to node's driver_internal_info
    # to pass to IPA.
    agent_add_clean_params(task)

    if manage_boot:
        prepare_agent_boot(task)

    # NOTE(dtantsur): calling prepare_ramdisk may power off the node, so we
    # need to check fast-track again and reboot if needed.
    fast_track = manager_utils.is_fast_track(task)
    if not fast_track:
        manager_utils.node_power_action(task, states.REBOOT)
        # Tell the conductor we are waiting for the agent to boot.
        return states.CLEANWAIT


def tear_down_inband_cleaning(task, manage_boot=True):
    """Tears down the environment setup for in-band cleaning.

    This method does the following:
    1. Powers off the bare metal node (unless the node is fast
    tracked or there was a cleaning failure).
    2. If 'manage_boot' parameter is set to true, it also calls
    the 'clean_up_ramdisk' method of boot interface to clean
    up the environment that was set for booting agent ramdisk.
    3. Deletes the cleaning ports which were setup as part
    of cleaning.

    :param task: a TaskManager object containing the node
    :param manage_boot: If this is set to True, this method calls the
        'clean_up_ramdisk' method of boot interface to boot the agent
        ramdisk. If False, it skips this step.
    :raises: NetworkError, NodeCleaningFailure if the cleaning ports cannot be
        removed.
    """
    fast_track = manager_utils.is_fast_track(task)

    node = task.node
    cleaning_failure = (node.fault == faults.CLEAN_FAILURE)

    if not (fast_track or cleaning_failure or node.disable_power_off):
        manager_utils.node_power_action(task, states.POWER_OFF)

    if manage_boot:
        task.driver.boot.clean_up_ramdisk(task)

    power_state_to_restore = manager_utils.power_on_node_if_needed(task)
    task.driver.network.remove_cleaning_network(task)
    if not (fast_track or cleaning_failure):
        if node.disable_power_off:
            manager_utils.node_power_action(task, states.REBOOT)
        else:
            manager_utils.restore_power_state_if_needed(
                task, power_state_to_restore)


def prepare_inband_service(task):
    """Boot a service ramdisk on the node.

    :param task: a TaskManager instance.
    :raises: NetworkError if the tenant ports cannot be removed.
    :raises: InvalidParameterValue when the wrong power state is specified
         or the wrong driver info is specified for power management.
    :raises: other exceptions by the node's power driver if something
            wrong occurred during the power action.
    :raises: any boot interface's prepare_ramdisk exceptions.
    :returns: Returns states.SERVICEWAIT
    """
    with driver_utils.power_off_and_on(task):
        # NOTE(TheJulia): Revealing that the power is off at any time can
        # cause external power sync to decide that the node must be off.
        # This may result in a post-rescued instance being turned off
        # unexpectedly after rescue has started.
        # TODO(TheJulia): Once we have power/state callbacks to nova,
        # the reset of the power_state can be removed.
        task.node.power_state = states.POWER_ON
        task.node.save()

        task.driver.boot.clean_up_instance(task)
        with manager_utils.power_state_for_network_configuration(task):
            task.driver.network.unconfigure_tenant_networks(task)
            task.driver.network.add_servicing_network(task)
        if CONF.agent.manage_agent_boot:
            # prepare_ramdisk will set the boot device
            prepare_agent_boot(task)

    return states.SERVICEWAIT


def tear_down_inband_service(task):
    """Tears down the environment setup for in-band service.

    This method does the following:
    1. Powers off the bare metal node (unless the node is fast
    tracked or there was a service failure).
    2. If 'manage_boot' parameter is set to true, it also calls
    the 'clean_up_ramdisk' method of boot interface to clean
    up the environment that was set for booting agent ramdisk.
    3. Deletes the cleaning ports which were setup as part
    of cleaning.

    :param task: a TaskManager object containing the node
    :raises: NetworkError, NodeServiceFailure if the cleaning ports cannot be
        removed.
    """
    node = task.node
    service_failure = (node.fault == faults.SERVICE_FAILURE)

    if not service_failure and not node.disable_power_off:
        manager_utils.node_power_action(task, states.POWER_OFF)

    task.driver.boot.clean_up_ramdisk(task)

    if not service_failure:
        with manager_utils.power_state_for_network_configuration(task):
            task.driver.network.remove_servicing_network(task)
            task.driver.network.configure_tenant_networks(task)

        task.driver.boot.prepare_instance(task)
        # prepare_instance does not power on the node, the deploy interface is
        # normally responsible for that.
        next_state = (states.REBOOT if task.node.disable_power_off
                      else states.POWER_ON)
        manager_utils.node_power_action(task, next_state)


def get_image_instance_info(node):
    """Gets the image information from the node.

    Get image information for the given node instance from its
    'instance_info' property.

    :param node: a single Node.
    :returns: A dict with required image properties retrieved from
        node's 'instance_info'.
    :raises: MissingParameterValue, if image_source is missing in node's
        instance_info. Also raises same exception if kernel/ramdisk is
        missing in instance_info for non-glance images.
    """
    # TODO(TheJulia): We seem to have a lack of direct unit testing of this
    # method, but that is likely okay. If memory serves we test this at
    # a few different levels. That being said, it would be good for some
    # more explicit unit testing to exist.
    info = {}

    boot_iso = node.instance_info.get('boot_iso')
    image_source = node.instance_info.get('image_source')
    if boot_iso:
        if image_source:
            raise exception.InvalidParameterValue(_(
                "An 'image_source' and 'boot_iso' parameter may not be "
                "specified at the same time."))
        info['boot_iso'] = boot_iso
    else:
        boot_option = get_boot_option(node)
        if boot_option == 'ramdisk':
            # Ramdisk deploy does not require an image
            info['kernel'] = node.instance_info.get('kernel')
            info['ramdisk'] = node.instance_info.get('ramdisk')
        else:
            info['image_source'] = image_source

            is_whole_disk_image = node.driver_internal_info.get(
                'is_whole_disk_image')
            if (not is_whole_disk_image
                    and boot_option != 'local'
                    and not service_utils.is_glance_image(image_source)):
                info['kernel'] = node.instance_info.get('kernel')
                info['ramdisk'] = node.instance_info.get('ramdisk')

    error_msg = (_("Cannot validate image information for node %s because one "
                   "or more parameters are missing from its instance_info and "
                   "insufficient information is present to boot from a remote "
                   "volume")
                 % node.uuid)
    check_for_missing_params(info, error_msg)

    return info


_ERR_MSG_INVALID_DEPLOY = _("Invalid parameter %(param)s: %(reason)s")


def parse_instance_info(node, image_deploy=True):
    """Gets the instance specific Node deployment info.

    This method validates whether the 'instance_info' property of the
    supplied node contains the required information for this driver to
    deploy images to the node.

    :param node: a single Node.
    :param image_deploy: If the deployment interface is aware this
                         is an image based deployment, default
                         True.
    :returns: A dict with the instance_info values.
    :raises: MissingParameterValue, if any of the required parameters are
        missing.
    :raises: InvalidParameterValue, if any of the parameters have invalid
        value.
    """

    info = node.instance_info
    i_info = {}
    i_info['image_source'] = info.get('image_source')
    iwdi = node.driver_internal_info.get('is_whole_disk_image')
    boot_option = get_boot_option(node)
    if not iwdi:
        if (i_info['image_source']
                and boot_option != 'local'
                and not service_utils.is_glance_image(
                    i_info['image_source'])):
            i_info['kernel'] = info.get('kernel')
            i_info['ramdisk'] = info.get('ramdisk')
        if image_deploy:
            # root_gb is expected with partition images.
            i_info['root_gb'] = info.get('root_gb')

    # NOTE(TheJulia): Kernel/ramdisk may be optional and originated
    # with pure workload network booting.

    error_msg = _("Some parameters were missing in node's instance_info")
    check_for_missing_params(i_info, error_msg)

    # This is used in many places, so keep it even for whole-disk images.
    # There is also a potential use case of creating an ephemeral partition via
    # cloud-init and telling ironic to avoid metadata wipe via setting
    # preserve_ephemeral (not saying it will work, but it seems possible).
    preserve_ephemeral = info.get('preserve_ephemeral', False)
    try:
        i_info['preserve_ephemeral'] = (
            strutils.bool_from_string(preserve_ephemeral, strict=True))
    except ValueError as e:
        raise exception.InvalidParameterValue(
            _ERR_MSG_INVALID_DEPLOY % {'param': 'preserve_ephemeral',
                                       'reason': e})

    if iwdi:
        if i_info.get('swap_mb') or i_info.get('ephemeral_mb'):
            err_msg_invalid = _("Cannot deploy whole disk image with "
                                "swap or ephemeral size set")
            raise exception.InvalidParameterValue(err_msg_invalid)
    elif image_deploy:
        # NOTE(TheJulia): This only applies to partition image deploys.
        _validate_layout_properties(node, info, i_info)

    i_info['configdrive'] = info.get('configdrive')

    return i_info


def _validate_layout_properties(node, info, i_info):
    i_info['swap_mb'] = info.get('swap_mb', 0)
    i_info['ephemeral_gb'] = info.get('ephemeral_gb', 0)
    # NOTE(vdrok): We're casting disk layout parameters to int only after
    # ensuring that it is possible
    for param in DISK_LAYOUT_PARAMS:
        try:
            int(i_info[param])
        except ValueError:
            reason = _("%s is not an integer value.") % i_info[param]
            raise exception.InvalidParameterValue(_ERR_MSG_INVALID_DEPLOY %
                                                  {'param': param,
                                                   'reason': reason})

    i_info['root_mb'] = 1024 * int(i_info['root_gb'])
    i_info['swap_mb'] = int(i_info['swap_mb'])
    i_info['ephemeral_mb'] = 1024 * int(i_info['ephemeral_gb'])
    i_info['ephemeral_format'] = info.get('ephemeral_format')
    if i_info['ephemeral_gb'] and not i_info['ephemeral_format']:
        i_info['ephemeral_format'] = CONF.pxe.default_ephemeral_format

    # NOTE(Zhenguo): If rebuilding with preserve_ephemeral option, check
    # that the disk layout is unchanged.
    if i_info['preserve_ephemeral']:
        _check_disk_layout_unchanged(node, i_info)


def _check_disk_layout_unchanged(node, i_info):
    """Check whether disk layout is unchanged.

    If the node has already been deployed to, this checks whether the disk
    layout for the node is the same as when it had been deployed to.

    :param node: the node of interest
    :param i_info: instance information (a dictionary) for the node, containing
                   disk layout information
    :raises: InvalidParameterValue if the disk layout changed
    """
    # If a node has been deployed to, this is the instance information
    # used for that deployment.
    if 'instance' not in node.driver_internal_info:
        return

    error_msg = ''
    for param in DISK_LAYOUT_PARAMS:
        param_value = int(node.driver_internal_info['instance'][param])
        if param_value != int(i_info[param]):
            error_msg += (_(' Deployed value of %(param)s was %(param_value)s '
                            'but requested value is %(request_value)s.') %
                          {'param': param, 'param_value': param_value,
                           'request_value': i_info[param]})

    if error_msg:
        err_msg_invalid = _("The following parameters have different values "
                            "from previous deployment:%(error_msg)s")
        raise exception.InvalidParameterValue(err_msg_invalid %
                                              {'error_msg': error_msg})


def _get_image_dir_path(node_uuid):
    """Generate the dir for an instances disk."""
    return os.path.join(CONF.pxe.images_path, node_uuid)


def _get_image_file_path(node_uuid):
    """Generate the full path for an instances disk."""
    return os.path.join(_get_image_dir_path(node_uuid), 'disk')


def _get_http_image_symlink_dir_path():
    """Generate the dir for storing symlinks to cached instance images."""
    return os.path.join(CONF.deploy.http_root, CONF.deploy.http_image_subdir)


def _get_http_image_symlink_file_path(node_uuid):
    """Generate the full path for the symlink to an cached instance image."""
    return os.path.join(_get_http_image_symlink_dir_path(), node_uuid)


def direct_deploy_should_convert_raw_image(node):
    """Whether converts image to raw format for specified node.

    :param node: ironic node object
    :returns: Boolean, whether the direct deploy interface should convert
        image to raw.
    """
    return CONF.force_raw_images and CONF.agent.stream_raw_images


@image_cache.cleanup(priority=50)
class InstanceImageCache(image_cache.ImageCache):

    def __init__(self):
        master_path = CONF.pxe.instance_master_path or None
        super(self.__class__, self).__init__(
            master_path,
            # MiB -> B
            cache_size=CONF.pxe.image_cache_size * 1024 * 1024,
            # min -> sec
            cache_ttl=CONF.pxe.image_cache_ttl * 60)


@METRICS.timer('cache_instance_image')
def cache_instance_image(ctx, node, force_raw=None, expected_format=None,
                         expected_checksum=None, expected_checksum_algo=None):
    """Fetch the instance's image from Glance

    This method pulls the disk image and writes them to the appropriate
    place on local disk.

    :param ctx: context
    :param node: an ironic node object
    :param force_raw: whether convert image to raw format
    :param expected_format: The expected format of the disk image contents.
    :param expected_checksum: The expected image checksum, to be used if we
           need to convert the image to raw prior to deploying.
    :param expected_checksum_algo: The checksum algo in use, if separately
           set.
    :returns: a tuple containing the uuid of the image and the path in
        the filesystem where image is cached.
    :raises: InvalidImage if the requested image is invalid and cannot be
        used for deployed based upon contents of the image or the metadata
        surrounding the image not matching the configured image.
    """
    # NOTE(dtantsur): applying the default here to make the option mutable
    if force_raw is None:
        force_raw = CONF.force_raw_images

    i_info = parse_instance_info(node)
    fileutils.ensure_tree(_get_image_dir_path(node.uuid))
    image_path = _get_image_file_path(node.uuid)

    if 'image_source' in node.driver_internal_info:
        uuid = node.driver_internal_info.get('image_source')
    else:
        uuid = i_info['image_source']

    img_auth = image_service.get_image_service_auth_override(node)

    LOG.debug("Fetching image %(image)s for node %(uuid)s",
              {'image': uuid, 'uuid': node.uuid})

    image_list = fetch_images(ctx, InstanceImageCache(), [(uuid, image_path)],
                              force_raw, expected_format=expected_format,
                              expected_checksum=expected_checksum,
                              expected_checksum_algo=expected_checksum_algo,
                              image_auth_data=img_auth)
    return (uuid, image_path, image_list[0][2])


@METRICS.timer('destroy_images')
def destroy_images(node_uuid):
    """Delete instance's image file.

    :param node_uuid: the uuid of the ironic node.
    """
    utils.unlink_without_raise(_get_image_file_path(node_uuid))
    utils.rmtree_without_raise(_get_image_dir_path(node_uuid))
    InstanceImageCache().clean_up()


@METRICS.timer('compute_image_checksum')
def compute_image_checksum(image_path, algorithm='md5'):
    """Compute checksum by given image path and algorithm."""
    # NOTE(TheJulia): This likely wouldn't be removed, but if we do
    # significant refactoring we could likely just change everything
    # over to the images common code, if we don't need the metrics
    # data anymore.
    return checksum_utils.compute_image_checksum(image_path, algorithm)


def remove_http_instance_symlink(node_uuid):
    symlink_path = _get_http_image_symlink_file_path(node_uuid)
    utils.unlink_without_raise(symlink_path)


def destroy_http_instance_images(node):
    """Delete instance image file and symbolic link refers to it."""
    remove_http_instance_symlink(node.uuid)
    destroy_images(node.uuid)


def _validate_image_url(node, url, secret=False, inspect_image=None,
                        expected_format=None):
    """Validates image URL through the HEAD request.

    :param url: URL to be validated
    :param secret: if URL is secret (e.g. swift temp url),
        it will not be shown in logs.
    :param inspect_image: If the requested URL should have extensive
        content checking applied. Defaults to the value provided by
        the [conductor]conductor_always_validates_images configuration
        parameter setting, but is also able to be turned off by supplying
        False where needed to perform a redirect or URL head request only.
    :param expected_format: The expected image format, if known, for
        the image inspection logic.
    :returns: Returns a dictionary with basic information about the
              requested image if image introspection is
    """
    if inspect_image is not None:
        # The caller has a bit more context and we can rely upon it,
        # for example if it knows we cannot or should not inspect
        # the image contents.
        inspect = inspect_image
    elif not CONF.conductor.disable_deep_image_inspection:
        inspect = CONF.conductor.conductor_always_validates_images
    else:
        # If we're here, file inspection has been explicitly disabled.
        inspect = False

    # NOTE(TheJulia): This method gets used in two different ways.
    # The first is as a "i did a thing, let me make sure my url works."
    # The second is to validate a remote URL is valid. In the remote case
    # we will grab the file and proceed from there.
    image_info = {}
    try:
        # NOTE(TheJulia): This method only validates that an exception
        # is NOT raised. In other words, that the endpoint does not
        # return a 200. If we're fed a folder list, this will still
        # work, which is a good and bad thing at the same time. :/
        if image_service.is_container_registry_url(url):
            oci = image_service.OciImageService()
            image_auth = image_service.get_image_service_auth_override(node)
            oci.set_image_auth(url, image_auth)
            oci.validate_href(url, secret)
        else:
            image_service.HttpImageService().validate_href(url, secret)
    except exception.ImageRefValidationFailed as e:
        with excutils.save_and_reraise_exception():
            LOG.error("The specified URL is not a valid HTTP(S) URL or is "
                      "not reachable for node %(node)s: %(msg)s",
                      {'node': node.uuid, 'msg': e})
    if inspect:
        LOG.info("Inspecting image contents for %(node)s with url %(url)s. "
                 "Expecting user supplied format: %(expected)s",
                 {'node': node.uuid,
                  'expected': expected_format,
                  'url': url})
        # Utilizes the file cache since it knows how to pull files down
        # and handles pathing and caching and all that fun, however with
        # force_raw set as false.

        # The goal here being to get the file we would normally just point
        # IPA at, be it via swift transfer *or* direct URL request, and
        # perform the safety check on it before allowing it to proceed.
        ctx = context.get_admin_context()
        # NOTE(TheJulia): Because we're using the image cache here, we
        # let it run the image validation checking as it's normal course
        # of action, and save what it tells us the image format is.
        # if there *was* a mismatch, it will raise the error.

        # NOTE(TheJulia): We don't need to supply the checksum here, because
        # we are not converting the image. The net result is the deploy
        # interface or remote agent has the responsibility to checksum the
        # image.
        _, image_path, img_format = cache_instance_image(
            ctx,
            node,
            force_raw=False,
            expected_format=expected_format)
        # NOTE(TheJulia): We explicitly delete this file because it has no use
        # in the cache after this point.
        utils.unlink_without_raise(image_path)
        image_info['disk_format'] = img_format
    return image_info


def _cache_and_convert_image(task, instance_info, image_info=None):
    """Cache an image locally and convert it to RAW if needed.

    :param task: The Taskmanager object related to this action.
    :param instance_info: The instance_info field being used in
                          association with this method call.
    :param image_info: The supplied image_info from Glance.
    """
    # Ironic cache and serve images from httpboot server
    force_raw = direct_deploy_should_convert_raw_image(task.node)
    if 'image_source' in task.node.driver_internal_info:
        image_source = task.node.driver_internal_info.get('image_source')
    else:
        image_source = task.node.instance_info.get('image_source')

    if image_info is None:
        initial_format = instance_info.get('image_disk_format')
    else:
        initial_format = image_info.get('disk_format')
    checksum, checksum_algo = checksum_utils.get_checksum_and_algo(
        instance_info)
    _, image_path, img_format = cache_instance_image(
        task.context, task.node,
        force_raw=force_raw,
        expected_format=initial_format,
        expected_checksum=checksum,
        expected_checksum_algo=checksum_algo)
    if force_raw or image_info is None:
        if force_raw:
            instance_info['image_disk_format'] = 'raw'
        else:
            LOG.debug('Detecting image format for the locally cached image '
                      '%(image)s for node %(node)s',
                      {'image': image_path, 'node': task.node.uuid})
            instance_info['image_disk_format'] = \
                images.get_source_format(image_source,
                                         image_path)

        # Standard behavior is for image_checksum to be MD5,
        # so if the hash algorithm is None, then we will use
        # sha256.
        if image_info is None:
            os_hash_algo = instance_info.get('image_os_hash_algo')
            hash_value = instance_info.get('image_os_hash_value')
            old_checksum = instance_info.get('image_checksum')
        else:
            os_hash_algo = image_info.get('os_hash_algo')
            hash_value = image_info.get('os_hash_value')
            old_checksum = image_info.get('checksum')

        if initial_format != instance_info['image_disk_format']:
            if not os_hash_algo or os_hash_algo == 'md5':
                LOG.debug("Checksum algorithm for image %(image)s for node "
                          "%(node)s is set to '%(algo)s', changing to sha256",
                          {'algo': os_hash_algo, 'node': task.node.uuid,
                           'image': image_path})
                os_hash_algo = 'sha256'

            LOG.debug('Recalculating checksum for image %(image)s for node '
                      '%(node)s due to image conversion',
                      {'image': image_path, 'node': task.node.uuid})
            instance_info['image_checksum'] = None
            hash_value = checksum_utils.compute_image_checksum(image_path,
                                                               os_hash_algo)
        else:
            instance_info['image_checksum'] = old_checksum

        instance_info['image_os_hash_algo'] = os_hash_algo
        instance_info['image_os_hash_value'] = hash_value
    else:
        instance_info['image_checksum'] = image_info['checksum']
        instance_info['image_disk_format'] = image_info['disk_format']
        instance_info['image_os_hash_algo'] = image_info[
            'os_hash_algo']
        instance_info['image_os_hash_value'] = image_info[
            'os_hash_value']

    # Create symlink and update image url
    symlink_dir = _get_http_image_symlink_dir_path()
    fileutils.ensure_tree(symlink_dir)
    symlink_path = _get_http_image_symlink_file_path(task.node.uuid)
    file_extension = None
    if get_boot_option(task.node) == 'kickstart':
        # 'liveimg --url' kickstart command uses the file extension to
        # identify the OS image type. Without a valid file extension it will
        # assume the disk image is a partition image and try to 'mount' it on
        # the ramdisk. See 'liveimg' command for more details
        # https://pykickstart.readthedocs.io/en/latest/kickstart-docs.html
        valid_file_extensions = ['.img', '.tar', '.tbz', '.tgz', '.txz',
                                 '.tar.gz', '.tar.bz2', '.tar.xz']
        if image_info and 'disk_file_extension' in image_info['properties']:
            ext = image_info['properties']['disk_file_extension']
            file_extension = ext if ext in valid_file_extensions else None
        if file_extension:
            symlink_path = symlink_path + file_extension
        else:
            LOG.warning("The 'disk_file_extension' property was not set on "
                        "the glance image or not a valid extension so the "
                        "anaconda installer will try to 'mount' the OS image.")
    utils.create_link_without_raise(image_path, symlink_path)

    base_url = CONF.deploy.http_url
    if base_url.endswith('/'):
        base_url = base_url[:-1]
    http_image_url = '/'.join(
        [base_url, CONF.deploy.http_image_subdir,
         task.node.uuid])
    if file_extension:
        http_image_url = http_image_url + file_extension
    # We don't inspect the image in our url check because we just need to do
    # an quick path validity check here, we should be checking contents way
    # earlier on in this method.
    _validate_image_url(task.node, http_image_url, secret=False,
                        inspect_image=False)
    instance_info['image_url'] = http_image_url


def get_image_download_source(node):
    """Get the effective value of image_download_source for the node."""
    return (node.instance_info.get('image_download_source')
            or node.driver_info.get('image_download_source')
            or CONF.agent.image_download_source)


def _instance_info_for_glance(task, instance_info, image_download_source,
                              image_source, iwdi):
    """Helper for build_instance_info_for_deploy with glance."""
    # This is a helper method which should only contain code as it relates
    # to interacting with glance.
    glance = image_service.GlanceImageService(context=task.context)
    image_info = glance.show(image_source)
    LOG.debug('Got image info: %(info)s for node %(node)s.',
              {'info': image_info, 'node': task.node.uuid})
    # Values are explicitly set into the instance info field
    # so IPA have the values available.
    instance_info['image_checksum'] = image_info['checksum']
    instance_info['image_os_hash_algo'] = image_info['os_hash_algo']
    instance_info['image_os_hash_value'] = image_info['os_hash_value']
    if image_download_source == 'swift':
        # In this case, we are getting a file *from* swift for a glance
        # image which is backed by swift. IPA downloads the file directly
        # from swift, but cannot get any metadata related to it otherwise.
        swift_temp_url = glance.swift_temp_url(image_info)
        image_format = image_info.get('disk_format')
        # In the process of validating the URL is valid, we will perform
        # the requisite safety checking of the asset as we can end up
        # converting it in the agent, or needing the disk format value
        # to be correct for the Ansible deployment interface.
        validate_results = _validate_image_url(
            task.node, swift_temp_url, secret=True,
            expected_format=image_format)
        instance_info['image_url'] = swift_temp_url
        instance_info['image_disk_format'] = \
            validate_results.get('disk_format', image_format)
    else:
        # In this case, we're directly downloading the glance image and
        # hosting it locally for retrieval by the IPA.
        _cache_and_convert_image(task, instance_info, image_info)

    # We're just populating extra information for a glance backed image in
    # case a deployment interface driver needs them at some point.
    instance_info['image_container_format'] = (
        image_info['container_format'])
    instance_info['image_tags'] = image_info.get('tags', [])
    instance_info['image_properties'] = image_info['properties']

    if not iwdi and get_boot_option(task.node) != 'local':
        instance_info['kernel'] = image_info['properties']['kernel_id']
        instance_info['ramdisk'] = image_info['properties']['ramdisk_id']
    return instance_info, image_info


@METRICS.timer('build_instance_info_for_deploy')
def build_instance_info_for_deploy(task):
    """Build instance_info necessary for deploying to a node.

    :param task: a TaskManager object containing the node
    :returns: a dictionary containing the properties to be updated
        in instance_info
    :raises: exception.ImageRefValidationFailed if image_source is not
        Glance href and is not HTTP(S) URL.
    """
    node = task.node
    instance_info = node.instance_info
    di_info = node.driver_internal_info
    iwdi = node.driver_internal_info.get('is_whole_disk_image')
    image_source = instance_info['image_source']

    # Remove the saved image_source in case it exists in driver_internal_info
    di_info.pop('image_source', None)

    # Save out driver_internal_info to prevent race conditions.
    node.driver_internal_info = di_info

    # Flag if we know the source is a path, used for Anaconda
    # deploy interface where you can just tell anaconda to
    # consume artifacts from a path. In this case, we are not
    # doing any image conversions, we're just passing through
    # a URL in the form of configuration.
    isap = node.driver_internal_info.get('is_source_a_path')

    # If our url ends with a /, i.e. we have been supplied with a path,
    # we can only deploy this in limited cases for drivers and tools
    # which are aware of such. i.e. anaconda.
    image_download_source = get_image_download_source(node)

    # There is no valid reason this should already be set, and
    # and gets replaced at various points in this sequence.
    instance_info['image_url'] = None

    # This flag exists to lockout the overall continued flow of
    # file validation if glance is in use. This is because glance
    # can have have objects stored in Swift and those objects can
    # be directly referenced by a separate swift client. Which means,
    # additional information then needs to be gathered and exchanged
    # which is a separate process from just a remote http file.
    is_glance_image = False

    # TODO(TheJulia): We should likely look at splitting this method
    # into everal distinct helpers. First, glance, then OCI, then
    # general file activities like download/cache or verify a remote
    # URL.

    # Remote image services/repositories are a little different, they house
    # extra data we need to collect data from to streamline the process.
    if service_utils.is_glance_image(image_source):
        # We know the image source is likely rooted from a glance record,
        # so we don't need to do other checks unrelated to non-glance flows.
        is_glance_image = True
        # Trigger the glance image handling code path
        (instance_info, image_info) = _instance_info_for_glance(
            task, instance_info, image_download_source, image_source,
            iwdi)

    elif image_service.is_container_registry_url(image_source):
        # Is an oci image, we need to figure out the particulars...
        # but we *don't* need to also handle special casing with Swift.
        # We will setup things so _cache_and_convert_image can do the needful
        # Or just validate the remote url data.
        oci = image_service.OciImageService()
        image_auth = image_service.get_image_service_auth_override(task.node)
        oci.set_image_auth(image_source, image_auth)
        # Ask the image service method to identify and gather information
        # about the image. This is different from a specific manifest supplied
        # upfront.
        image_info = oci.identify_specific_image(
            image_source, image_download_source,
            node.properties.get('cpu_arch')
        )
        if (image_info.get('image_disk_format') == 'unknown'
            and instance_info.get('image_disk_format') == 'raw'):
            # Ironic, internally, resets image_disk_format for IPA, and
            # we're in a case where we've been given a specific URL, which
            # might not be raw. There is no way to know what is actually
            # correct, so we'll pop the value out completely, and let
            # auto-detection run it's course, so rebuilds or redeploy
            # attempts are an available option.
            image_info.pop('image_disk_format')
            instance_info.pop('image_disk_format')
        instance_info.update(image_info)
        # Save what we are using for discoverability by the user, and
        # save an override image_source to driver_internal_info for
        # other methods to rely upon as the authoritative source.
        image_source = instance_info.get('oci_image_manifest_url')
        # This preserves an override for _cache_and_convert_image
        # so it knows where to actually retrieve data from without
        # us overriding image_source saved by the user, so rebuilds
        # will work as expected.
        di_info['image_source'] = image_source
        node.driver_internal_info = di_info
    if not is_glance_image:
        if (image_source.startswith('file://')
                or image_download_source == 'local'):
            # In this case, we're explicitly downloading (or copying a file)
            # hosted locally so IPA can download it directly from Ironic.

            # NOTE(TheJulia): Intentionally only supporting file:/// as image
            # based deploy source since we don't want to, nor should we be in
            # in the business of copying large numbers of files as it is a
            # huge performance impact.

            _cache_and_convert_image(task, instance_info)
        else:
            # This is the "all other cases" logic for aspects like the user
            # has supplied us a direct URL to reference. In cases like the
            # anaconda deployment interface where we might just have a path
            # and not a file, or where a user may be supplying a full URL to
            # a remotely hosted image, we at a minimum need to check if the
            # url is valid, and address any redirects upfront.
            try:
                # NOTE(TheJulia): In the case we're here, we not doing an
                # integrated image based deploy, but we may also be doing
                # a path based anaconda base deploy, in which case we have
                # no backing image, but we need to check for a URL
                # redirection. So, if the source is a path (i.e. isap),
                # we don't need to inspect the image as there is no image
                # in the case for the deployment to drive.
                validated_results = {}
                if isap:
                    # This is if the source is a path url, such as one used by
                    # anaconda templates to to rely upon bootstrapping
                    # defaults.
                    _validate_image_url(node, image_source,
                                        inspect_image=False)
                else:
                    # When not isap, we can just let _validate_image_url make
                    # the required decision on if contents need to be sampled,
                    # or not. We try to pass the image_disk_format which may
                    # be declared by the user, and if not we set
                    # expected_format to None.
                    validate_results = _validate_image_url(
                        node,
                        image_source,
                        expected_format=instance_info.get(
                            'image_disk_format',
                            None))
                # image_url is internal, and used by IPA and some boot
                # templates. In most cases, it needs to come from image_source
                # explicitly.
                if 'disk_format' in validated_results:
                    # Ensure IPA has the value available, so write what we
                    # detect, if anything. This is also an item which might be
                    # needful with ansible deploy interface, when used in
                    # standalone mode.
                    instance_info['image_disk_format'] = \
                        validate_results.get('disk_format')
                if not instance_info.get('image_url'):
                    instance_info['image_url'] = image_source
            except exception.ImageRefIsARedirect as e:
                # At this point, we've got a redirect response from the
                # webserver, and we're going to try to handle it as a single
                # redirect action, as requests, by default, only lets a single
                # redirect to occur. This is likely a URL pathing fix, like a
                # trailing / on a path,
                # or move to HTTPS from a user supplied HTTP url.
                if e.redirect_url:
                    # Since we've got a redirect, we need to carry the rest of
                    # the request logic as well, which includes recording a
                    # disk format, if applicable.
                    instance_info['image_url'] = e.redirect_url
                    # We need to save the image_source back out so it caches
                    instance_info['image_source'] = e.redirect_url
                    task.node.instance_info = instance_info
                    if not isap:
                        # The redirect doesn't relate to a path being used, so
                        # the target is a filename, likely cause is webserver
                        # telling the client to use HTTPS.
                        validated_results = _validate_image_url(
                            node, e.redirect_url,
                            expected_format=instance_info.get(
                                'image_disk_format', None))
                        if 'disk_format' in validated_results:
                            instance_info['image_disk_format'] = \
                                validated_results.get('disk_format')
                else:
                    raise

    if not isap:
        if not iwdi:
            instance_info['image_type'] = images.IMAGE_TYPE_PARTITION
            i_info = parse_instance_info(node)
            instance_info.update(i_info)
        else:
            instance_info['image_type'] = images.IMAGE_TYPE_WHOLE_DISK
    else:
        # Call central parsing so we retain things like config drives.
        i_info = parse_instance_info(node, image_deploy=False)
        instance_info.update(i_info)
    return instance_info


def check_interface_capability(interface, capability):
    """Evaluate interface to determine if capability is present.

    :param interface: The interface object to check.
    :param capability: The value representing the capability that
                       the caller wishes to check if present.

    :returns: True if capability found, otherwise False.
    """
    return capability in getattr(interface, 'capabilities', [])


def get_remote_boot_volume(task):
    """Identify a boot volume from any configured volumes.

    :returns: None or the volume target representing the volume.
    """
    targets = task.volume_targets
    for volume in targets:
        if volume['boot_index'] == 0:
            return volume


def populate_storage_driver_internal_info(task):
    """Set node driver_internal_info for boot from volume parameters.

    :param task: a TaskManager object containing the node.
    :raises: StorageError when a node has an iSCSI or FibreChannel boot volume
             defined but is not capable to support it.
    """
    node = task.node
    boot_volume = get_remote_boot_volume(task)
    if not boot_volume:
        return
    vol_type = str(boot_volume.volume_type).lower()
    node_caps = driver_utils.capabilities_to_dict(
        node.properties.get('capabilities'))
    if vol_type == 'iscsi' and 'iscsi_boot' not in node_caps:
        # TODO(TheJulia): In order to support the FCoE and HBA boot cases,
        # some additional logic will be needed here to ensure we align.
        # The deployment, in theory, should never reach this point
        # if the interfaces all validated, but we shouldn't use that
        # as the only guard against bad configurations.
        raise exception.StorageError(_('Node %(node)s has an iSCSI boot '
                                       'volume defined and no iSCSI boot '
                                       'support available.') %
                                     {'node': node.uuid})
    if vol_type == 'fibre_channel' and 'fibre_channel_boot' not in node_caps:
        raise exception.StorageError(_('Node %(node)s has a Fibre Channel '
                                       'boot volume defined and no Fibre '
                                       'Channel boot support available.') %
                                     {'node': node.uuid})
    boot_capability = ("%s_volume_boot" % vol_type)
    deploy_capability = ("%s_volume_deploy" % vol_type)
    vol_uuid = boot_volume['uuid']
    if check_interface_capability(task.driver.boot, boot_capability):
        node.set_driver_internal_info('boot_from_volume', vol_uuid)
    # NOTE(TheJulia): This would be a convenient place to check
    # if we need to know about deploying the volume.
    if (check_interface_capability(task.driver.deploy, deploy_capability)
            and task.driver.storage.should_write_image(task)):
        node.set_driver_internal_info('boot_from_volume_deploy', vol_uuid)
        # NOTE(TheJulia): This is also a useful place to include a
        # root device hint since we should/might/be able to obtain
        # and supply that information to IPA if it needs to write
        # the image to the volume.
    node.save()


def tear_down_storage_configuration(task):
    """Clean up storage configuration.

    Remove entries from driver_internal_info for storage and
    deletes the volume targets from the database. This is done
    to ensure a clean state for the next boot of the machine.
    """

    # TODO(mjturek): TheJulia mentioned that this should
    # possibly be configurable for the standalone case. However,
    # this is dangerous if IPA is not handling the cleaning.
    for volume in task.volume_targets:
        volume.destroy()
        LOG.info('Successfully deleted volume target %(target)s. '
                 'The node associated with the target was %(node)s.',
                 {'target': volume.uuid, 'node': task.node.uuid})

    node = task.node
    node.del_driver_internal_info('boot_from_volume')
    node.del_driver_internal_info('boot_from_volume_deploy')
    node.save()


def is_iscsi_boot(task):
    """Return true if booting from an iscsi volume."""
    node = task.node
    volume = node.driver_internal_info.get('boot_from_volume')
    if volume:
        try:
            boot_volume = objects.VolumeTarget.get_by_uuid(
                task.context, volume)
            if boot_volume.volume_type == 'iscsi':
                return True
        except exception.VolumeTargetNotFound:
            return False
    return False


# NOTE(etingof): retain original location of these funcs for compatibility
is_secure_boot_requested = boot_mode_utils.is_secure_boot_requested
get_boot_mode_for_deploy = boot_mode_utils.get_boot_mode_for_deploy
parse_instance_info_capabilities = (
    utils.parse_instance_info_capabilities
)

# NOTE(dtantsur): backward compatibility, do not use
get_async_step_return_state = async_steps.get_return_state
set_async_step_flags = async_steps.set_node_flags


def prepare_agent_boot(task):
    """Prepare booting the agent on the node.

    :param task: a TaskManager instance.
    """
    deploy_opts = build_agent_options(task.node)
    task.driver.boot.prepare_ramdisk(task, deploy_opts)


def reboot_to_finish_step(task, timeout=None):
    """Reboot the node into IPA to finish a deploy/clean step.

    :param task: a TaskManager instance.
    :param timeout: timeout (in seconds) positive integer (> 0) for any
      power state. ``None`` indicates to use default timeout.
    :returns: states.CLEANWAIT if cleaning operation in progress
              or states.DEPLOYWAIT if deploy operation in progress.
    """
    disable_ramdisk = task.node.driver_internal_info.get(
        'cleaning_disable_ramdisk')
    if not disable_ramdisk:
        if (manager_utils.is_fast_track(task)
                and not task.node.disable_power_off):
            LOG.debug('Forcing power off on node %s for a clean reboot into '
                      'the agent image', task.node)
            manager_utils.node_power_action(task, states.POWER_OFF)
        prepare_agent_boot(task)

    manager_utils.node_power_action(task, states.REBOOT, timeout)
    return async_steps.get_return_state(task.node)


def step_error_handler(task, logmsg, errmsg=None):
    """Run the correct handler for the current step.

    :param task: a TaskManager instance.
    :param logmsg: Message to be logged.
    :param errmsg: Message for the user. Optional, if not provided `logmsg` is
        used.
    """
    if task.node.provision_state in [states.CLEANING, states.CLEANWAIT]:
        manager_utils.cleaning_error_handler(task, logmsg, errmsg=errmsg)
    elif task.node.provision_state in [states.DEPLOYING, states.DEPLOYWAIT]:
        manager_utils.deploying_error_handler(task, logmsg, errmsg=errmsg)
    elif task.node.provision_state in [states.SERVICING, states.SERVICEWAIT]:
        manager_utils.servicing_error_handler(task, logmsg, errmsg=errmsg)


def get_root_device_for_deploy(node):
    """Get a root device requested for deployment or None.

    :raises: InvalidParameterValue on invalid hints.
    :return: Parsed root device hints or None if no hints were provided.
    """
    hints = node.instance_info.get('root_device')
    if not hints:
        hints = node.properties.get('root_device')
        if not hints:
            return
        source = 'properties'
    else:
        source = 'instance_info'

    try:
        return utils.parse_root_device_hints(hints)
    except ValueError as e:
        raise exception.InvalidParameterValue(
            _('Failed to validate the root device hints %(hints)s (from the '
              'node\'s %(source)s) for node %(node)s: %(error)s') %
            {'node': node.uuid, 'hints': hints, 'source': source, 'error': e})


def needs_agent_ramdisk(node, mode='deploy'):
    """Checks whether the node requires an agent ramdisk now."""
    if mode != 'deploy':
        return True  # Rescue always needs a ramdisk

    if get_boot_option(node) != 'ramdisk':
        return True  # Normal deploys need an agent

    # Ramdisk deploys don't need an agent, but cleaning will. Since we don't
    # want nodes to be stuck on deletion, require an agent when cleaning is
    # enabled.
    return not manager_utils.skip_automated_cleaning(node)
