import unittest

from offgrid.netmap import (
    Connection,
    describe_sources,
    parse_netstat,
    parse_ss,
    render,
    render_map,
    risk,
)

SS = """\
Netid State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
udp   UNCONN 0      0          127.0.0.54:53       0.0.0.0:*
tcp   LISTEN 0      128          0.0.0.0:22         0.0.0.0:*
tcp   LISTEN 0      4096       127.0.0.1:8080       0.0.0.0:*
tcp   LISTEN 0      4096             [::]:8776        [::]:*
tcp   ESTAB  0      0         192.168.1.5:50122   93.184.216.34:443 users:(("firefox",pid=4242,fd=55))
tcp   ESTAB  0      0        127.0.0.1:50123      127.0.0.1:8080 users:(("curl",pid=99,fd=3))
"""

NETSTAT = """\
Active Internet connections (servers and established)
Proto Recv-Q Send-Q Local Address           Foreign Address         State
tcp        0      0 0.0.0.0:22              0.0.0.0:*               LISTEN
tcp        0      0 127.0.0.1:8080          0.0.0.0:*               LISTEN
tcp        0      0 10.0.0.2:5050           1.1.1.1:443             ESTABLISHED
"""


class ParseTests(unittest.TestCase):
    def test_ss_lines(self):
        connections = parse_ss(SS)
        self.assertEqual(len(connections), 6)
        self.assertEqual(connections[3].local, "[::]:8776")
        self.assertEqual(connections[3].local_port, 8776)
        self.assertTrue(connections[3].listening)
        public = connections[4]
        self.assertEqual(public.process, "firefox")
        self.assertEqual(public.pid, 4242)
        self.assertTrue(public.remote_public)

    def test_netstat_lines(self):
        connections = parse_netstat(NETSTAT)
        self.assertEqual(len(connections), 3)
        self.assertEqual(connections[0].local_port, 22)
        self.assertTrue(connections[2].remote_public)


class RiskTests(unittest.TestCase):
    def test_wildcard_listener_is_called_out(self):
        connection = Connection("tcp", "0.0.0.0:8776", "0.0.0.0:*", "LISTEN")
        self.assertTrue(connection.wildcard)
        self.assertIn("reachable from your network", risk(connection))

    def test_loopback_listener_is_quiet(self):
        connection = Connection("tcp", "127.0.0.1:8080", "0.0.0.0:*", "LISTEN")
        self.assertEqual(risk(connection), "")
        self.assertEqual(connection.label(), "llama.cpp model server")

    def test_private_peer_is_quiet(self):
        connection = Connection("tcp", "192.168.1.5:5000", "192.168.1.20:8776", "ESTAB")
        self.assertEqual(risk(connection), "")

    def test_public_peer_is_flagged(self):
        connection = Connection("tcp", "192.168.1.5:5000", "93.184.216.34:443", "ESTAB")
        self.assertIn("leaves your local network", risk(connection))


class MapTests(unittest.TestCase):
    def test_map_lists_listeners_and_live_connections(self):
        lines = render_map(parse_ss(SS), "test-machine")
        text = "\n".join(lines)
        self.assertIn("test-machine", text)
        self.assertIn("listening (3)", text)
        self.assertIn("established (2)", text)
        self.assertIn("llama.cpp model server", text)
        self.assertIn("firefox, pid 4242", text)
        self.assertIn("reachable from your network", text)
        self.assertIn("leaves your local network", text)

    def test_empty_map_explains_itself(self):
        text = "\n".join(render_map([], "test-machine"))
        self.assertIn("no connection table available", text)

    def test_sources_are_honest_about_limits(self):
        text = "\n".join(describe_sources(parse_ss(SS), "ss"))
        self.assertIn("source: ss", text)
        self.assertIn("requires root", text)
        self.assertIn("other devices", text)

    def test_render_returns_lines(self):
        self.assertTrue(all(isinstance(line, str) for line in render(parse_ss(SS), "m")))
        self.assertEqual(render(parse_ss(SS), "m"), render_map(parse_ss(SS), "m"))


if __name__ == "__main__":
    unittest.main()
