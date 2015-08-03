#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


import os
import re
import socket
import ssl
import sys
import threading
import time
import traceback

import requests
ca_path = requests.certs.where()

import util
import x509


def Connection(server, queue, config_path):
    """Makes asynchronous connections to a remote remote electrum server.
    Returns the running thread that is making the connection.

    Once the thread has connected, it finishes, placing a tuple on the
    queue of the form (server, socket), where socket is None if
    connection failed.
    """
    host, port, protocol = server.split(':')
    if not protocol in 'st':
        raise Exception('Unknown protocol: %s' % protocol)
    c = TcpConnection(server, queue, config_path)
    c.start()
    return c

class TcpConnection(threading.Thread):

    def __init__(self, server, queue, config_path):
        threading.Thread.__init__(self)
        self.daemon = True
        self.config_path = config_path
        self.queue = queue
        self.server = server
        self.host, self.port, self.protocol = self.server.split(':')
        self.host = str(self.host)
        self.port = int(self.port)
        self.use_ssl = (self.protocol == 's')

    def print_error(self, *msg):
        util.print_error("[%s]" % self.host, *msg)

    def check_host_name(self, peercert, name):
        """Simple certificate/host name checker.  Returns True if the
        certificate matches, False otherwise.  Does not support
        wildcards."""
        # Check that the peer has supplied a certificate.
        # None/{} is not acceptable.
        if not peercert:
            return False
        if peercert.has_key("subjectAltName"):
            for typ, val in peercert["subjectAltName"]:
                if typ == "DNS" and val == name:
                    return True
        else:
            # Only check the subject DN if there is no subject alternative
            # name.
            cn = None
            for attr, val in peercert["subject"]:
                # Use most-specific (last) commonName attribute.
                if attr == "commonName":
                    cn = val
            if cn is not None:
                return cn == name
        return False

    def get_simple_socket(self):
        try:
            l = socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            self.print_error("cannot resolve hostname")
            return
        for res in l:
            try:
                s = socket.socket(res[0], socket.SOCK_STREAM)
                s.connect(res[4])
                s.settimeout(2)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                return s
            except BaseException as e:
                continue
        else:
            self.print_error("failed to connect", str(e))

    def get_socket(self):
        if self.use_ssl:
            cert_path = os.path.join(self.config_path, 'certs', self.host)
            if not os.path.exists(cert_path):
                is_new = True
                s = self.get_simple_socket()
                if s is None:
                    return
                # try with CA first
                try:
                    s = ssl.wrap_socket(s, ssl_version=ssl.PROTOCOL_TLSv1, cert_reqs=ssl.CERT_REQUIRED, ca_certs=ca_path, do_handshake_on_connect=True)
                except ssl.SSLError, e:
                    s = None
                if s and self.check_host_name(s.getpeercert(), self.host):
                    self.print_error("SSL certificate signed by CA")
                    return s

                # get server certificate.
                # Do not use ssl.get_server_certificate because it does not work with proxy
                s = self.get_simple_socket()
                if s is None:
                    return
                try:
                    s = ssl.wrap_socket(s, ssl_version=ssl.PROTOCOL_TLSv1, cert_reqs=ssl.CERT_NONE, ca_certs=None)
                except ssl.SSLError, e:
                    self.print_error("SSL error retrieving SSL certificate:", e)
                    return

                dercert = s.getpeercert(True)
                s.close()
                cert = ssl.DER_cert_to_PEM_cert(dercert)
                # workaround android bug
                cert = re.sub("([^\n])-----END CERTIFICATE-----","\\1\n-----END CERTIFICATE-----",cert)
                temporary_path = cert_path + '.temp'
                with open(temporary_path,"w") as f:
                    f.write(cert)
            else:
                is_new = False

        s = self.get_simple_socket()
        if s is None:
            return

        if self.use_ssl:
            try:
                s = ssl.wrap_socket(s,
                                    ssl_version=ssl.PROTOCOL_TLSv1,
                                    cert_reqs=ssl.CERT_REQUIRED,
                                    ca_certs= (temporary_path if is_new else cert_path),
                                    do_handshake_on_connect=True)
            except ssl.SSLError, e:
                self.print_error("SSL error:", e)
                if e.errno != 1:
                    return
                if is_new:
                    rej = cert_path + '.rej'
                    if os.path.exists(rej):
                        os.unlink(rej)
                    os.rename(temporary_path, rej)
                else:
                    with open(cert_path) as f:
                        cert = f.read()
                    try:
                        x = x509.X509()
                        x.parse(cert)
                    except:
                        traceback.print_exc(file=sys.stderr)
                        self.print_error("wrong certificate")
                        return
                    try:
                        x.check_date()
                    except:
                        self.print_error("certificate has expired:", cert_path)
                        os.unlink(cert_path)
                        return
                    self.print_error("wrong certificate")
                return
            except BaseException, e:
                self.print_error(e)
                if e.errno == 104:
                    return
                traceback.print_exc(file=sys.stderr)
                return

            if is_new:
                self.print_error("saving certificate")
                os.rename(temporary_path, cert_path)

        return s

    def run(self):
        socket = self.get_socket()
        if socket:
            self.print_error("connected")
        self.queue.put((self.server, socket))

