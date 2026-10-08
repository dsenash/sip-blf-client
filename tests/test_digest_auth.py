import hashlib
import re
import unittest

from blf import BLFButton, BLFManager
from sip_client import SIPClient, build_digest_header, parse_digest_challenge


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


CHALLENGE = ('Digest realm="default", nonce="bbf37375-e73f-4263-a41a-c087664ea91d", '
             'algorithm=MD5, qop="auth"')

# Known vectors: HA1 = md5(user:realm:pass), HA2 = md5(method:uri).
# Without qop: resp = md5(HA1:nonce:HA2).
# With qop=auth: resp = md5(HA1:nonce:nc:cnonce:qop:HA2).
CRED = {"username": "200", "password": "secret123"}
URI = "sip:201@default"
EXPECT_NO_QOP = md5(md5("200:default:secret123") + ":nonce123abc:"
                    + md5("SUBSCRIBE:sip:201@default"))
EXPECT_QOP = md5(md5("200:default:secret123") + ":nonce123abc:00000001:abcd1234:"
                 "auth:" + md5("SUBSCRIBE:sip:201@default"))


class ParseDigestChallengeTest(unittest.TestCase):
    def test_parses_freeswitch_challenge(self):
        params = parse_digest_challenge(CHALLENGE)
        self.assertEqual(params["realm"], "default")
        self.assertEqual(params["nonce"], "bbf37375-e73f-4263-a41a-c087664ea91d")
        self.assertEqual(params["algorithm"], "MD5")
        self.assertEqual(params["qop"], "auth")

    def test_parses_challenge_without_qop(self):
        params = parse_digest_challenge(
            'Digest realm="default", nonce="nonce123abc", algorithm=MD5')
        self.assertEqual(params["nonce"], "nonce123abc")
        self.assertNotIn("qop", params)

    def test_missing_or_non_digest_header_is_empty(self):
        self.assertEqual(parse_digest_challenge(""), {})
        self.assertEqual(parse_digest_challenge(None), {})
        self.assertEqual(parse_digest_challenge("Basic realm=\"x\""), {})


class DigestResponseTest(unittest.TestCase):
    def test_response_without_qop(self):
        header = build_digest_header(
            "SUBSCRIBE", URI, CRED["username"], CRED["password"],
            {"realm": "default", "nonce": "nonce123abc"})
        self.assertIn(f'response="{EXPECT_NO_QOP}"', header)

    def test_response_with_qop_auth(self):
        header = build_digest_header(
            "SUBSCRIBE", URI, CRED["username"], CRED["password"],
            {"realm": "default", "nonce": "nonce123abc", "qop": "auth"},
            nc=1, cnonce="abcd1234")
        self.assertIn(f'response="{EXPECT_QOP}"', header)
        self.assertIn("qop=auth", header)
        self.assertIn("nc=00000001", header)
        self.assertIn('cnonce="abcd1234"', header)

    def test_header_carries_the_mandated_fields(self):
        header = build_digest_header(
            "SUBSCRIBE", URI, CRED["username"], CRED["password"],
            {"realm": "default", "nonce": "nonce123abc"})
        self.assertTrue(header.startswith("Digest "))
        for field in (f'username="{CRED["username"]}"', 'realm="default"',
                      'nonce="nonce123abc"', f'uri="{URI}"'):
            self.assertIn(field, header)


