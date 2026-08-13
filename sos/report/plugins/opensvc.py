    # This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

from sos.report.plugins import Plugin, IndependentPlugin


class Opensvc(Plugin, IndependentPlugin):

    short_desc = 'OpenSVC cluster and services (config and state collection)'
    plugin_name = 'opensvc'
    profiles = ('cluster', 'services', 'system')
    packages = ('opensvc',)

    def detect_om_version(self):
        """Detect om version: non-zero exit -> treat as v2, zero -> v3"""
        om_path = self.exec_cmd("which om")
        if om_path['status'] != 0:
            self._log_debug("om command not found")
            return None
        res = self.exec_cmd(f"file -bL {om_path['output'].strip()}")
        if res['status'] != 0:
            self._log_debug(f"file command failed for {om_path['output'].strip()}")
            return None
        return 3 if "ELF" in res['output'] else 2 if "script" in res['output'] else None

    def get_status_v3(self, kind):
        object_tab = "OBJECT"
        get_objs = self.collect_cmd_output(f"om {kind} ls --color=no --output tab={object_tab}:meta.object")
        dirname = kind + '_status'
        if get_objs['status'] == 0:
            for line in get_objs['output'].splitlines():
                if line == object_tab:
                    continue
                self.add_cmd_output(
                    f"om {line} instance status --color=no",
                    subdir=dirname
                )

    def get_config_v3(self, kind):
        object_tab = "OBJECT"
        get_objs = self.collect_cmd_output(f"om {kind} ls --color=no --output tab={object_tab}:meta.object")
        dirname = kind + '_config'
        if get_objs['status'] == 0:
            for line in get_objs['output'].splitlines():
                if line == object_tab:
                    continue
                self.add_cmd_output(
                    f"om {line} config show --color=yes --redact-secrets",
                    subdir=dirname
                )

    def get_status_v2(self, kind):
        get_objs = self.collect_cmd_output(f"om {kind} ls --color=no")
        dirname = kind + '_status'
        if get_objs['status'] == 0:
            for line in get_objs['output'].splitlines():
                self.add_cmd_output(
                    f"om {line} print status --color=no",
                    subdir=dirname
                )

    def get_status(self, kind, om_version):
        """ Get the status of opensvc management service """
        if om_version == 3:
            self.get_status_v3(kind)
        else:
            self.get_status_v2(kind)

    def setup_v3(self):

        commands = []

        def add_cmd(cmd, format=None):
            if format is None:
                format = ["auto", "json", "flat"]

            for fmt in format:
                color = "yes" if fmt == "auto" else "no"
                commands.append(f"om {cmd} --color={color} --output {fmt}")

        self.add_copy_spec([
            "/etc/opensvc/*",
            "/var/log/opensvc/*",
            "/etc/conf.d/opensvc",
            "/etc/default/opensvc",
            "/etc/sysconfig/opensvc",
            "/var/lib/opensvc/*.json",
            "/var/lib/opensvc/list.*",
            "/var/lib/opensvc/ccfg",
            "/var/lib/opensvc/cfg",
            "/var/lib/opensvc/certs/ca_certificates",
            "/var/lib/opensvc/certs/certificate_chain",
            "/var/lib/opensvc/compliance/*",
            "/var/lib/opensvc/namespaces/*",
            "/var/lib/opensvc/node/*",
            "/var/lib/opensvc/svc/*",
            "/var/lib/opensvc/vol/*",
            "/var/lib/opensvc/nscfg/*",
            "/var/lib/opensvc/*.stack",
        ])

        commands_string = [
            ("pool list",),
            ("net list",),
            ("mon",),
            ("daemon dns dump",),
            ("daemon relay status",),
            ("daemon status",),
            ("daemon ps",),
            ("array list",),
            ("daemon hb status", ["auto"]),
        ]

        for command in commands_string:
            add_cmd(*command)

        self.add_cmd_output(commands)
        self.add_dir_listing("/run/opensvc", recursive=True)
        self.get_config_v3("sec")
        self.get_config_v3("usr")

    def setup_v2(self):
        commands = []

        def add_cmd(cmd, format=None):
            if format is None:
                format = ["auto", "json", "flat_json"]

            for fmt in format:
                color = "yes" if fmt == "auto" else "no"
                commands.append(f"om {cmd} --color={color} --output {fmt}")

        self.add_copy_spec([
            "/etc/opensvc/*",
            "/var/log/opensvc/*",
            "/etc/conf.d/opensvc",
            "/etc/default/opensvc",
            "/etc/sysconfig/opensvc",
            "/var/lib/opensvc/*.json",
            "/var/lib/opensvc/list.*",
            "/var/lib/opensvc/ccfg",
            "/var/lib/opensvc/cfg",
            "/var/lib/opensvc/certs/ca_certificates",
            "/var/lib/opensvc/certs/certificate_chain",
            "/var/lib/opensvc/compliance/*",
            "/var/lib/opensvc/namespaces/*",
            "/var/lib/opensvc/namespaces/*/svc",
            "/var/lib/opensvc/namespaces/*/vol",
            "/var/lib/opensvc/namespaces/*/cfg",
            "/var/lib/opensvc/namespaces/*/nscfg"
            "/var/lib/opensvc/node/*",
            "/var/lib/opensvc/svc/*",
            "/var/lib/opensvc/vol/*",
            "/var/lib/opensvc/nscfg/*",
        ])

        command_string = [
            "pool status --verbose",
            "net status",
            "mon",
            "daemon dns dump",
            "daemon relay status",
            "daemon status"
        ]

        for command in command_string:
            add_cmd(command)

        self.add_cmd_output(commands)

    def setup(self):
        om_version = self.detect_om_version()
        if om_version is None:
            return
        if om_version == 3:
            self.setup_v3()
        else:
            self.setup_v2()

        self.add_dir_listing('/var/lib/opensvc', recursive=True)
        self.get_status('vol', om_version)
        self.get_status('svc', om_version)
        pid_file = "/var/lib/opensvc/osvcd.pid"
        try:
            with open(pid_file, 'r', encoding='utf-8') as file:
                pid = file.read().strip()
                if not pid:
                    self._log_debug(f"{pid_file} is empty")
                    return
                if not pid.isdigit():
                    self._log_debug(f"Invalid PID in {pid_file}: {pid}")
                    return
                self.add_copy_spec(f"/proc/{pid}/task/*/status")
        except (IOError, FileNotFoundError, PermissionError) as error:
            self._log_debug(
                f"Error while reading PID file {pid_file}: {error}"
            )

    def postproc(self):
        # Example:
        #
        # [hb#2]
        # secret = mypassword
        # type = relay
        # timeout = 30
        #
        # to
        #
        # [hb#2]
        # secret = ****************************
        # type = relay
        # timeout = 30

        cluster_regex = r"(\s*secret =\s*)\S+"
        self.do_file_sub(
            "/etc/opensvc/cluster.conf",
            cluster_regex,
            r"\1****************************"
        )

        node_regex = r"(\s*uuid =\s*)\S+"
        self.do_file_sub(
            "/etc/opensvc/node.conf",
            node_regex,
            r"\1****************************"
        )

# vim: set et ts=4 sw=4 :
