import unittest

from sip_client import SIPClient, SIPDialog


def make_client():
    return SIPClient({
        "local_ip": "192.168.0.10",
        "local_port": 5080,
        "transport": "udp",
        "from_user": "100",
        "display_name": "SIP Client",
        "user_agent": "PythonSIP/1.0",
        "rtp_port": 10000,
    })


class DialogMessageShapeTest(unittest.TestCase):
    """Guards the bytes the client puts on the wire. Header order is not asserted."""

    def setUp(self):
        self.client = make_client()
        self.dialog = self.client.create_dialog("sip:101@192.168.0.89:5060")

    def test_invite_carries_expected_headers(self):
        msg = self.dialog.build_invite()
        self.assertEqual(msg.method, "INVITE")
        self.assertEqual(msg.uri, "sip:101@192.168.0.89:5060")
        self.assertIn("SIP/2.0/UDP 192.168.0.10:5080", msg.headers["Via"])
        self.assertIn(";rport", msg.headers["Via"])
        self.assertEqual(msg.headers["Max-Forwards"], "70")
        self.assertIn("<sip:100@192.168.0.10:5080>;tag=", msg.headers["From"])
        self.assertEqual(msg.headers["To"], "<sip:101@192.168.0.89:5060>")
        self.assertEqual(msg.headers["CSeq"], "1 INVITE")
        self.assertEqual(msg.headers["Contact"],
                         "<sip:100@192.168.0.10:5080;transport=udp>")
        self.assertEqual(msg.headers["User-Agent"], "PythonSIP/1.0")
        self.assertEqual(msg.headers["Content-Type"], "application/sdp")
        self.assertTrue(msg.body.startswith("v=0\r\n"))

    def test_ack_reuses_invite_cseq_and_carries_to_tag(self):
        self.dialog.build_invite()
        msg = self.dialog.build_ack(to_tag="remoteTAG")
        self.assertEqual(msg.method, "ACK")
        self.assertEqual(msg.headers["CSeq"], "1 ACK")
        self.assertEqual(msg.headers["To"],
                         "<sip:101@192.168.0.89:5060>;tag=remoteTAG")
        self.assertEqual(msg.headers["Call-ID"], self.dialog.call_id)

    def test_bye_increments_cseq_and_keeps_call_id(self):
        invite = self.dialog.build_invite()
        ack = self.dialog.build_ack(to_tag="remoteTAG")
        msg = self.dialog.build_bye(to_tag="remoteTAG")
        self.assertEqual(msg.method, "BYE")
        self.assertEqual(msg.headers["CSeq"], "2 BYE")
        self.assertEqual(msg.headers["Call-ID"], invite.headers["Call-ID"])
        self.assertEqual(msg.headers["Call-ID"], ack.headers["Call-ID"])

    def test_building_advances_cseq_exactly_once(self):
        self.dialog.build_invite()
        self.assertEqual(self.dialog.cseq, 2)

    def test_every_dialog_has_independent_identity(self):
        other = self.client.create_dialog("sip:102@192.168.0.89:5060")
        self.assertNotEqual(other.call_id, self.dialog.call_id)
        self.assertNotEqual(other.from_tag, self.dialog.from_tag)
        self.assertEqual(other.cseq, 1)


class SIPClientTest(unittest.TestCase):
    def test_sdp_advertises_configured_media_port(self):
        sdp = make_client().build_sdp()
        self.assertIn("m=audio 10000 RTP/AVP 0 8 101", sdp)
        self.assertIn("c=IN IP4 192.168.0.10", sdp)

    def test_parse_sdp_reads_media_address(self):
        parsed = make_client().parse_sdp(
            "v=0\r\nc=IN IP4 10.0.0.5\r\nm=audio 12000 RTP/AVP 0\r\n")
        self.assertEqual(parsed["media_ip"], "10.0.0.5")
        self.assertEqual(parsed["media_port"], 12000)

    def test_create_dialog_binds_to_client(self):
        client = make_client()
        dialog = client.create_dialog("sip:101@192.168.0.89:5060")
        self.assertIsInstance(dialog, SIPDialog)
        self.assertIs(dialog.client, client)

    def test_send_without_open_socket_raises(self):
        with self.assertRaises(RuntimeError):
            make_client().send(b"test")


if __name__ == "__main__":
    unittest.main()