class AuthedSubscribeTest(unittest.TestCase):
    """A 407 must produce one authenticated SUBSCRIBE, not a logged rejection."""

    CHALLENGE = CHALLENGE

    def setUp(self):
        self.sent = []
        self.client = SIPClient({
            "local_ip": "127.0.0.1", "local_port": 5080, "transport": "udp",
            "from_user": "200", "display_name": "SIP Client",
            "user_agent": "PythonSIP/1.0",
        })
        self.client.send = lambda data: self.sent.append(data)
        button = BLFButton("201", "201", "default", "Kitchen")
        self.log_lines = []
        self.manager = BLFManager(self.client, [button], auth=CRED,
                                  log=self.log_lines.append)
        self.manager.subscribe_all()
        self.dialog = self.manager._dialogs["201"]

    def challenge_response(self, code, header_name):
        from sip_client import SIPHeaders
        return type("R", (), {
            "method": None, "status_code": code, "reason": "Auth",
            "body": "", "headers": SIPHeaders({
                "Call-ID": self.dialog.call_id,
                header_name: self.CHALLENGE})})()

    def test_proxy_authenticate_resends_subscribe_with_credentials(self):
        self.assertEqual(len(self.sent), 1)
        self.manager.handle_response(self.challenge_response(
            407, "Proxy-Authenticate"))
        self.assertEqual(len(self.sent), 2)
        resent = self.sent[1].decode()
        self.assertIn("Proxy-Authorization: Digest", resent)
        self.assertIn('nonce="bbf37375-e73f-4263-a41a-c087664ea91d"', resent)
        self.assertIn('uri="%s"' % self.dialog.target_uri, resent)
        ha1 = md5("200:default:secret123")
        ha2 = md5("SUBSCRIBE:" + self.dialog.target_uri)
        nonce = "bbf37375-e73f-4263-a41a-c087664ea91d"
        self.assertIn("qop=auth", resent)
        # locate nc/cnonce actually sent and check the response matches them
        nc = re.search(r"nc=([0-9a-f]+)", resent).group(1)
        cnonce = re.search(r'cnonce="([^"]+)"', resent).group(1)
        expected = md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        self.assertIn(f'response="{expected}"', resent)

    def test_www_authenticate_uses_authorization_header(self):
        self.manager.handle_response(self.challenge_response(
            401, "WWW-Authenticate"))
        resent = self.sent[1].decode()
        self.assertIn("Authorization: Digest", resent)
        self.assertNotIn("Proxy-Authorization", resent)

    def test_a_second_407_with_the_same_nonce_does_not_loop(self):
        self.manager.handle_response(self.challenge_response(
            407, "Proxy-Authenticate"))
        self.manager.handle_response(self.challenge_response(
            407, "Proxy-Authenticate"))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(any("rejected" in line for line in self.log_lines))

    def test_fresh_nonces_still_stop_after_a_few_tries(self):
        # A server that mints a new nonce on every attempt must not be able
        # to keep the client resending forever.
        from sip_client import SIPHeaders
        for index in range(10):
            self.manager.handle_response(type("R", (), {
                "method": None, "status_code": 407, "reason": "Auth",
                "body": "", "headers": SIPHeaders({
                    "Call-ID": self.dialog.call_id,
                    "Proxy-Authenticate": ('Digest realm="default", '
                                           f'nonce="nonce{index}", '
                                           "algorithm=MD5, qop=\"auth\"")})})())
        self.assertLessEqual(len(self.sent), 4)
        self.assertTrue(any("rejected" in line for line in self.log_lines))

    def test_407_without_credentials_is_still_a_rejection(self):
        sent = []
        client = SIPClient({"local_ip": "127.0.0.1", "local_port": 5080,
                            "transport": "udp", "from_user": "200"})
        client.send = lambda data: sent.append(data)
        button = BLFButton("201", "201", "default", "Kitchen")
        lines = []
        manager = BLFManager(client, [button], log=lines.append)
        manager.subscribe_all()
        dialog = manager._dialogs["201"]
        from sip_client import SIPHeaders
        manager.handle_response(type("R", (), {
            "method": None, "status_code": 407, "reason": "Auth", "body": "",
            "headers": SIPHeaders({"Call-ID": dialog.call_id,
                                   "Proxy-Authenticate": CHALLENGE})})())
        self.assertEqual(len(sent), 1)
        self.assertTrue(any("rejected: 407" in line for line in lines))

    def test_refresh_after_auth_keeps_the_credentials(self):
        from sip_client import SIPHeaders
        manager = self.manager
        manager.handle_response(type("R", (), {
            "method": None, "status_code": 407, "reason": "Auth", "body": "",
            "headers": SIPHeaders({"Call-ID": self.dialog.call_id,
                                   "Proxy-Authenticate": CHALLENGE})})())
        manager.handle_response(type("R", (), {
            "method": None, "status_code": 200, "reason": "OK", "body": "",
            "headers": SIPHeaders({
                "Call-ID": self.dialog.call_id,
                "To": "<sip:201@default>;tag=serverTAG"})})())
        manager.force_refresh()
        self.assertIn("Proxy-Authorization: Digest", self.sent[2].decode())


class AuthConfigTest(unittest.TestCase):
    def test_build_blf_manager_passes_auth_from_config(self):
        from main import build_blf_manager
        client = SIPClient({"local_ip": "127.0.0.1"})
        config = {
            "blf": {"enabled": True,
                    "buttons": [{"name": "201", "number": "201"}]},
            "auth": {"username": "200", "password": "secret123"},
        }
        manager = build_blf_manager(config, client, log=lambda m: None)
        self.assertEqual(manager.auth, {"username": "200",
                                        "password": "secret123"})

    def test_build_blf_manager_without_auth_keeps_none(self):
        from main import build_blf_manager
        client = SIPClient({"local_ip": "127.0.0.1"})
        config = {"blf": {"enabled": True,
                          "buttons": [{"name": "201", "number": "201"}]}}
        manager = build_blf_manager(config, client, log=lambda m: None)
        self.assertIsNone(manager.auth)


if __name__ == "__main__":
    unittest.main()
