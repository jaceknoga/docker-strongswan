"""
This module contains everything needed to configure 'strongswan'.
Author: Sascha Falk <sascha@falk-online.eu>
License: MIT License
"""

import os
import re
import socket

from netaddr import IPAddress, IPNetwork, AddrFormatError
from mako.template import Template
from OpenSSL import crypto, SSL
from subprocess import run, DEVNULL
from ..cc_helpers import read_text_file, write_text_file, \
                         get_env_setting_bool, get_env_setting_integer, get_env_setting_string, \
                         iptables_run, iptables_add, ip6tables_run, ip6tables_add, \
                         does_mount_point_exist, is_mount_point_readonly, \
                         load_kernel_module, resolve_hostnames
from ..cc_log import Log
from ..cc_service import Service
from .cc_ca import CertificateAuthority

# ---------------------------------------------------------------------------------------------------------------------

# keys/certificates for use in production environments
EXTERNAL_PKI_BASE_DIR          = "/data/external_ca"
EXTERNAL_PKI_CA_CERT_FILE      = os.path.join(EXTERNAL_PKI_BASE_DIR, "ca-cert.pem")
EXTERNAL_PKI_SERVER_CERT_FILE  = os.path.join(EXTERNAL_PKI_BASE_DIR, "server", "cert.pem")
EXTERNAL_PKI_SERVER_KEY_FILE   = os.path.join(EXTERNAL_PKI_BASE_DIR, "server", "key.pem")

# configuration files
IPSEC_CONF_PATH                  = "/etc/ipsec.conf"
IPSEC_CONF_TEMPLATE_PATH         = "/etc/ipsec.conf.mako"
IPSEC_SECRETS_PATH               = "/etc/ipsec.secrets"
IPSEC_SECRETS_TEMPLATE_PATH      = "/etc/ipsec.secrets.mako"
STRONGSWAN_CONF_PATH             = "/etc/strongswan.conf"
STRONGSWAN_CONF_TEMPLATE_PATH    = "/etc/strongswan.conf.mako"
NAMED_CONF_OPTIONS_PATH          = "/etc/bind/named.conf.options"
NAMED_CONF_OPTIONS_TEMPLATE_PATH = "/etc/bind/named.conf.options.mako"
NDPPD_CONF_PATH                  = "/etc/ndppd.conf"
NDPPD_CONF_TEMPLATE_PATH         = "/etc/ndppd.conf.mako"
SUPERVISORD_NDPPD_CONF_PATH      = "/etc/supervisor/conf.d/ndppd.conf"

# IP Addresses
IPV6_NETWORK_GUA                 = IPNetwork("2000::/3")
IPV6_NETWORK_SITE_LOCAL          = IPNetwork("fc00::/7")

# ---------------------------------------------------------------------------------------------------------------------

# line used to separate blocks of information in the log
SEPARATOR_LINE = "----------------------------------------------------------------------------------------------------------------------"

# ---------------------------------------------------------------------------------------------------------------------


# name of the service
service_name = "strongswan"

# determines whether the service is run by the startup script
enabled = True

def get_service():
    "Returns an instance of the service provided by the service plugin."
    return StrongSwan()


###################################################################################################################################################################################
# Service Class
###################################################################################################################################################################################


