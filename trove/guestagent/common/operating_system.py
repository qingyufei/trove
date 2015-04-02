# Copyright (c) 2011 OpenStack Foundation
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

import inspect
import os
import stat

import operator
from oslo_concurrency.processutils import UnknownArgumentError
from trove.common import exception
from trove.common import utils
from trove.common.i18n import _

REDHAT = 'redhat'
DEBIAN = 'debian'
SUSE = 'suse'


class FileMode(object):
    """
    Represent file permissions (or 'modes') that can be applied on a filesystem
    path by functions such as 'chmod'. The way the modes get applied
    is generally controlled by the operation ('reset', 'add', 'remove')
    group to which they belong.
    All modes are represented as octal numbers. Modes are combined in a
    'bitwise OR' (|) operation.
    Multiple modes belonging to a single operation are combined
    into a net value for that operation which can be retrieved by one of the
    'get_*_mode' methods.
    Objects of this class are compared by the net values of their
    individual operations.

    :seealso: chmod

    :param reset:            List of (octal) modes that will be set,
                             other bits will be cleared.
    :type reset:             list

    :param add:              List of (octal) modes that will be added to the
                             current mode.
    :type add:               list

    :param remove:           List of (octal) modes that will be removed from
                             the current mode.
    :type remove:            list
    """

    @classmethod
    def SET_FULL(cls):
        return cls(reset=[stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO])  # =0777

    @classmethod
    def SET_GRP_RW_OTH_R(cls):
        return cls(reset=[stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH])  # =0064

    @classmethod
    def ADD_READ_ALL(cls):
        return cls(add=[stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH])  # +0444

    @classmethod
    def ADD_GRP_RW(cls):
        return cls(add=[stat.S_IRGRP | stat.S_IWGRP])  # +0060

    def __init__(self, reset=None, add=None, remove=None):
        self._reset = list(reset) if reset is not None else []
        self._add = list(add) if add is not None else []
        self._remove = list(remove) if remove is not None else []

    def get_reset_mode(self):
        """Get the net (combined) mode that will be set.
        """
        return self._combine_modes(self._reset)

    def get_add_mode(self):
        """Get the net (combined) mode that will be added.
        """
        return self._combine_modes(self._add)

    def get_remove_mode(self):
        """Get the net (combined) mode that will be removed.
        """
        return self._combine_modes(self._remove)

    def _combine_modes(self, modes):
        return reduce(operator.or_, modes) if modes else None

    def has_any(self):
        """Check if any modes are specified.
        """
        return bool(self._reset or self._add or self._remove)

    def __hash__(self):
        return hash((self.get_reset_mode(),
                     self.get_add_mode(),
                     self.get_remove_mode()))

    def __eq__(self, other):
        if other and isinstance(other, FileMode):
            if other is self:
                return True

            return (other.get_reset_mode() == self.get_reset_mode() and
                    other.get_add_mode() == self.get_add_mode() and
                    other.get_remove_mode() == self.get_remove_mode())

        return False

    def __repr__(self):
        args = []
        if self._reset:
            args.append('reset=[{:03o}]'.format(self.get_reset_mode()))
        if self._add:
            args.append('add=[{:03o}]'.format(self.get_add_mode()))
        if self._remove:
            args.append('remove=[{:03o}]'.format(self.get_remove_mode()))

        return 'Modes({:s})'.format(', '.join(args))


def get_os():
    if os.path.isfile("/etc/redhat-release"):
        return REDHAT
    elif os.path.isfile("/etc/SuSE-release"):
        return SUSE
    else:
        return DEBIAN


def file_discovery(file_candidates):
    for file in file_candidates:
        if os.path.isfile(file):
            return file


def service_discovery(service_candidates):
    """
    This function discovering how to start, stop, enable, disable service
    in current environment. "service_candidates" is array with possible
    system service names. Works for upstart, systemd, sysvinit.
    """
    result = {}
    for service in service_candidates:
        # check upstart
        if os.path.isfile("/etc/init/%s.conf" % service):
            # upstart returns error code when service already started/stopped
            result['cmd_start'] = "sudo start %s || true" % service
            result['cmd_stop'] = "sudo stop %s || true" % service
            result['cmd_enable'] = ("sudo sed -i '/^manual$/d' "
                                    "/etc/init/%s.conf" % service)
            result['cmd_disable'] = ("sudo sh -c 'echo manual >> "
                                     "/etc/init/%s.conf'" % service)
            break
        # check sysvinit
        if os.path.isfile("/etc/init.d/%s" % service):
            result['cmd_start'] = "sudo service %s start" % service
            result['cmd_stop'] = "sudo service %s stop" % service
            if os.path.isfile("/usr/sbin/update-rc.d"):
                result['cmd_enable'] = "sudo update-rc.d %s defaults; sudo " \
                                       "update-rc.d %s enable" % (service,
                                                                  service)
                result['cmd_disable'] = "sudo update-rc.d %s defaults; sudo " \
                                        "update-rc.d %s disable" % (service,
                                                                    service)
            elif os.path.isfile("/sbin/chkconfig"):
                result['cmd_enable'] = "sudo chkconfig %s on" % service
                result['cmd_disable'] = "sudo chkconfig %s off" % service
            break
        # check systemd
        service_path = "/lib/systemd/system/%s.service" % service
        if os.path.isfile(service_path):
            result['cmd_start'] = "sudo systemctl start %s" % service
            result['cmd_stop'] = "sudo systemctl stop %s" % service

            # currently "systemctl enable" doesn't work for symlinked units
            # as described in https://bugzilla.redhat.com/1014311, therefore
            # replacing a symlink with its real path
            if os.path.islink(service_path):
                real_path = os.path.realpath(service_path)
                unit_file_name = os.path.basename(real_path)
                result['cmd_enable'] = ("sudo systemctl enable %s" %
                                        unit_file_name)
                result['cmd_disable'] = ("sudo systemctl disable %s" %
                                         unit_file_name)
            else:
                result['cmd_enable'] = "sudo systemctl enable %s" % service
                result['cmd_disable'] = "sudo systemctl disable %s" % service
            break
    return result


