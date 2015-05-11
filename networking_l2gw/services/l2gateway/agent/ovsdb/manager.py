# Copyright (c) 2015 OpenStack Foundation.
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
from contextlib import contextmanager

import eventlet

from neutron import context as ctx
from neutron.i18n import _LE
from neutron.openstack.common import loopingcall

from networking_l2gw.services.l2gateway.agent import base_agent_manager
from networking_l2gw.services.l2gateway.agent import l2gateway_config
from networking_l2gw.services.l2gateway.agent.ovsdb import ovsdb_monitor
from networking_l2gw.services.l2gateway.agent.ovsdb import ovsdb_writer
from networking_l2gw.services.l2gateway.common import constants as n_const

from oslo.config import cfg
from oslo_log import log as logging


LOG = logging.getLogger(__name__)


class OVSDBManager(base_agent_manager.BaseAgentManager):
    """OVSDB variant of agent manager.

       Listens to state change notifications from OVSDB servers and
       handles transactions (RPCs) destined to OVSDB servers.
    """
    def __init__(self, conf=None):
        super(OVSDBManager, self).__init__(conf)
        self._extract_ovsdb_config(conf)
        self.looping_task = loopingcall.FixedIntervalLoopingCall(
            self._connect_to_ovsdb_server)

    def _extract_ovsdb_config(self, conf):
        self.conf = conf or cfg.CONF
        ovsdb_hosts = self.conf.ovsdb.ovsdb_hosts
        if ovsdb_hosts != '':
            ovsdb_hosts = ovsdb_hosts.split(',')
            for host in ovsdb_hosts:
                self._process_ovsdb_host(host)
            # Ensure that max_connection_retries is less than
            # the periodic interval.
            if (self.conf.ovsdb.max_connection_retries >=
                    self.conf.ovsdb.periodic_interval):
                raise SystemExit("max_connection_retries should be "
                                 "less than periodic interval")

    def _process_ovsdb_host(self, host):
        try:
            host_splits = str(host).split(':')
            ovsdb_identifier = str(host_splits[0]).strip()
            ovsdb_conf = {n_const.OVSDB_IDENTIFIER: ovsdb_identifier,
                          'ovsdb_ip': str(host_splits[1]).strip(),
                          'ovsdb_port': str(host_splits[2]).strip()}
            priv_key_path = self.conf.ovsdb.l2_gw_agent_priv_key_base_path
            cert_path = self.conf.ovsdb.l2_gw_agent_cert_base_path
            ca_cert_path = self.conf.ovsdb.l2_gw_agent_ca_cert_base_path
            use_ssl = priv_key_path and cert_path and ca_cert_path
            if use_ssl:
                ssl_ovsdb = {'use_ssl': True,
                             'private_key':
                                 "/".join([str(priv_key_path),
                                           '.'.join([str(host_splits[0]).
                                                     strip(),
                                                     'key'])]),
                             'certificate':
                                 "/".join([str(cert_path),
                                           '.'.join([str(host_splits[0]).
                                                     strip(), 'cert'])]),
                             'ca_cert':
                                 "/".join([str(ca_cert_path),
                                           '.'.join([str(host_splits[0]).
                                                     strip(), 'ca_cert'])])
                             }
                ovsdb_conf.update(ssl_ovsdb)
            LOG.debug("ovsdb_conf = %s", str(ovsdb_conf))
            gateway = l2gateway_config.L2GatewayConfig(ovsdb_conf)
            self.gateways[ovsdb_identifier] = gateway
        except Exception as ex:
            LOG.exception(_LE("Exception %(ex)s occurred while processing "
                              "host %(host)s"), {'ex': ex, 'host': host})

    def _connect_to_ovsdb_server(self):
        """Initializes the connection to the OVSDB servers."""
        ovsdb_states = {}
        if self.gateways and self.l2gw_agent_type == n_const.MONITOR:
            for key in self.gateways.keys():
                gateway = self.gateways.get(key)
                ovsdb_fd = gateway.ovsdb_fd
                if not (ovsdb_fd and ovsdb_fd.connected):
                    LOG.debug("OVSDB server %s is disconnected",
                              str(gateway.ovsdb_ip))
                    try:
                        ovsdb_fd = ovsdb_monitor.OVSDBMonitor(
                            self.conf.ovsdb,
                            gateway,
                            self.agent_to_plugin_rpc)
                    except Exception:
                        ovsdb_states[key] = 'disconnected'
                        # Log a warning and continue so that it can be retried
                        # in the next iteration.
                        LOG.error(_LE("OVSDB server %s is not "
                                      "reachable"), gateway.ovsdb_ip)
                        # Continue processing the next element in the list.
                        continue
                    gateway.ovsdb_fd = ovsdb_fd
                    try:
                        eventlet.greenthread.spawn_n(
                            ovsdb_fd.set_monitor_response_handler)
                    except Exception:
                        raise SystemExit(Exception.message)
                if ovsdb_fd and ovsdb_fd.connected:
                    ovsdb_states[key] = 'connected'
        LOG.debug("Calling notify_ovsdb_states")
        self.plugin_rpc.notify_ovsdb_states(ctx.get_admin_context(),
                                            ovsdb_states)

    def handle_report_state_failure(self):
        # Not able to deliver the heart beats to the Neutron server.
        # Let us change the mode to Transact so that when the
        # Neutron server is connected back, it will make an agent
        # Monitor agent and OVSDB data will be read entirely. This way,
        # the OVSDB data in Neutron database will be the latest and in
        # sync with that in the OVSDB server tables.
        if self.l2gw_agent_type == n_const.MONITOR:
            self.l2gw_agent_type = ''
            self.agent_state.get('configurations')[n_const.L2GW_AGENT_TYPE
                                                   ] = self.l2gw_agent_type
            self._stop_looping_task()
            self._disconnect_all_ovsdb_servers()

    def _disconnect_all_ovsdb_servers(self):
        if self.gateways:
            for key, gateway in self.gateways.items():
                ovsdb_fd = gateway.ovsdb_fd
                if ovsdb_fd and ovsdb_fd.connected:
                    gateway.ovsdb_fd.disconnect()

    def set_monitor_agent(self, context, hostname):
        """Handle RPC call from plugin to update agent type.

        RPC call from the plugin to accept that I am a monitoring
        or a transact agent. This is a fanout cast message
        """
        super(OVSDBManager, self).set_monitor_agent(context, hostname)

        # If set to Monitor, then let us start monitoring the OVSDB
        # servers without any further delay.
        if self.l2gw_agent_type == n_const.MONITOR:
            self._start_looping_task()
        else:
            # Otherwise, stop monitoring the OVSDB servers
            # and close the open connections if any.
            self._stop_looping_task()
            self._disconnect_all_ovsdb_servers()

    def _stop_looping_task(self):
        if self.looping_task._running:
            self.looping_task.stop()

    def _start_looping_task(self):
        if not self.looping_task._running:
            self.looping_task.start(interval=self.conf.ovsdb.
                                    periodic_interval)

    @contextmanager
    def _open_connection(self, ovsdb_identifier):
        ovsdb_fd = None
        gateway = self.gateways.get(ovsdb_identifier)
        try:
            ovsdb_fd = ovsdb_writer.OVSDBWriter(self.conf.ovsdb,
                                                gateway)
            yield ovsdb_fd
        finally:
            if ovsdb_fd:
                ovsdb_fd.disconnect()

    def _is_valid_request(self, ovsdb_identifier):
        val_req = ovsdb_identifier and ovsdb_identifier in self.gateways.keys()
        if not val_req:
            LOG.warning(n_const.ERROR_DICT
                        [n_const.L2GW_INVALID_OVSDB_IDENTIFIER])
        return val_req

    def delete_network(self, context, ovsdb_identifier, logical_switch_uuid):
        """Handle RPC cast from plugin to delete a network."""
        if self._is_valid_request(ovsdb_identifier):
            with self._open_connection(ovsdb_identifier) as ovsdb_fd:
                ovsdb_fd.delete_logical_switch(logical_switch_uuid)

    def add_vif_to_gateway(self, context, ovsdb_identifier,
                           logical_switch_dict, locator_dict,
                           mac_dict):
        """Handle RPC cast from plugin to insert neutron port MACs."""
        if self._is_valid_request(ovsdb_identifier):
            with self._open_connection(ovsdb_identifier) as ovsdb_fd:
                ovsdb_fd.insert_ucast_macs_remote(logical_switch_dict,
                                                  locator_dict,
                                                  mac_dict)

    def delete_vif_from_gateway(self, context, ovsdb_identifier,
                                logical_switch_uuid, mac):
        """Handle RPC cast from plugin to delete neutron port MACs."""
        if self._is_valid_request(ovsdb_identifier):
            with self._open_connection(ovsdb_identifier) as ovsdb_fd:
                ovsdb_fd.delete_ucast_macs_remote(logical_switch_uuid, mac)

    def update_vif_to_gateway(self, context, ovsdb_identifier,
                              locator_dict, mac_dict):
        """Handle RPC cast from plugin to update neutron port MACs.

        for VM migration.
        """
        if self._is_valid_request(ovsdb_identifier):
            with self._open_connection(ovsdb_identifier) as ovsdb_fd:
                ovsdb_fd.update_ucast_macs_remote(locator_dict,
                                                  mac_dict)

    def update_connection_to_gateway(self, context, ovsdb_identifier,
                                     logical_switch_dict, locator_dicts,
                                     mac_dicts, port_dicts):
        """Handle RPC cast from plugin.

        Handle RPC cast from plugin to connect/disconnect a network
        to/from an L2 gateway.
        """
        if self._is_valid_request(ovsdb_identifier):
            with self._open_connection(ovsdb_identifier) as ovsdb_fd:
                ovsdb_fd.update_connection_to_gateway(logical_switch_dict,
                                                      locator_dicts,
                                                      mac_dicts,
                                                      port_dicts)

    def agent_to_plugin_rpc(self, ovsdb_data):
        self.plugin_rpc.update_ovsdb_changes(ctx.get_admin_context(),
                                             ovsdb_data)