class StrongSwan(Service):

    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def prepare(self):
        """
        Reads environment variables and checks preconditions the following call to configure() needs to succeed. In case
        of anything being screwed in the configuration or system, this method should throw an exception to abort starting
        up before configure() modifies any configuration files.
        """

        # ALLOW_INTERCLIENT_COMMUNICATION
        # -------------------------------------------------------------------------------------------------------------
        self._allow_interclient_communication = get_env_setting_bool("ALLOW_INTERCLIENT_COMMUNICATION", False)

        # CLIENT_SUBNET_IPV4
        # -----------------------------------------------------------------------------------------
        self._client_subnet_ipv4 = get_env_setting_string("CLIENT_SUBNET_IPV4", "10.0.0.0/24")
        try:
            self._client_subnet_ipv4 = IPNetwork(self._client_subnet_ipv4)
        except AddrFormatError:
            Log.write_error("The specified network ({0}) is not a valid IPv4 network.", self._client_subnet_ipv4)
            raise

        # CLIENT_SUBNET_IPV6
        # (must be either in the Global Unicast Address (GUA) range or in the site-local range)
        # -------------------------------------------------------------------------------------------------------------

        # read environment variable
        self._client_subnet_ipv6 = get_env_setting_string("CLIENT_SUBNET_IPV6", "fd00:DEAD:BEEF:AFFE::/64")
        try:
            self._client_subnet_ipv6 = IPNetwork(self._client_subnet_ipv6)
        except AddrFormatError:
            Log.write_error("The specified network ({0}) is not a valid IPv6 network.", self._client_subnet_ipv6)
            raise

        # check whether the specified subnet belongs is in the GUA range or the ULA range
        if self._client_subnet_ipv6.is_unicast() and not self._client_subnet_ipv6.is_private():
            self._client_subnet_ipv6_is_gua = True
            self._client_subnet_ipv6_is_site_local = False
        elif self._client_subnet_ipv6.is_private():
            self._client_subnet_ipv6_is_gua = False
            self._client_subnet_ipv6_is_site_local = True
        else:
            Log.write_error("The specified network ({0}) is neither in the GUA range ({1}) nor in the site-local range ({2}).",
                            str(self._client_subnet_ipv6), str(IPV6_NETWORK_GUA), str(IPV6_NETWORK_SITE_LOCAL))
            raise RuntimeError()

        # USE_DOCKER_DNS
        # -------------------------------------------------------------------------------------------------------------
        self._use_docker_dns = get_env_setting_bool("USE_DOCKER_DNS", True)

        # DNS_SERVERS
        # -------------------------------------------------------------------------------------------------------------
        if self._use_docker_dns: self._dns_servers = "127.0.0.11"
        else:                    self._dns_servers = get_env_setting_string("DNS SERVERS", "8.8.8.8, 8.8.4.4, 2001:4860:4860::8888, 2001:4860:4860::8844")
        self._dns_servers = [ s.strip() for s in self._dns_servers.split(",") ]

# TODO: add validation

        # USE_INTERNAL_PKI
        # -------------------------------------------------------------------------------------------------------------
        self._use_internal_pki   = get_env_setting_bool("USE_INTERNAL_PKI", True)

        # VPN_HOSTNAMES
        # -------------------------------------------------------------------------------------------------------------
        self._vpn_hostnames = get_env_setting_string("VPN_HOSTNAMES", socket.gethostname())
        self._vpn_hostnames = [ s.strip() for s in self._vpn_hostnames.split(",") ]