class Interface:
    """The Interface class handles a socket connected to a single remote
    electrum server.  It's exposed API is:

    - Member functions close(), fileno(), get_responses(), has_timed_out(),
      ping_required(), queue_request(), send_requests()
    - Member variable server.
    """

    def __init__(self, server, socket):
        self.server = server
        self.host, _, _ = server.split(':')
        self.socket = socket

        self.pipe = util.SocketPipe(socket)
        self.pipe.set_timeout(0.0)  # Don't wait for data
        # Dump network messages.  Set at runtime from the console.
        self.debug = False
        self.message_id = 0
        self.unsent_requests = []
        self.unanswered_requests = {}
        # Set last ping to zero to ensure immediate ping
        self.last_request = time.time()
        self.last_ping = 0
        self.closed_remotely = False

    def print_error(self, *msg):
        util.print_error("[%s]" % self.host, *msg)

    def fileno(self):
        # Needed for select
        return self.socket.fileno()

    def close(self):
        if not self.closed_remotely:
            self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()

    def queue_request(self, request):
        '''Queue a request.'''
        self.request_time = time.time()
        self.unsent_requests.append(request)

    def send_requests(self):
        '''Sends all queued requests.  Returns False on failure.'''
        def copy_request(orig):
            # Replace ID after making copy - mustn't change caller's copy
            request = orig.copy()
            request['id'] = self.message_id
            self.message_id += 1
            if self.debug:
                self.print_error("-->", request, orig.get('id'))
            return request

        requests_as_sent = map(copy_request, self.unsent_requests)
        try:
            self.pipe.send_all(requests_as_sent)
        except socket.error, e:
            self.print_error("socket error:", e)
            return False
        # unanswered_requests stores the original unmodified user
        # request, keyed by wire ID
        for n, request in enumerate(self.unsent_requests):
            self.unanswered_requests[requests_as_sent[n]['id']] = request
        self.unsent_requests = []
        return True

    def ping_required(self):
        '''Maintains time since last ping.  Returns True if a ping should
        be sent.
        '''
        now = time.time()
        if now - self.last_ping > 60:
            self.last_ping = now
            return True
        return False

    def has_timed_out(self):
        '''Returns True if the interface has timed out.'''
        if (self.unanswered_requests and time.time() - self.request_time > 10
            and self.pipe.idle_time() > 10):
            self.print_error("timeout", len(self.unanswered_requests))
            return True

        return False

    def get_responses(self):
        '''Call if there is data available on the socket.  Returns a list of
        notifications and a list of responses.  The notifications are
        singleton unsolicited responses presumably as a result of
        prior subscriptions.  The responses are (request, response)
        pairs.  If the connection was closed remotely or the remote
        server is misbehaving, the last notification will be None.
        '''
        notifications, responses = [], []
        while True:
            try:
                response = self.pipe.get()
            except util.timeout:
                break
            if response is None:
                notifications.append(None)
                self.closed_remotely = True
                self.print_error("connection closed remotely")
                break
            if self.debug:
                self.print_error("<--", response)
            wire_id = response.pop('id', None)
            if wire_id is None:
                notifications.append(response)
            elif wire_id in self.unanswered_requests:
                request = self.unanswered_requests.pop(wire_id)
                responses.append((request, response))
            else:
                notifications.append(None)
                self.print_error("unknown wire ID", wire_id)
                break

        return notifications, responses


def check_cert(host, cert):
    try:
        x = x509.X509()
        x.parse(cert)
    except:
        traceback.print_exc(file=sys.stdout)
        return

    try:
        x.check_date()
        expired = False
    except:
        expired = True

    m = "host: %s\n"%host
    m += "has_expired: %s\n"% expired
    util.print_msg(m)


# Used by tests
def _match_hostname(name, val):
    if val == name:
        return True

    return val.startswith('*.') and name.endswith(val[1:])

def test_certificates():
    from simple_config import SimpleConfig
    config = SimpleConfig()
    mydir = os.path.join(config.path, "certs")
    certs = os.listdir(mydir)
    for c in certs:
        print c
        p = os.path.join(mydir,c)
        with open(p) as f:
            cert = f.read()
        check_cert(c, cert)

if __name__ == "__main__":
    test_certificates()
