# This file is part of the sos project: https://github.com/sosreport/sos
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# version 2 of the GNU General Public License.
#
# See the LICENSE file in the source distribution for further information.

import re

from sos.report.plugins import Plugin, IndependentPlugin


class Opensvc(Plugin, IndependentPlugin):

    short_desc = 'OpenSVC cluster and services (config and state collection)'
    plugin_name = 'opensvc'
    profiles = ('cluster', 'services', 'system')
    # v2 package, v3 server package
    packages = ('opensvc', 'opensvc-server')

    def detect_om_version(self):
        """ Detect om version: ELF binary => v3, script => v2 """
        om_path = self.path_join("/usr/bin/om")
        try:
            with open(om_path, 'rb') as om_file:
                magic = om_file.read(4)
        except OSError as error:
            self._log_debug(f"Error while reading {om_path}: {error}")
            return None
        if magic == b'\x7fELF':
            return 3
        if magic.startswith(b'#!'):
            return 2
        self._log_debug(f"Unable to detect om version from {om_path}")
        return None

    def get_v3_rc(self):
        """ Return the v3 release candidate number, 0 for alpha and beta
        releases, None for a final release or an unparsable version """
        res = self.collect_cmd_output("om --version",
                                      suggest_filename="version")
        if res['status'] != 0:
            return None
        if re.search(r"3\.0\.0-(alpha|beta)", res['output']):
            return 0
        match = re.search(r"3\.0\.0-rc(\d+)", res['output'])
        if match:
            return int(match.group(1))
        return None

    def list_objects(self, kind, om_version):
        """ Return the names of the objects of the given kind """
        if om_version == 3:
            header = "OBJECT"
            cmd = (f"om {kind} ls --color=no "
                   f"--output tab={header}:meta.object")
        else:
            header = None
            cmd = f"om {kind} ls --color=no"
        res = self.collect_cmd_output(cmd, suggest_filename=f"{kind}-ls")
        if res['status'] != 0:
            return []
        objs = []
        for line in res['output'].splitlines():
            line = line.strip()
            if not line or line == header:
                continue
            objs.append(line)
        return objs

    @staticmethod
    def split_object_path(path, kind):
        """ Split an object path [[<ns>/]<kind>/]<name> into its
        namespace, kind and name """
        parts = path.split('/')
        name = parts[-1]
        kind = parts[-2] if len(parts) > 1 else kind
        namespace = parts[-3] if len(parts) > 2 else "root"
        return namespace, kind, name

    def add_object_cmd(self, kind, obj, cmd, suffix):
        """ Run an om command on an object, saved as
        <kind>_<suffix>/<ns>/<kind>/<name>-<suffix> """
        namespace, obj_kind, name = self.split_object_path(obj, kind)
        self.add_cmd_output(
            f"om {obj} {cmd}",
            suggest_filename=f"{name}-{suffix}",
            subdir=f"{kind}_{suffix}/{namespace}/{obj_kind}"
        )

    def get_status(self, kind, om_version):
        """ Get the status of the objects of the given kind """
        action = "instance status" if om_version == 3 else "print status"
        for obj in self.list_objects(kind, om_version):
            self.add_object_cmd(kind, obj, f"{action} --color=no", "status")

    def get_config_v3(self, kind):
        """ Get the config of the objects of the given kind, secrets
        redacted """
        for obj in self.list_objects(kind, 3):
            self.add_object_cmd(
                kind, obj, "config show --color=no --redact-secrets", "config"
            )

    def setup_v3(self):
        self.add_journal(units="opensvc-server")
        self.add_copy_spec([
            "/var/lib/opensvc/*.stack*",
            "/var/lib/opensvc/cni/net.d/*.conf",
        ])
        self.add_dir_listing("/dev/shm/opensvc", recursive=True,
                             suggest_filename="ls-dev-shm-opensvc")

        # None for a final release: every command is supported
        rc = self.get_v3_rc()

        def supported(min_rc):
            return min_rc is None or rc is None or rc >= min_rc

        # (command, output formats, minimum rc). Saved as <cmd> for the
        # auto format or no format, <cmd>.json for the json format, the
        # "**" selector left out.
        both = ("auto", "json")
        cmds = [
            ("pool list", both, None),
            ("net list", both, None),
            ("mon", ("auto",), None),
            ("daemon dns dump", both, None),
            ("daemon relay status", both, None),
            ("daemon status", both, None),
            ("daemon ps", both, None),
            ("daemon hb status", ("auto",), None),
            ("daemon exec list", both, 37),
            ("daemon orchestration list", both, 37),
            ("daemon session list", both, 37),
            ("array list", both, None),
            ("node list", both, None),
            ("node drivers", ("auto",), None),
            ("node schedule list", both, None),
            ("node capabilities list", both, None),
            ("node config validate", ("auto",), None),
            ("node relay list", both, None),
            ("node metrics", (), 33),
            ("cluster config validate", ("auto",), None),
            ("network ip list", both, None),
            ("pool volume list", both, None),
            ("** config validate", ("auto",), None),
            ("** instance list", both, None),
            ("** instance device list", both, None),
            ("** schedule list", both, None),
        ]
        for cmd, fmts, min_rc in cmds:
            if not supported(min_rc):
                self._log_debug(f"om {cmd} needs v3.0.0-rc{min_rc}, "
                                f"skipped")
                continue
            name = cmd.replace('** ', '').replace(' ', '-')
            if not fmts:
                self.add_cmd_output(f"om {cmd} --color=no",
                                    suggest_filename=name)
            for fmt in fmts:
                self.add_cmd_output(
                    f"om {cmd} --color=no --output {fmt}",
                    suggest_filename=name if fmt == "auto" else f"{name}.{fmt}"
                )

        if supported(33):
            self.get_config_v3("sec")
            self.get_config_v3("usr")
        else:
            self._log_debug("sec and usr configs need v3.0.0-rc33, skipped")

    def setup_v2(self):
        self.add_journal(units="opensvc-agent")
        cmds = [
            ("pool status --verbose", "pool-status"),
            ("net status --verbose", "net-status"),
            ("mon", "mon"),
            ("daemon dns dump", "daemon-dns-dump"),
            ("daemon relay status", "daemon-relay-status"),
            ("daemon status --format flat_json", "daemon-status"),
        ]
        for cmd, name in cmds:
            self.add_cmd_output(f"om {cmd} --color=no",
                                suggest_filename=name)

    def setup(self):
        # sec and usr objects configs hold secrets in clear text.
        # Globbed forbidden paths must match the files, not only their
        # parent directory.
        self.add_forbidden_path([
            "/etc/opensvc/sec",
            "/etc/opensvc/usr",
            "/etc/opensvc/namespaces/*/sec/*",
            "/etc/opensvc/namespaces/*/usr/*",
            "/var/lib/opensvc/sec",
            "/var/lib/opensvc/usr",
            "/var/lib/opensvc/namespaces/*/sec/*",
            "/var/lib/opensvc/namespaces/*/usr/*",
        ])

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
        ])
        self.add_dir_listing('/var/lib/opensvc', recursive=True,
                             suggest_filename="ls-var-lib-opensvc")

        om_version = self.detect_om_version()
        if om_version == 3:
            self.setup_v3()
        elif om_version == 2:
            self.setup_v2()

        if om_version is not None:
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

        regex = r"(\s*(?:secret|uuid)\s*=\s*)\S+"
        for conf in ("cluster.conf", "node.conf"):
            self.do_file_sub(
                f"/etc/opensvc/{conf}",
                regex,
                r"\1****************************"
            )


# vim: set et ts=4 sw=4 :