# TODO: add validation

        # PROTECT_CLIENTS_FROM_INTERNET
        # -------------------------------------------------------------------------------------------------------------
        self._protect_clients_from_internet = get_env_setting_bool("PROTECT_CLIENTS_FROM_INTERNET", True)

        # determine IP addresses that map to the configured hostnames
        # -------------------------------------------------------------------------------------------------------------
        Log.write_note("Looking up IP addresses of the specified hostnames...")
        self._ip_addresses_by_hostname = resolve_hostnames(self._vpn_hostnames)
        for hostname,(ipv4_addresses,ipv6_addresses) in self._ip_addresses_by_hostname.items():
            if len(ipv4_addresses) > 0:
                Log.write_note("- {0} : {1}".format(hostname, ",".join(ipv4_addresses)))
            if len(ipv6_addresses) > 0:
                Log.write_note("- {0} : {1}".format(hostname, ",".join(ipv6_addresses)))

        # load af_key module is loaded (kernel support for IPSec)
        # -------------------------------------------------------------------------------------------------------------
        load_kernel_module("af_key")


    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

    def configure(self):
        """
        Creates/modifies the configuration file according to environment variables.
        """

        # setup cryptographic stuff
        # -------------------------------------------------------------------------------------------------------------
        if self._use_internal_pki:
            self.init_pki_internal()
        else:
            self.init_pki_external();

        # determine the start and the end of the client ip range
        # (the first address becomes the IP of the VPN server itself)
        # -------------------------------------------------------------------------------------------------------------

        # IPv4
        self._own_ip_in_client_subnet_ipv4 = self._client_subnet_ipv4[1]
        self._client_ip_range_start_ipv4 = self._client_subnet_ipv4[2]
        self._client_ip_range_end_ipv4 = self._client_subnet_ipv4[-1]

        # IPv6
        effective_client_subnet_ipv6 = self._client_subnet_ipv6
        if self._client_subnet_ipv6.prefixlen < 96:
          # subnet is too large, strongswan can only handle subnets up to /96 => use a smaller subnet
          effective_client_subnet_ipv6 = next(effective_client_subnet_ipv6.subnet(96))
          
        self._own_ip_in_client_subnet_ipv6 = effective_client_subnet_ipv6[1]
        self._client_ip_range_start_ipv6 = effective_client_subnet_ipv6[2]
        self._client_ip_range_end_ipv6 = effective_client_subnet_ipv6[-1]

        # prepare context for the template engine that will generate strongswan.conf and ipsec.conf
        # -------------------------------------------------------------------------------------------------------------
        template_context = {
          "ca_key_path"                    : self._ca_key_path,
          "ca_cert_path"                   : self._ca_cert_path,
          "server_key_path"                : self._server_key_path,
          "server_cert_path"               : self._server_cert_path,
          "dns_servers"                    : self._dns_servers,
          "ip_addresses_by_hostname"       : self._ip_addresses_by_hostname,
          "client_subnet_ipv4"             : self._client_subnet_ipv4,
          "client_subnet_ipv6"             : self._client_subnet_ipv6,
          "own_ip_in_client_subnet_ipv4"   : self._own_ip_in_client_subnet_ipv4,
          "client_ip_range_start_ipv4"     : self._client_ip_range_start_ipv4,
          "client_ip_range_end_ipv4"       : self._client_ip_range_end_ipv4,
          "own_ip_in_client_subnet_ipv6"   : self._own_ip_in_client_subnet_ipv6,
          "client_ip_range_start_ipv6"     : self._client_ip_range_start_ipv6,
          "client_ip_range_end_ipv6"       : self._client_ip_range_end_ipv6,
        }

        # generate bind.conf.options
        # -------------------------------------------------------------------------------------------------------------
        rendered = Template(filename = NAMED_CONF_OPTIONS_TEMPLATE_PATH).render(**template_context)
        with open(NAMED_CONF_OPTIONS_PATH, "wt") as f:
            f.write(rendered)

        # generate ipsec.conf
        # -------------------------------------------------------------------------------------------------------------
        rendered = Template(filename = IPSEC_CONF_TEMPLATE_PATH).render(**template_context)
        with open(IPSEC_CONF_PATH, "wt") as f:
            f.write(rendered)

        # generate ipsec.secrets
        # -------------------------------------------------------------------------------------------------------------
        rendered = Template(filename = IPSEC_SECRETS_TEMPLATE_PATH).render(**template_context)
        with open(IPSEC_SECRETS_PATH, "wt") as f:
            f.write(rendered)

        # generate strongswan.conf
        # -------------------------------------------------------------------------------------------------------------
        rendered = Template(filename = STRONGSWAN_CONF_TEMPLATE_PATH).render(**template_context)
        with open(STRONGSWAN_CONF_PATH, "wt") as f:
            f.write(rendered)

        # generate ndppd.conf
        # -------------------------------------------------------------------------------------------------------------
        rendered = Template(filename = NDPPD_CONF_TEMPLATE_PATH).render(**template_context)
        with open(NDPPD_CONF_PATH, "wt") as f:
            f.write(rendered)

        # disable ndppd, if VPN clients do not have global addresses (no need for neighbor discovery)
        # -------------------------------------------------------------------------------------------------------------
        if not self._client_subnet_ipv6_is_gua:
            os.rename(SUPERVISORD_NDPPD_CONF_PATH, SUPERVISORD_NDPPD_CONF_PATH + ".inactive")

        # remount /proc/sys read-write to enable 'sysctl' to work properly
        # (only needed, if the container is not run in privileged mode)
        # -------------------------------------------------------------------------------------------------------------
        sys_proc_remounted_rw = False
        if does_mount_point_exist("/proc/sys") and is_mount_point_readonly("/proc/sys"):        
            Log.write_note("Remounting /proc/sys read-write...")
            run(["mount", "-o", "remount,rw", "/proc/sys"], check=True, stdout=DEVNULL)
            sys_proc_remounted_rw = True

        # configure networking
        # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

        Log.write_note("Configuring networking...")

        # add a dummy device with an ip address for the vpn server in the client network
        run(["ip", "link", "add", "type", "dummy"], check=True, stdout=DEVNULL)
        run(["ip", "addr", "add", str(self._own_ip_in_client_subnet_ipv4) + "/" + str(self._client_subnet_ipv4.prefixlen), "dev", "dummy0"], check=True, stdout=DEVNULL)
        run(["ip", "addr", "add", str(self._own_ip_in_client_subnet_ipv6) + "/" + str(self._client_subnet_ipv6.prefixlen), "dev", "dummy0"], check=True, stdout=DEVNULL)
        run(["ip", "link", "set", "up", "dummy0"], check=True, stdout=DEVNULL)
        run(["ip", "route", "add", str(self._own_ip_in_client_subnet_ipv4), "dev", "dummy0"], check=True, stdout=DEVNULL)
        run(["ip", "route", "add", str(self._own_ip_in_client_subnet_ipv6), "dev", "dummy0"], check=True, stdout=DEVNULL)

        # enable forwarding
        run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=True, stdout=DEVNULL)
        run(["sysctl", "-w", "net.ipv6.conf.default.forwarding=1"], check=True, stdout=DEVNULL)
        run(["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"], check=True, stdout=DEVNULL)

        # accept router advertisements on eth0, although we're forwarding packets
        run(["sysctl", "-w", "net.ipv6.conf.eth0.accept_ra=2"], check=True, stdout=DEVNULL)

        # do not accept ICMP redirects (prevent MITM attacks)
        run(["sysctl", "-w", "net.ipv4.conf.all.accept_redirects=0"], check=True, stdout=DEVNULL)
        run(["sysctl", "-w", "net.ipv6.conf.all.accept_redirects=0"], check=True, stdout=DEVNULL)

        # do not send ICMP redirects (we are not a router that should redirect others)
        # (in IPv6 redirects are mandatory for routers)
        run(["sysctl", "-w", "net.ipv4.conf.all.send_redirects=0"], check=True, stdout=DEVNULL)

        # disable Path MTU discovery to prevent packet fragmentation problems
        run(["sysctl", "-w", "net.ipv4.ip_no_pmtu_disc=1"], check=True, stdout=DEVNULL)

        # configure firewalling
        # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

        Log.write_note("=> Configuring firewall")

        # filter all packets that have RH0 headers (deprecated, can be used for DoS attacks)
        # -------------------------------------------------------------------------------------------------------------
        ip6tables_add("INPUT",   "DROP", ["-m", "rt", "--rt-type", "0"], "RH0 Exploit Protection")
        ip6tables_add("FORWARD", "DROP", ["-m", "rt", "--rt-type", "0"], "RH0 Exploit Protection")
        ip6tables_add("OUTPUT",  "DROP", ["-m", "rt", "--rt-type", "0"], "RH0 Exploit Protection")

        # protect against spoofing attacks
        # -------------------------------------------------------------------------------------------------------------

        # prevent attacker from using the loopback address as source address
        iptables_add( "INPUT",   "DROP", ["!", "-i", "lo", "-s", "127.0.0.0/8"], "Anti-Spoofing")
        iptables_add( "FORWARD", "DROP", ["!", "-i", "lo", "-s", "127.0.0.0/8"], "Anti-Spoofing")
        ip6tables_add("INPUT",   "DROP", ["!", "-i", "lo", "-s", "::1/128"],     "Anti-Spoofing")
        ip6tables_add("FORWARD", "DROP", ["!", "-i", "lo", "-s", "::1/128"],     "Anti-Spoofing")

        # prevent attacker from using a VPN client address as source address
        iptables_add( "INPUT",   "DROP", ["-s", str(self._client_subnet_ipv4), "-m", "policy", "--dir", "in", "--pol", "none"], "Anti-Spoofing")
        iptables_add( "FORWARD", "DROP", ["-s", str(self._client_subnet_ipv4), "-m", "policy", "--dir", "in", "--pol", "none"], "Anti-Spoofing")
        ip6tables_add("INPUT",   "DROP", ["-s", str(self._client_subnet_ipv6), "-m", "policy", "--dir", "in", "--pol", "none"], "Anti-Spoofing")
        ip6tables_add("FORWARD", "DROP", ["-s", str(self._client_subnet_ipv6), "-m", "policy", "--dir", "in", "--pol", "none"], "Anti-Spoofing")

        # allow localhost to access everything
        # -------------------------------------------------------------------------------------------------------------
        iptables_add( "INPUT", "ACCEPT", ["-i", "lo"])
        ip6tables_add("INPUT", "ACCEPT", ["-i", "lo"])

        # allow IPSec related traffic
        # -------------------------------------------------------------------------------------------------------------
        iptables_add( "INPUT", "ACCEPT", ["-p", "udp", "--dport", "500"])
        iptables_add( "INPUT", "ACCEPT", ["-p", "udp", "--dport", "4500"])
        iptables_add( "INPUT", "ACCEPT", ["-p", "esp"])
        ip6tables_add("INPUT", "ACCEPT", ["-p", "udp", "--dport", "500"])
        ip6tables_add("INPUT", "ACCEPT", ["-p", "udp", "--dport", "4500"])
        ip6tables_add("INPUT", "ACCEPT", ["-p", "esp"])

        # allow packets that belong to already existing connections
        # -------------------------------------------------------------------------------------------------------------
        iptables_add( "INPUT",   "DROP",   ["-m", "conntrack", "--ctstate", "INVALID"])
        iptables_add( "INPUT",   "ACCEPT", ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"])
        iptables_add( "FORWARD", "DROP",   ["-m", "conntrack", "--ctstate", "INVALID"])
        iptables_add( "FORWARD", "ACCEPT", ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"])
        ip6tables_add("INPUT",   "DROP",   ["-m", "conntrack", "--ctstate", "INVALID"])
        ip6tables_add("INPUT",   "ACCEPT", ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"])
        ip6tables_add("FORWARD", "DROP",   ["-m", "conntrack", "--ctstate", "INVALID"])
        ip6tables_add("FORWARD", "ACCEPT", ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"])

        # allow VPN clients to access the DNS server
        # -------------------------------------------------------------------------------------------------------------
        iptables_add( "INPUT", "ACCEPT", ["-p", "udp", "-s", str(self._client_subnet_ipv4), "--dport", "53", "-m", "policy", "--dir", "in", "--pol", "ipsec"])
        iptables_add( "INPUT", "ACCEPT", ["-p", "tcp", "-s", str(self._client_subnet_ipv4), "--dport", "53", "-m", "policy", "--dir", "in", "--pol", "ipsec"])
        ip6tables_add("INPUT", "ACCEPT", ["-p", "udp", "-s", str(self._client_subnet_ipv6), "--dport", "53", "-m", "policy", "--dir", "in", "--pol", "ipsec"])
        ip6tables_add("INPUT", "ACCEPT", ["-p", "tcp", "-s", str(self._client_subnet_ipv6), "--dport", "53", "-m", "policy", "--dir", "in", "--pol", "ipsec"])

        # block packets between VPN clients (if requested)
        # -------------------------------------------------------------------------------------------------------------
        if not self._allow_interclient_communication:
            iptables_add( "FORWARD", "DROP", ["-s", str(self._client_subnet_ipv4), "-d", str(self._client_subnet_ipv4)])
            ip6tables_add("FORWARD", "DROP", ["-s", str(self._client_subnet_ipv6), "-d", str(self._client_subnet_ipv6)])

        # allow ICMP packets
        # -------------------------------------------------------------------------------------------------------------

        # ICMP Type | INPUT | FORWARD | Description
        # -----------------------------------------------------------------------------------------
        #       0   |  yes  |   yes   | echo reply
        #       3   |  yes  |   yes   | destination unreachable
        #       8   |  yes  |   yes   | echo request (protect against ping-of-death)
        #      11   |  yes  |   yes   | time exceeded
        #      12   |  yes  |   yes   | parameter problem
        #      30   |  yes  |   yes   | traceroute
        # -----------------------------------------------------------------------------------------

        iptables_run(["-N", "AllowICMP_I"])
        iptables_add("AllowICMP_I", "ACCEPT", ["-p", "icmp", "--icmp-type", "0"])
        iptables_add("AllowICMP_I", "ACCEPT", ["-p", "icmp", "--icmp-type", "3"])
        iptables_add("AllowICMP_I", "ACCEPT", ["-p", "icmp", "--icmp-type", "8", "-m", "limit", "--limit", "5/sec", "--limit-burst", "20"])
        iptables_add("AllowICMP_I", "ACCEPT", ["-p", "icmp", "--icmp-type", "11"])
        iptables_add("AllowICMP_I", "ACCEPT", ["-p", "icmp", "--icmp-type", "12"])
        iptables_add("AllowICMP_I", "ACCEPT", ["-p", "icmp", "--icmp-type", "30"])
        iptables_add("AllowICMP_I", "DROP")
        iptables_add("INPUT", "AllowICMP_I", ["-p", "icmp"])

        iptables_run(["-N", "AllowICMP_F"])
        iptables_add("AllowICMP_F", "ACCEPT", ["-p", "icmp", "--icmp-type", "0"])
        iptables_add("AllowICMP_F", "ACCEPT", ["-p", "icmp", "--icmp-type", "3"])
        iptables_add("AllowICMP_F", "ACCEPT", ["-p", "icmp", "--icmp-type", "8", "-m", "limit", "--limit", "5/sec", "--limit-burst", "20"])
        iptables_add("AllowICMP_F", "ACCEPT", ["-p", "icmp", "--icmp-type", "11"])
        iptables_add("AllowICMP_F", "ACCEPT", ["-p", "icmp", "--icmp-type", "12"])
        iptables_add("AllowICMP_F", "ACCEPT", ["-p", "icmp", "--icmp-type", "30"])
        iptables_add("AllowICMP_F", "DROP")
        iptables_add("FORWARD", "AllowICMP_F", ["-p", "icmp"])

        #  ICMPv6 Type | INPUT | FORWARD | Description
        # -----------------------------------------------------------------------------------------
        #         1    |  yes  |   yes   | destination unreachable
        #         2    |  yes  |   yes   | packet too big
        #         3    |  yes  |   yes   | time exceeded
        #         4    |  yes  |   yes   | parameter problem
        #       128    |  yes  |   yes   | echo request (protect against ping-of-death)
        #       129    |  yes  |   yes   | echo reply
        #       130    |  yes  |   yes   | multicast listener query
        #       131    |  yes  |   yes   | version 1 multicast listener report
        #       132    |  yes  |   yes   | multicast listener done
        #       133    |  yes  |   no    | router solicitation
        #       134    |  yes  |   no    | router advertisement
        #       135    |  yes  |   no    | neighbor solicitation
        #       136    |  yes  |   no    | neighbor advertisement
        #       151    |  yes  |   no    | multicast router advertisement
        #       152    |  yes  |   no    | multicast router solicitation
        #       153    |  yes  |   no    | multicast router termination
        # -----------------------------------------------------------------------------------------
        ip6tables_run(["-N", "AllowICMP_I"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "1"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "2"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "3"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "4"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "128", "-m", "limit", "--limit", "5/sec", "--limit-burst", "10"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "129"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "130"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "131"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "132"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "133"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "134"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "135"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "136"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "151"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "152"])
        ip6tables_add("AllowICMP_I", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "153"])
        ip6tables_add("AllowICMP_I", "DROP")
        ip6tables_add("INPUT", "AllowICMP_I", ["-p", "icmpv6"])

        ip6tables_run(["-N", "AllowICMP_F"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "1"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "2"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "3"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "4"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "128", "-m", "limit", "--limit", "5/sec", "--limit-burst", "10"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "129"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "130"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "131"])
        ip6tables_add("AllowICMP_F", "ACCEPT", ["-p", "icmpv6", "--icmpv6-type", "132"])
        ip6tables_add("AllowICMP_F", "DROP")
        ip6tables_add("FORWARD", "AllowICMP_F", ["-p", "icmpv6"])

        # allow VPN clients to initiate new connections
        # -------------------------------------------------------------------------------------------------------------
        iptables_add("FORWARD", "ACCEPT", [
                     "-s", str(self._client_subnet_ipv4),
                     "-m", "conntrack", "--ctstate", "NEW",
                     "-m", "policy", "--dir", "in", "--pol", "ipsec"])

        ip6tables_add("FORWARD", "ACCEPT", [
                      "-s", str(self._client_subnet_ipv6),
                      "-m", "conntrack", "--ctstate", "NEW",
                      "-m", "policy", "--dir", "in", "--pol", "ipsec"])

        # allow packets that initiate new connections from the internet to VPN clients, if protection is disabled
        # -------------------------------------------------------------------------------------------------------------
        if not self._protect_clients_from_internet:

            iptables_add("FORWARD", "ACCEPT", [
                         "-d", str(self._client_subnet_ipv4),
                         "-m", "conntrack", "--ctstate", "NEW"])

            ip6tables_add("FORWARD", "ACCEPT", [
                          "-d", str(self._client_subnet_ipv6),
                          "-m", "conntrack", "--ctstate", "NEW"])

        # drop everything else
        # -------------------------------------------------------------------------------------------------------------
        iptables_add( "INPUT",   "DROP")
        iptables_add( "FORWARD", "DROP")
        ip6tables_add("INPUT",   "DROP")
        ip6tables_add("FORWARD", "DROP")

        # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
        # Packet Mangling
        # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

        # Reduce the size of TCP packets by adjusting the packets' maximum segment size to prevent IP packet fragmentation on some clients
        # This prevents issues with some VPN clients, but it is controversially discussed (google 'MSS Clamping' for details).
        # Many tunnel implementation use a tunnel MTU of 1400 bytes, so the following MSS values should be reasonable:
        # - TCP MSS (IPv4): 1400 bytes (tunnel MTU) - 20 bytes (IPv4 header) - 20 bytes (TCP header) = 1360 bytes
        # - TCP MSS (IPv6): 1400 bytes (tunnel MTU) - 40 bytes (IPv6 header) - 20 bytes (TCP header) = 1340 bytes
        # -------------------------------------------------------------------------------------------------------------
        iptables_run([ "-t", "mangle",
                       "-A", "FORWARD",
                       "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
                       "-s", str(self._client_subnet_ipv4),
                       "-m", "policy", "--dir", "in", "--pol", "ipsec",
                       "-m", "tcpmss", "--mss", "1361:1500",
                       "-j", "TCPMSS", "--set-mss", "1360"])

        ip6tables_run(["-t", "mangle",
                       "-A", "FORWARD",
                       "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
                       "-s", str(self._client_subnet_ipv6),
                       "-m", "policy", "--dir", "in", "--pol", "ipsec",
                       "-m", "tcpmss", "--mss", "1341:1500",
                       "-j", "TCPMSS", "--set-mss", "1340"])

        # configure masquerading to allow clients to access the internet
        # -------------------------------------------------------------------------------------------------------------

        Log.write_note("=> Enabling masquerading for IPv4")

        iptables_add("POSTROUTING", "ACCEPT", [
                     "-t", "nat",
                     "-s", str(self._client_subnet_ipv4),
                     "-m", "policy", "--dir", "out", "--pol", "ipsec"])

        iptables_add("POSTROUTING", "MASQUERADE", [
                     "-t", "nat",
                     "-s", str(self._client_subnet_ipv4)])

        if self._client_subnet_ipv6_is_site_local:

            # site local network (ip addresses are not valid on the internet)
            # => enable masquerading

            Log.write_note("=> Enabling masquerading for IPv6")

            ip6tables_add("POSTROUTING", "ACCEPT", [
                          "-t", "nat",
                          "-s", str(self._client_subnet_ipv6),
                          "-m", "policy", "--dir", "out", "--pol", "ipsec"])

            ip6tables_add("POSTROUTING", "MASQUERADE", [
                          "-t", "nat",
                          "-s", str(self._client_subnet_ipv6)])

        # remount /proc/sys read-only again
        # -------------------------------------------------------------------------------------------------------------
        if sys_proc_remounted_rw:
            Log.write_note("Remounting /proc/sys read-only...")
            run(["mount", "-o", "remount,ro", "/proc/sys"], check=True, stdout=DEVNULL)


    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------
    # -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------


    def init_pki_internal(self):
        """
        Initializes the internal Public-Key Infrastructure (PKI) and loads/generates the appropriate keys and certificates
        needed to run the VPN server.
        """

        ca = CertificateAuthority()

        # get the key/certificate of the VPN server (create it, if necessary)
        sans = [ "DNS:" + s for s in self._vpn_hostnames  ]
        vpn_server_data = ca.get_vpn_server_data(sans)

        self._server_key       = vpn_server_data["key"]
        self._server_key_path  = vpn_server_data["key path"]
        self._server_cert      = vpn_server_data["certificate"]
        self._server_cert_path = vpn_server_data["certificate path"]
        self._ca_key           = ca.ca_key
        self._ca_key_path      = ca.ca_key_path
        self._ca_cert          = ca.ca_cert
        self._ca_cert_path     = ca.ca_cert_path

        # log the certificate of the VPN server
        dump = crypto.dump_certificate(crypto.FILETYPE_TEXT, self._server_cert).decode('utf-8')
        Log.write_note("Certificate of the VPN server\n{1}\n{0}\n{1}", dump, SEPARATOR_LINE)

    # ---------------------------------------------------------------------------------------------------------------------

    def init_pki_external(self):
        """
        """

        pass


###################################################################################################################################################################################
# Helper functions
###################################################################################################################################################################################

