import unittest

from sip_client import SIPMessage


def wire(*lines, body=""):
    return "\r\n".join(list(lines) + ["", body]).encode()


class ParseResponseTest(unittest.TestCase):
    def test_reads_status_code_and_reason(self):
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK", "Call-ID: abc"))
        self.assertEqual(msg.status_code, 200)
        self.assertEqual(msg.reason, "OK")
        self.assertIsNone(msg.method)

    def test_reads_headers(self):
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK", "Call-ID: abc",
                                    "Expires: 3600"))
        self.assertEqual(msg.headers["Call-ID"], "abc")
        self.assertEqual(msg.headers["Expires"], "3600")

    def test_reads_the_body_after_the_blank_line(self):
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK", "Content-Length: 2",
                                    body="ab"))
        self.assertEqual(msg.body, "ab")

    def test_empty_datagram_is_rejected(self):
        self.assertIsNone(SIPMessage.parse(b""))

    def test_malformed_status_line_does_not_raise(self):
        # The client is a long-running process holding live subscriptions; a
        # single junk datagram must not be able to take it down.
        self.assertIsNone(SIPMessage.parse(wire("SIP/2.0 abc OK")))
        self.assertIsNone(SIPMessage.parse(wire("SIP/2.0", "Call-ID: x")))
        self.assertIsNone(SIPMessage.parse(wire("garbage")))


class HeaderFormTest(unittest.TestCase):
    def test_header_without_a_space_after_the_colon(self):
        # RFC 3261 HCOLON does not require the space, and real servers omit it.
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK",
                                    "Content-Type:application/blf+xml"))
        self.assertEqual(msg.headers["Content-Type"], "application/blf+xml")

    def test_header_lookup_ignores_case(self):
        # FreeSWITCH sends Content-Type; other stacks send content-type. A
        # case-sensitive lookup silently dropped those NOTIFYs.
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK",
                                    "content-type: application/blf+xml"))
        self.assertEqual(msg.headers.get("Content-Type"), "application/blf+xml")
        self.assertIn("Content-Type", msg.headers)

    def test_added_header_is_found_regardless_of_case(self):
        msg = SIPMessage()
        msg.add_header("call-id", "abc")
        self.assertEqual(msg.headers.get("Call-ID"), "abc")

    def test_extra_whitespace_after_the_colon_is_stripped(self):
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK", "Call-ID:    abc"))
        self.assertEqual(msg.headers["Call-ID"], "abc")


class BuildTest(unittest.TestCase):
    def test_round_trips_a_request(self):
        raw = (b"NOTIFY sip:101@default SIP/2.0\r\n"
               b"Call-ID: call-1\r\n"
               b"CSeq: 3 NOTIFY\r\n"
               b"Content-Length: 4\r\n"
               b"\r\n"
               b"<xml")
        msg = SIPMessage.parse(raw)
        self.assertEqual(msg.method, "NOTIFY")
        self.assertEqual(msg.uri, "sip:101@default")
        self.assertEqual(msg.headers["Call-ID"], "call-1")
        self.assertEqual(msg.body, "<xml")


class RepeatedHeaderTest(unittest.TestCase):
    """A request may carry the same header more than once, and the answer must
    copy every one of them. Collapsing them kept only the last Via, so a
    response through a proxy chain was routed to the wrong hop, and the next
    leg never got one at all."""

    def test_all_values_returns_every_copy_in_order(self):
        msg = SIPMessage.parse(wire(
            "NOTIFY sip:101@default SIP/2.0",
            "Via: SIP/2.0/UDP proxy1;branch=1",
            "Via: SIP/2.0/UDP edge;branch=2",
            "Call-ID: c"))
        self.assertEqual(msg.headers.all_values("Via"),
                         ["SIP/2.0/UDP proxy1;branch=1",
                          "SIP/2.0/UDP edge;branch=2"])

    def test_all_values_returns_a_single_copy_unchanged(self):
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK", "Call-ID: abc"))
        self.assertEqual(msg.headers.all_values("Call-ID"), ["abc"])

    def test_all_values_of_an_absent_header_is_empty(self):
        msg = SIPMessage()
        self.assertEqual(msg.headers.all_values("Via"), [])

    def test_build_repeats_a_multi_valued_header(self):
        msg = SIPMessage(status_code=200, reason="OK")
        msg.add_header("Via", "SIP/2.0/UDP proxy1;branch=1")
        msg.add_header("Via", "SIP/2.0/UDP edge;branch=2")
        built = msg.build()
        self.assertEqual(built.count("Via: "), 2)

    def test_ordinary_headers_are_still_emitted_once(self):
        msg = SIPMessage(status_code=200, reason="OK")
        msg.add_header("Call-ID", "c")
        self.assertEqual(msg.build().count("Call-ID: "), 1)

    def test_deleting_a_header_removes_every_copy(self):
        msg = SIPMessage.parse(wire("SIP/2.0 200 OK", "Via: a", "Via: b"))
        del msg.headers["Via"]
        self.assertEqual(msg.headers.all_values("Via"), [])


if __name__ == "__main__":
    unittest.main()