def update_owner(user, group, path):
    """
       Changes the owner and group for the path (recursively)
    """
    utils.execute_with_timeout("chown", "-R", "%s:%s" % (user, group), path,
                               run_as_root=True, root_helper="sudo")


def chmod(path, mode, recursive=True, force=False, **kwargs):
    """Changes the mode of a given file.

    :seealso: Modes for more information on the representation of modes.
    :seealso: _execute_shell_cmd for valid optional keyword arguments.

    :param path:            Path to the modified file.
    :type path:             string

    :param mode:            File permissions (modes).
                            The modes will be applied in the following order:
                            reset (=), add (+), remove (-)
    :type mode:             FileMode

    :param recursive:       Operate on files and directories recursively.
    :type recursive:        boolean

    :param force:           Suppress most error messages.
    :type force:            boolean

    :raises:                :class:`UnprocessableEntity` if path not given.
    :raises:                :class:`UnprocessableEntity` if no mode given.
    """

    if path:
        options = (('f', force), ('R', recursive))
        shell_modes = _build_shell_chmod_mode(mode)
        _execute_shell_cmd('chmod', options, shell_modes, path, **kwargs)
    else:
        raise exception.UnprocessableEntity(
            _("Cannot change mode of a blank file."))


def _build_shell_chmod_mode(mode):
    """
    Build a shell representation of given mode.

    :seealso: Modes for more information on the representation of modes.

    :param mode:            File permissions (modes).
    :type mode:             FileModes

    :raises:                :class:`UnprocessableEntity` if no mode given.

    :returns: Following string for any non-empty modes:
              '=<reset mode>,+<add mode>,-<remove mode>'
    """

    # Handle methods passed in as constant fields.
    if inspect.ismethod(mode):
        mode = mode()

    if mode and mode.has_any():
        text_modes = (('=', mode.get_reset_mode()),
                      ('+', mode.get_add_mode()),
                      ('-', mode.get_remove_mode()))
        return ','.join(
            ['{0:s}{1:03o}'.format(item[0], item[1]) for item in text_modes
             if item[1]])
    else:
        raise exception.UnprocessableEntity(_("No file mode specified."))


def remove(path, force=False, recursive=True, **kwargs):
    """Remove a given file or directory.

    :seealso: _execute_shell_cmd for valid optional keyword arguments.

    :param path:            Path to the removed file.
    :type path:             string

    :param force:           Ignore nonexistent files.
    :type force:            boolean

    :param recursive:       Remove directories and their contents recursively.
    :type recursive:        boolean

    :raises:                :class:`UnprocessableEntity` if path not given.
    """

    if path:
        options = (('f', force), ('R', recursive))
        _execute_shell_cmd('rm', options, path, **kwargs)
    else:
        raise exception.UnprocessableEntity(_("Cannot remove a blank file."))


def _execute_shell_cmd(cmd, options, *args, **kwargs):
    """Execute a given shell command passing it
    given options (flags) and arguments.

    Takes optional keyword arguments:
    :param as_root:        Execute as root.
    :type as_root:         boolean

    :param timeout:        Number of seconds if specified,
                           default if not.
                           There is no timeout if set to None.
    :type timeout:         integer

    :raises:               class:`UnknownArgumentError` if passed unknown args.
    """

    exec_args = {}
    if kwargs.pop('as_root', False):
        exec_args['run_as_root'] = True
        exec_args['root_helper'] = 'sudo'

    if 'timeout' in kwargs:
        exec_args['timeout'] = kwargs.pop('timeout')

    if kwargs:
        raise UnknownArgumentError(_("Got unknown keyword args: %r") % kwargs)

    cmd_flags = _build_command_options(options)
    cmd_args = cmd_flags + list(args)
    utils.execute_with_timeout(cmd, *cmd_args, **exec_args)


def _build_command_options(options):
    """Build a list of flags from given pairs (option, is_enabled).
    Each option is prefixed with a single '-'.
    Include only options for which is_enabled=True.
    """

    return ['-' + item[0] for item in options if item[1]]
